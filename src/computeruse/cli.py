"""Command-line entrypoint: run the composed agent against one goal.

``python -m computeruse --goal "..."`` wires the whole stack — driver process
(ADR-1), autonomy guard, kill-switch, visual sensor, episodic memory and skill
distillation — into a single runnable OODA loop.

The default *provider* is a scripted demo (two clicks, then finish) so the
stack can be exercised end to end without an LLM; pass ``--provider module:fn``
to inject any ``Callable[[WorkingState], AgentTurn]`` (e.g. a real model
client). The demo drives the *simulated* driver by default; ``--real`` passes
through to the driver for actual host actuation on macOS.

Note on ``--verify``: verification itself is always on — every action is
checked against whatever witnesses exist (the accessibility surface, the
focused field's value, the frontmost app). ``--verify`` adds the *pixel*
witness, which costs a screen capture per verified action and is the only
witness that can see a purely visual change.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from computeruse.agent import Agent, AgentConfig
from computeruse.orchestrator.budget import (
    BudgetExceededError,
    RunBudget,
    RunUsage,
    budget_verdict,
)
from computeruse.orchestrator.client import ActuationClient, DriverRpcError
from computeruse.orchestrator.evidence import CompletionVerdict
from computeruse.orchestrator.loop import (
    KillSwitchTripped,
    MaxStepsError,
    StuckLoopError,
    UnrecoverableFailureError,
    WorkingState,
)
from computeruse.orchestrator.prompts import completion_auditor, scaffolded_provider
from computeruse.orchestrator.schemas import AgentTurn, Finish, MouseClick
from computeruse.providers.openai import (
    DEFAULT_MODEL,
    ModelCallStats,
    OpenAIError,
    TokenPrice,
    call_cost_usd,
    openai_model,
    price_for,
)
from computeruse.security.autonomy import (
    AutonomyLevel,
    PermissionConfirmationRequired,
    PermissionDeniedError,
)
from computeruse.security.killswitch import KillSwitch, install_sigint_catcher
from computeruse.vision.apps import extract_goal_app, infer_target_app

DEFAULT_SOCKET = "/tmp/actuation-driver.sock"
DEFAULT_STORE = Path.home() / ".computeruse"
DEMO_PROVIDER: str = "demo"
OPENAI_PREFIX: str = "openai"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="computeruse",
        description="Autonomous physical computer-use agent (macOS).",
    )
    parser.add_argument("--goal", required=True, help="The task to accomplish.")
    parser.add_argument(
        "--app",
        default=None,
        help="Target application name, e.g. 'Google Chrome' (default: auto-discover "
        "from the focused window). With --real the named app is brought to the "
        "front before the run.",
    )
    parser.add_argument(
        "--provider",
        default=DEMO_PROVIDER,
        help=f"Provider: {DEMO_PROVIDER!r} or 'module:callable' (default: {DEMO_PROVIDER!r}).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Raw text model; wrapped by the weak-model scaffolding (prompt "
        f"building + corrective retries). '{OPENAI_PREFIX}[:model_id]' uses the "
        f"built-in OpenAI transport (default model {DEFAULT_MODEL!r}, key from "
        "OPENAI_API_KEY); otherwise 'module:callable' (str -> str). Takes "
        "precedence over --provider.",
    )
    parser.add_argument(
        "--level",
        type=int,
        choices=[0, 1, 2, 3],
        default=AutonomyLevel.GUARDED.value,
        help="Autonomy level 0-3 (default: 2 = guarded).",
    )
    parser.add_argument("--socket", default=DEFAULT_SOCKET, help="Driver Unix socket path.")
    parser.add_argument(
        "--driver",
        default=None,
        help="Path to the actuation-driver binary; spawns it if the socket is absent.",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="Spawn the driver with the real (Quartz) backend instead of simulated.",
    )
    parser.add_argument(
        "--verify",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Add the pixel witness to action verification (a screen capture "
        "before and after each verifiable action). Verification against the "
        "accessibility surface and focused-field values runs regardless. "
        "Defaults to ON for real LLM runs; pass --no-verify to disable.",
    )
    parser.add_argument(
        "--vision",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable multimodal visual perception (screenshot to VLM).",
    )
    parser.add_argument("--store", default=str(DEFAULT_STORE), help="Episodes + skills directory.")
    parser.add_argument("--max-steps", type=int, default=100)
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Decompose the goal into ordered sub-goals and advance through "
        "them (a finish marks the current sub-goal done); checkpoints are "
        "written to --store/checkpoints for resumability.",
    )
    parser.add_argument(
        "--deadline-seconds",
        type=float,
        default=None,
        help="Wall-clock ceiling for the run. Checked between steps; when it "
        "is passed the run stops cleanly with its artifacts already written.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Total model-token ceiling for the run (prompt + completion, "
        "summed across every call). Checked between steps.",
    )
    parser.add_argument(
        "--max-cost",
        type=float,
        default=None,
        help="Model-spend ceiling for the run, in USD, computed from published "
        "list prices. Only available for a priced --model openai[:id]; a "
        "custom transport has no known price, so use --max-tokens there.",
    )
    parser.add_argument(
        "--trace-dir",
        default=None,
        help="Write one JSON object per step (decision, action, verification "
        "verdict, error) to <trace-dir>/<run_id>/steps.jsonl. Off by default.",
    )
    parser.add_argument(
        "--trace-screenshots",
        action="store_true",
        help="With --trace-dir, also save the exact frame the model decided "
        "from for each step, as <trace-dir>/<run_id>/step-NNN.png.",
    )
    return parser.parse_args(argv)


def scripted_provider(goal: str) -> Callable[[WorkingState], AgentTurn]:
    """A deterministic demo provider: two clicks, then finish.

    Two steps is the distiller's minimum for a *skill*, so the demo exercises
    the full learn loop: run -> distill -> remember.
    """

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="demo step one",
                sub_goal=f"{goal}: first click",
                action=MouseClick(type="mouse_click", x=100, y=100),
            )
        if state.step_index == 1:
            return AgentTurn(
                thought="demo step two",
                sub_goal=f"{goal}: second click",
                action=MouseClick(type="mouse_click", x=200, y=200),
            )
        return AgentTurn(
            thought="done",
            sub_goal="workflow complete",
            action=Finish(type="finish", status="success", summary="demo workflow done"),
        )

    return provider


def discover_app(socket_path: str) -> str:
    """Resolve the frontmost app via the driver; ``"unknown"`` when unreachable.

    Best-effort context, not a hard requirement: a missing driver or a refused
    probe degrades to ``"unknown"`` (with a warning) rather than aborting —
    but the resolution happens *before* the provider is built, so a
    ``--model`` scaffold can name the real app in its prompt.
    """
    try:
        with ActuationClient(socket_path, connect_retries=1) as client:
            return client.focused_window().app_name or "unknown"
    except Exception as exc:  # noqa: BLE001 - boundary catch-all: surface the real reason
        # Carry the real reason into the warning: "unknown" alone hides whether
        # the driver is down or consent is missing, and the fix differs (Law
        # 6.3: explicit context, never a silent swallow).
        print(
            f"warning: could not discover the focused app ({exc}); using 'unknown'",
            file=sys.stderr,
        )
        return "unknown"


def load_provider(spec: str, goal: str) -> Callable[[WorkingState], AgentTurn]:
    """Resolve a ``module:callable`` spec to a provider function.

    The demo provider is built around the actual goal so its sub-goals carry
    the task text (which the autonomy guard reads for risk classification).
    """
    if spec == DEMO_PROVIDER:
        return scripted_provider(goal)
    return cast(Callable[[WorkingState], AgentTurn], _load_callable(spec, "provider"))


@dataclass(frozen=True)
class ModelBinding:
    """The two callables one model transport is used for.

    ``provider`` decides the next action; ``completion_check`` re-reads the
    screen to challenge a claimed success. They share a transport (one API key,
    one model, one usage counter) but never share a context — the whole point
    of the audit is that it is not persuaded by the reasoning it is checking.
    """

    provider: Callable[[WorkingState], AgentTurn]
    completion_check: Callable[[WorkingState, str], CompletionVerdict]


def load_model_binding(
    spec: str,
    *,
    app: str,
    max_steps: int = 100,
    stats_sink: Callable[[object], None] | None = None,
) -> ModelBinding:
    """Resolve a raw text model and wrap it with the weak-model scaffolding.

    ``openai[:model_id]`` selects the built-in OpenAI transport (key resolved by
    :func:`load_api_key`); any other
    ``module:callable`` spec is imported as a user-provided ``str -> str``
    transport. Either way the scaffolding builds the prompt from the working state
    (goal, last error, knowledge, mounted skill) and retries with corrective hints
    when the model emits invalid JSON (Law 2.1). ``max_steps`` is passed through
    so the prompt always states the true step budget.
    """
    load_api_key()

    if spec == OPENAI_PREFIX or spec.startswith(f"{OPENAI_PREFIX}:"):
        model_id = spec.split(":", 1)[1].strip() if ":" in spec else ""
        if _accepts_keyword(openai_model, "stats_sink"):
            model = cast(Callable[[str], str], openai_model(model_id or None, stats_sink=stats_sink))
        else:
            # Legacy/experimental transports that predate usage telemetry get
            # a plain call; the panel then simply shows no token counters.
            model = cast(Callable[[str], str], openai_model(model_id or None))
    else:
        model = cast(Callable[[str], str], _load_callable(spec, "model"))
    return ModelBinding(
        provider=scaffolded_provider(model, app=app, max_steps=max_steps),
        completion_check=completion_auditor(model, app=app),
    )


def load_model(
    spec: str,
    *,
    app: str,
    max_steps: int = 100,
    stats_sink: Callable[[object], None] | None = None,
) -> Callable[[WorkingState], AgentTurn]:
    """Resolve a model spec to just its decision provider.

    The thin form for callers that only drive actions; ``load_model_binding``
    is what the CLI uses, because a real run also needs the completion
    auditor bound to the same transport.
    """
    return load_model_binding(
        spec, app=app, max_steps=max_steps, stats_sink=stats_sink
    ).provider


#: Where the OpenAI key is looked for, in precedence order. The process
#: environment always wins; a repo-local ``.env`` beats the shared per-user
#: store so a checkout can be pointed at a different key without touching the
#: machine's default. Both files are gitignored — a key must never be committed.
ENV_FILES: tuple[Path, ...] = (Path(".env"), DEFAULT_STORE / "env")


def load_api_key() -> bool:
    """Populate ``OPENAI_API_KEY`` from the first env file that defines it.

    Returns whether a key is available afterwards. An already-exported variable
    is never overwritten: an explicit ``OPENAI_API_KEY=... computeruse ...`` must
    beat whatever a file happens to contain, or a user cannot switch keys for a
    single run.

    Parsing is deliberately minimal (``KEY=value``, quotes stripped, ``#``
    comments and blank lines ignored) rather than a dotenv dependency: this
    reads a secret, and a small explicit parser is easier to audit than an
    import. Unreadable files are skipped — a permissions problem on one
    location must not stop the next from being tried — and the caller reports
    the missing key with actionable text.
    """
    if os.environ.get("OPENAI_API_KEY"):
        return True
    for env_file in ENV_FILES:
        try:
            if not env_file.is_file():
                continue
            for raw in env_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                if name.strip() != "OPENAI_API_KEY":
                    continue
                value = value.strip().strip("\"'")
                if value:
                    os.environ["OPENAI_API_KEY"] = value
                    return True
        except OSError as exc:
            print(f"warning: could not read {env_file} ({exc})", file=sys.stderr)
    return False


def _accepts_keyword(func: Callable[..., object], keyword: str) -> bool:
    """True when ``func`` can accept the named keyword argument.

    Inspects ``**kwargs`` or explicit keyword parameters instead of relying on
    a trial call (which would double-invoke user code); bound methods and
    builtins without introspectable signatures default to accepting.
    """
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return True
    params = signature.parameters
    return any(
        p.kind is inspect.Parameter.VAR_KEYWORD
        or (p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY) and p.name == keyword)
        for p in params.values()
    )


def _load_callable(spec: str, kind: str) -> Callable[..., object]:
    """Import ``module:attr`` from a spec (caller casts to its contract)."""
    module_name, _, attr = spec.partition(":")
    if not attr:
        raise ValueError(f"{kind} spec must be 'module:callable', got {spec!r}")
    module = importlib.import_module(module_name)
    callable_ = getattr(module, attr)
    if not callable(callable_):
        raise TypeError(f"{spec!r} does not resolve to a callable")
    return callable_


def cli_confirm_handler(turn: AgentTurn) -> bool:
    """Prompt the user on stderr/stdin for actions that require confirmation.

    Reads exactly one line from stdin so the channel works both for an
    interactive terminal (typing y/N) and for the menu launcher's piped
    channel (the panel's Approve/Deny buttons write the answer — M6). Unlike
    ``input()``, ``readline()`` does not echo a prompt to stdout, which would
    leak into the panel's log stream. EOF (pipe closed / stopped run) is a
    denial — fail-closed (Law 5).
    """
    print(
        f"\n⚠️  CONFIRMATION REQUIRED: Agent wants to perform [{turn.action.type}] for goal: {turn.sub_goal!r}",
        file=sys.stderr,
    )
    print(f"   Payload: {turn.action.model_dump(exclude_none=True)}", file=sys.stderr)
    try:
        line = sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        return False
    return line.strip().lower() in ("y", "yes")


def resolve_verify(args: argparse.Namespace) -> bool:
    """Resolve the effective ``--verify`` value (explicit flag wins).

    An explicit ``--verify``/``--no-verify`` is honored as-is. Otherwise the
    pixel witness defaults ON for real LLM runs: it is the only witness that
    sees a purely visual change, and pairing it with the accessibility witness
    is what lets a genuine miss be called a miss (two independent witnesses
    must agree before an action is declared failed). Demo and simulated runs
    default OFF — they are exercising the wiring, not a real display, and the
    capture pair is pure cost there.
    """
    if args.verify is not None:
        return args.verify
    return bool(args.real and args.model is not None)


def resolve_cost_price(args: argparse.Namespace) -> TokenPrice | None:
    """The published price to bill this run against, when --max-cost is set.

    Returns ``None`` when no cost ceiling was asked for. Raises
    :class:`~computeruse.providers.openai.OpenAIError` when a ceiling *was*
    asked for against a transport whose price nobody published — a budget
    enforced against a made-up number is worse than no budget at all.
    """
    if args.max_cost is None:
        return None
    model_spec: str | None = args.model
    if model_spec is None:
        raise OpenAIError(
            "--max-cost needs a model to price; pass --model openai[:model_id]"
        )
    if model_spec != OPENAI_PREFIX and not model_spec.startswith(f"{OPENAI_PREFIX}:"):
        raise OpenAIError(
            f"--max-cost cannot price the custom transport {model_spec!r}; "
            "use --max-tokens instead"
        )
    model_id = model_spec.split(":", 1)[1].strip() if ":" in model_spec else ""
    return price_for(model_id or DEFAULT_MODEL)


def build_config(
    args: argparse.Namespace,
    *,
    goal: str,
    activate_named_app: bool,
    app_inferred: bool = False,
    stats_sink: Callable[[object], None] | None = None,
    budget_guard: Callable[[], None] | None = None,
) -> AgentConfig:
    """Compose the CLI's args into the single immutable config the agent runs.

    ``activate_named_app`` is True when the resolved app (user-named via
    ``--app`` *or* inferred from the goal) should be brought to the front on
    a real backend. ``app_inferred`` marks a goal-inferred app: its
    activation failure (e.g. the app is not installed) must not abort the
    run — the agent proceeds on the frontmost app and adapts (autonomy over
    hard failure).
    """
    completion_check: Callable[[WorkingState, str], CompletionVerdict] | None = None
    if args.model is not None:
        binding = load_model_binding(
            args.model,
            app=args.app or "unknown",
            max_steps=args.max_steps,
            stats_sink=stats_sink,
        )
        provider = binding.provider
        # Only a real model can audit its own completion claim against the
        # screen; a scripted provider's "success" is the test's own fixture
        # and must be taken at face value.
        completion_check = binding.completion_check
    else:
        provider = load_provider(args.provider, goal)
    confirm_handler = (
        cli_confirm_handler
        if sys.stdin.isatty() or os.environ.get("COMPUTERUSE_MENU") == "1"
        else None
    )
    return AgentConfig(
        goal=goal,
        app=args.app,
        provider=provider,
        socket_path=args.socket,
        store_dir=Path(args.store),
        autonomy_level=AutonomyLevel(args.level),
        confirm_handler=confirm_handler,
        enable_visual_verification=resolve_verify(args),
        enable_vision=getattr(args, "vision", True),
        # OBSERVE precondition: a *resolved* app (user-named or goal-inferred)
        # on a *real* backend is activated (the simulated backend never touches
        # the host — Law 1). An auto-discovered app is never activated:
        # discovery already names the frontmost app, and activating it again
        # would be a no-op at best.
        activate_app_on_start=activate_named_app and args.app is not None,
        tolerate_activation_failure=app_inferred,
        # Ctrl-C at any moment reclaims control (Law 5 fail-safe); polled live
        # between steps via the signal predicate.
        kill_switch=KillSwitch(monitor=None, signal_predicate=install_sigint_catcher()),
        completion_check=completion_check,
        max_steps=args.max_steps,
        enable_planning=getattr(args, "plan", False),
        trace_dir=Path(args.trace_dir) if args.trace_dir is not None else None,
        trace_screenshots=getattr(args, "trace_screenshots", False),
        budget_guard=budget_guard,
    )


def spawn_driver(binary: str, socket_path: str, *, real: bool) -> subprocess.Popen[bytes]:
    """Start the driver if the socket is not already served; return the process.

    The driver logs its startup diagnosis on stderr — most importantly *why*
    it refuses to run (e.g. missing Accessibility consent), so a bare "code 1"
    is useless to the user. stderr is piped to a daemon drain thread (never
    DEVNULL: a full pipe would block the driver mid-run) and the last lines
    are attached to any startup error, so the reason always reaches the panel
    (Law 6.3 explicit error propagation).
    """
    # Remove a stale socket file before spawning — otherwise a crashed run's
    # leftover file would make the wait loop (and the client) race a dead
    # socket. The driver also removes it at startup; this makes it deterministic.
    Path(socket_path).unlink(missing_ok=True)
    command = [binary, socket_path]
    if real:
        command.append("--real")
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    stderr_tail: list[str] = []

    def _drain_stderr() -> None:
        """Keep the pipe drained and remember the tail for diagnostics."""
        assert process.stderr is not None
        for raw in process.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                stderr_tail.append(line)
                if len(stderr_tail) > 20:
                    del stderr_tail[:-20]

    drain_thread = threading.Thread(target=_drain_stderr, daemon=True)
    drain_thread.start()

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if Path(socket_path).exists():
            return process
        if process.poll() is not None:
            # The driver has exited; let the drain thread see EOF so the
            # failure message carries the driver's own diagnosis.
            drain_thread.join(timeout=0.5)
            detail = "\n".join(stderr_tail) or "the driver produced no output"
            raise RuntimeError(
                f"driver exited during startup (code {process.returncode}): {detail}"
            )
        time.sleep(0.05)
    process.terminate()
    drain_thread.join(timeout=0.5)
    detail = "\n".join(stderr_tail) or "the driver produced no output"
    raise RuntimeError(f"driver did not create socket {socket_path} in time: {detail}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    # Live step visibility: the runner logs every executed physical action at
    # INFO, and a real run takes seconds per LLM decision — without this the
    # terminal stays silent mid-run and the user sees "Chrome opened, nothing
    # happens". Stream the runner's lines to stderr so the run is observable
    # while the final summary block still lands on stdout at the end.
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    if args.model is None and args.provider == DEMO_PROVIDER:
        # The demo provider does two fixed clicks then finishes — deliberately
        # so the stack runs end-to-end without an LLM. On a *real* host that
        # reads as a broken agent, so never run it silently (Law 6.3).
        print(
            "WARNING: no --model set, so the scripted DEMO provider (two fixed "
            "clicks, then finish) will run, not an LLM. Pass --model openai to "
            "drive the real model.",
            file=sys.stderr,
        )
    if args.verify is None and resolve_verify(args):
        # Verification now defaults ON for real LLM runs: a miss must be
        # caught and folded into the model's next decision, or the agent
        # keeps clicking the same wrong spot (the observed failure mode).
        print(
            "note: --verify auto-enabled for this real run (a click that "
            "misses is caught and reported to the agent); pass --no-verify "
            "to disable.",
            file=sys.stderr,
        )
    if (
        args.real
        and args.model is not None
        and getattr(args, "vision", True)
        and not resolve_verify(args)
    ):
        # Vision (screenshot to the VLM) is on but the pixel witness is off.
        # Verification still runs against the accessibility surface, but a
        # purely visual change has no second witness — and an action is only
        # declared failed when two independent witnesses agree, so misses in
        # visual-only UI go unreported. These are independent switches; say so.
        print(
            "warning: --vision is on but --verify is off: the agent sees the "
            "screen, but actions are verified only against the accessibility "
            "surface. A miss in purely visual UI has no second witness and "
            "will not be reported. Pass --verify with --real for the full "
            "closed loop.",
            file=sys.stderr,
        )
    driver_process: subprocess.Popen[bytes] | None = None
    try:
        # When a driver binary is given we always spawn it (stale sockets are
        # cleared first); only without --driver do we attach to a running one.
        if args.driver is not None:
            driver_process = spawn_driver(args.driver, args.socket, real=args.real)
        # Autonomous app resolution: the goal may carry an explicit `[App
        # Name]` prefix, or name the target implicitly ("Excel'de aç",
        # "YouTube'da arat"). Resolve it here so the provider sees the
        # cleaned goal (no bracket wrapper) and the run can bring the right
        # app to the front without the user passing --app — autonomy by
        # design, not by configuration.
        explicit_app, cleaned_goal = extract_goal_app(args.goal)
        args.goal = cleaned_goal
        named_app = args.app is not None
        app_inferred_from_goal = explicit_app is not None
        if args.app is None:
            # The running-app list disambiguates service goals ("YouTube'da"
            # -> the running browser) but never restricts the result: `open
            # -a` can launch a not-running app. Best-effort by contract — a
            # probe failure degrades to inference without the list.
            running_apps: tuple[str, ...] = ()
            try:
                with ActuationClient(args.socket, connect_retries=1) as client:
                    running_apps = client.list_apps()
            except Exception as exc:  # noqa: BLE001 - inference is best-effort
                print(
                    f"warning: could not list running apps ({exc}); inferring without them",
                    file=sys.stderr,
                )
            inferred_app = explicit_app or infer_target_app(args.goal, running_apps)
            if inferred_app is not None:
                args.app = inferred_app
                app_inferred_from_goal = True
            else:
                # OBSERVE before DECIDE: no explicit or inferable app —
                # discover the frontmost one so the provider (and its scaffold
                # prompt) names the real app from the first turn.
                args.app = discover_app(args.socket)
        # Live usage telemetry: every successful model call reports its token
        # usage and per-call latency; each report is streamed to stderr as a
        # compact "st :" line the menu panel parses into its header counters
        # (token + elapsed), keeping the transport decoupled from the UI.
        run_started_at = time.monotonic()
        run_tokens: dict[str, int] = {"total": 0}
        run_calls = 0

        run_cost: dict[str, float] = {"usd": 0.0}
        # Resolved once, before the run: a cost ceiling against a model whose
        # price is unknown must fail at startup with an actionable message, not
        # twenty steps in when the guard first tries to evaluate it.
        price = resolve_cost_price(args)

        def stats_sink(call: object) -> None:
            nonlocal run_calls
            run_calls += 1
            if isinstance(call, ModelCallStats):
                run_tokens["total"] += call.total_tokens
                if price is not None:
                    run_cost["usd"] += call_cost_usd(price, call)
            elapsed = time.monotonic() - run_started_at
            print(
                f"st : tok_total={run_tokens['total']} elapsed={elapsed:.1f}s calls={run_calls}",
                file=sys.stderr,
                flush=True,
            )

        budget = RunBudget(
            deadline_seconds=args.deadline_seconds,
            max_tokens=args.max_tokens,
            max_cost_usd=args.max_cost,
        )

        def budget_guard() -> None:
            reason = budget_verdict(
                budget,
                RunUsage(
                    elapsed_seconds=time.monotonic() - run_started_at,
                    total_tokens=run_tokens["total"],
                    cost_usd=run_cost["usd"],
                ),
            )
            if reason is not None:
                raise BudgetExceededError(reason)

        config = build_config(
            args,
            goal=args.goal,
            activate_named_app=args.real and (named_app or app_inferred_from_goal),
            app_inferred=app_inferred_from_goal and not named_app,
            stats_sink=stats_sink,
            budget_guard=None if budget.is_unset else budget_guard,
        )
        result = Agent(config).run()

        print(f"goal        : {config.goal}")
        print(f"run_id      : {result.run_id}")
        print(f"app         : {result.app}")
        if config.trace_dir is not None:
            print(f"trace       : {config.trace_dir / result.run_id}")
        if config.activate_app_on_start:
            print(f"activated   : {config.app}")
        print(f"steps       : {len(result.state.completed_steps)}")
        if result.state.last_error:
            print(f"last_error  : {result.state.last_error}")
        if result.distilled is not None:
            label = f"distill     : {result.distilled.kind}"
            if result.distilled.definition is not None:
                label += f" ({result.distilled.definition.skill_id})"
            print(label)
        print(f"episodes    : {len(result.episodes)}")
        print(f"skills      : {len(result.skills)}")
        print(f"knowledge   : {len(result.knowledge)} entries for {config.app}")
        if result.skill is not None:
            print(f"skill       : {result.skill.skill_id} (mounted)")
        return 0
    except KillSwitchTripped:
        print("interrupted: human reclaimed control (kill-switch tripped)", file=sys.stderr)
        return 130
    except PermissionDeniedError as exc:
        print(f"blocked by autonomy guard: {exc}", file=sys.stderr)
        return 1
    except PermissionConfirmationRequired as exc:
        # A guarded/destructive decision paused for human sign-off: tell the
        # user what the model proposed so they can approve or steer it.
        print(f"confirmation required: {exc}", file=sys.stderr)
        return 1
    except DriverRpcError as exc:
        # A driver-side refusal (missing consent, unknown method) surfaces as
        # one clean line instead of a traceback; the driver's own message is
        # already the actionable hint.
        print(f"driver error: {exc}", file=sys.stderr)
        return 1
    except OpenAIError as exc:
        # Model-transport failures (missing key, API error) surface cleanly;
        # the user fixes the key/model and reruns (Law 6.3: explicit context).
        print(f"model transport error: {exc}", file=sys.stderr)
        return 1
    except StuckLoopError as exc:
        # The provider repeated one action with no progress (Law 2 guard): the
        # run ended by design, not by accident — say so plainly.
        print(f"stuck loop: {exc}", file=sys.stderr)
        return 1
    except UnrecoverableFailureError as exc:
        # The recovery ladder ran out: the agent could not get past one
        # obstacle. Report the classified failure rather than a traceback —
        # the kind names what to fix (consent, a wrong app, a dead driver).
        print(f"unrecoverable failure ({exc.failure.kind.value}): {exc}", file=sys.stderr)
        return 1
    except BudgetExceededError as exc:
        # A ceiling the operator set, not a failure of the agent: the run
        # stopped between steps with its episode and trace already written.
        print(f"budget stop: {exc}", file=sys.stderr)
        return 1
    except MaxStepsError as exc:
        # The loop hit its step budget without a finish; the user sees why
        # instead of a silent stop.
        print(f"max steps: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        # Setup/startup failures (driver spawn, fail-fast sensor probe) are
        # user-facing conditions, not bugs: one clean line beats a traceback.
        # Kept last: OpenAIError and DriverRpcError both derive from
        # RuntimeError, so their clauses must precede this one.
        print(f"setup error: {exc}", file=sys.stderr)
        return 1
    finally:
        if driver_process is not None and driver_process.poll() is None:
            driver_process.terminate()
            driver_process.wait(timeout=5)
