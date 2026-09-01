"""Command-line entrypoint: run the composed agent against one goal.

``python -m computeruse --goal "..."`` wires the whole stack — driver process
(ADR-1), autonomy guard, kill-switch, visual sensor, episodic memory and skill
distillation — into a single runnable OODA loop.

The default *provider* is a scripted demo (two clicks, then finish) so the
stack can be exercised end to end without an LLM; pass ``--provider module:fn``
to inject any ``Callable[[WorkingState], AgentTurn]`` (e.g. a real model
client). The demo drives the *simulated* driver by default; ``--real`` passes
through to the driver for actual host actuation on macOS.

Note on ``--verify``: the simulated driver cannot render, so a click never
visibly changes anything and ORIENT verification would fail by design. Enable
``--verify`` only with ``--real``, where the pixels can actually respond.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from computeruse.agent import Agent, AgentConfig
from computeruse.orchestrator.client import ActuationClient, DriverRpcError
from computeruse.orchestrator.loop import (
    KillSwitchTripped,
    MaxStepsError,
    StuckLoopError,
    WorkingState,
)
from computeruse.orchestrator.prompts import scaffolded_provider
from computeruse.orchestrator.schemas import AgentTurn, Finish, MouseClick
from computeruse.providers.openai import DEFAULT_MODEL, OpenAIError, openai_model
from computeruse.security.autonomy import (
    AutonomyLevel,
    PermissionConfirmationRequired,
    PermissionDeniedError,
)
from computeruse.security.killswitch import KillSwitch, install_sigint_catcher

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
        action="store_true",
        help="Enable ORIENT visual verification (needs --real; simulated never renders).",
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


def load_model(
    spec: str, *, app: str, max_steps: int = 100
) -> Callable[[WorkingState], AgentTurn]:
    """Resolve a raw text model and wrap it with the weak-model scaffolding.

    ``openai[:model_id]`` selects the built-in OpenAI transport (key from the
    ``OPENAI_API_KEY`` environment variable or ``~/.computeruse/env``); any other
    ``module:callable`` spec is imported as a user-provided ``str -> str``
    transport. Either way the scaffolding builds the prompt from the working state
    (goal, last error, knowledge, mounted skill) and retries with corrective hints
    when the model emits invalid JSON (Law 2.1). ``max_steps`` is passed through
    so the prompt always states the true step budget.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        env_file = DEFAULT_STORE / "env"
        if env_file.is_file():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("OPENAI_API_KEY="):
                        val = line.split("=", 1)[1].strip().strip("\"'")
                        if val:
                            os.environ["OPENAI_API_KEY"] = val
            except OSError:
                pass

    if spec == OPENAI_PREFIX or spec.startswith(f"{OPENAI_PREFIX}:"):
        model_id = spec.split(":", 1)[1].strip() if ":" in spec else ""
        model = openai_model(model_id or None)
    else:
        model = cast(Callable[[str], str], _load_callable(spec, "model"))
    return scaffolded_provider(model, app=app, max_steps=max_steps)


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
    """Prompt the user on stderr/stdin for actions that require confirmation."""
    print(
        f"\n⚠️  CONFIRMATION REQUIRED: Agent wants to perform [{turn.action.type}] for goal: {turn.sub_goal!r}",
        file=sys.stderr,
    )
    print(f"   Payload: {turn.action.model_dump(exclude_none=True)}", file=sys.stderr)
    try:
        ans = input("Approve and execute this action? (y/N): ").strip().lower()
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def build_config(args: argparse.Namespace, *, goal: str, activate_named_app: bool) -> AgentConfig:
    """Compose the CLI's args into the single immutable config the agent runs.

    ``activate_named_app`` is True only when the user *named* the app (via
    ``--app``) *and* the run uses the real backend.
    """
    if args.model is not None:
        provider = load_model(args.model, app=args.app or "unknown", max_steps=args.max_steps)
    else:
        provider = load_provider(args.provider, goal)
    confirm_handler = cli_confirm_handler if sys.stdin.isatty() else None
    return AgentConfig(
        goal=goal,
        app=args.app,
        provider=provider,
        socket_path=args.socket,
        store_dir=Path(args.store),
        autonomy_level=AutonomyLevel(args.level),
        confirm_handler=confirm_handler,
        enable_visual_verification=args.verify,
        enable_vision=getattr(args, "vision", True),
        # OBSERVE precondition: only a *user-named* app on a *real* backend is
        # activated (the simulated backend never touches the host — Law 1). An
        # auto-discovered app is never activated: discovery already names the
        # frontmost app, and activating it again would be a no-op at best.
        activate_app_on_start=activate_named_app and args.app is not None,
        # Ctrl-C at any moment reclaims control (Law 5 fail-safe); polled live
        # between steps via the signal predicate.
        kill_switch=KillSwitch(monitor=None, signal_predicate=install_sigint_catcher()),
        max_steps=args.max_steps,
        enable_planning=getattr(args, "plan", False),
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
    if (
        args.real
        and args.model is not None
        and getattr(args, "vision", True)
        and not args.verify
    ):
        # The user's real-run complaint: vision (screenshot to the VLM) is on
        # but --verify (pre/post pixel diff) is off, so a click that misses
        # its target is never caught — the agent keeps acting on a wrong
        # assumption. These are independent switches; say so loudly.
        print(
            "warning: --vision is on but --verify is off: the agent sees the "
            "screen but actions are NOT pixel-verified. A click that lands on "
            "the wrong element goes unnoticed. Pass --verify with --real to "
            "enable closed-loop verification.",
            file=sys.stderr,
        )
    driver_process: subprocess.Popen[bytes] | None = None
    try:
        # When a driver binary is given we always spawn it (stale sockets are
        # cleared first); only without --driver do we attach to a running one.
        if args.driver is not None:
            driver_process = spawn_driver(args.driver, args.socket, real=args.real)
        # Remember whether the user *named* an app: only a named app on a
        # real backend is brought to the front before the run (see
        # build_config).
        named_app = args.app is not None
        # OBSERVE before DECIDE: if no app was named, discover the frontmost
        # one from the driver so the provider (and its scaffold prompt) names
        # the real app from the first turn.
        if args.app is None:
            args.app = discover_app(args.socket)
        config = build_config(
            args, goal=args.goal, activate_named_app=named_app and args.real
        )
        result = Agent(config).run()

        print(f"goal        : {config.goal}")
        print(f"app         : {result.app}")
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
