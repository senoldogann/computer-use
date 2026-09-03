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
import random
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Final, cast

from computeruse.agent import Agent, AgentConfig
from computeruse.autonomous import (
    DEFAULT_IDLE_SECONDS as AUTONOMOUS_IDLE_SECONDS,
)
from computeruse.autonomous import (
    DEFAULT_REST_SECONDS as AUTONOMOUS_REST_SECONDS,
)
from computeruse.autonomous import (
    GoalProposal,
    MachineActivity,
    SessionLimits,
    propose_goal,
    run_autonomously,
)
from computeruse.memory.episodic import EpisodicStore
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
from computeruse.orchestrator.mission import (
    DEFAULT_MAX_ATTEMPTS,
    MissionStore,
    mission_blocked,
    mission_finished,
    mission_started,
    mission_unblocked,
    new_mission,
    remaining_goal,
    resumable,
)
from computeruse.orchestrator.planner import GoalPlan
from computeruse.orchestrator.prompts import completion_auditor, scaffolded_provider
from computeruse.orchestrator.report import (
    UsageRecord,
    UsageStore,
    period_ending,
    render,
    summarize,
)
from computeruse.orchestrator.schemas import (
    AgentTurn,
    Finish,
    MouseClick,
    action_from_payload,
)
from computeruse.orchestrator.supervisor import supervisor_for
from computeruse.providers.openai import (
    DEFAULT_MODEL,
    ModelCallStats,
    OpenAIError,
    TokenPrice,
    call_cost_usd,
    openai_model,
    price_for,
)
from computeruse.security.approvals import (
    ApprovalQueue,
    ApprovalRequest,
    ApprovalRequiredError,
    approval_request_for,
    goals_awaiting_decision,
    now_utc,
    pending_requests,
)
from computeruse.security.autonomy import (
    AutonomyLevel,
    PermissionConfirmationRequired,
    PermissionDeniedError,
    classify_risk,
)
from computeruse.security.grants import (
    ANY as GRANT_ANY,
)
from computeruse.security.grants import (
    GRANTABLE_VERBS,
    CapabilityGrant,
    GrantStore,
    action_verbs,
    active_grants,
    new_grant,
)
from computeruse.security.killswitch import KillSwitch, install_sigint_catcher
from computeruse.skills.registry import SkillRegistry
from computeruse.vision.apps import extract_goal_app, infer_target_app

LOGGER: Final = logging.getLogger(__name__)

DEFAULT_SOCKET = "/tmp/actuation-driver.sock"
DEFAULT_STORE = Path.home() / ".computeruse"
DEMO_PROVIDER: str = "demo"
OPENAI_PREFIX: str = "openai"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="computeruse",
        description="Autonomous physical computer-use agent (macOS).",
    )
    parser.add_argument(
        "--goal",
        default=None,
        help="The task to accomplish. Required unless --autonomous is given, in "
        "which case the agent chooses its own work from memory.",
    )
    parser.add_argument(
        "--autonomous",
        type=int,
        default=None,
        metavar="RUNS",
        help="Work unattended: wait for the machine to be free, choose a goal "
        "from memory, and do it, up to RUNS times. Requires at least one hard "
        "budget (--deadline-seconds, --max-tokens or --max-cost): an unattended "
        "process without a bound is not autonomy, it is a leak.",
    )
    parser.add_argument(
        "--idle-seconds",
        type=float,
        default=AUTONOMOUS_IDLE_SECONDS,
        help="How still the machine must be before the agent treats it as free "
        f"(default: {AUTONOMOUS_IDLE_SECONDS:.0f}).",
    )
    parser.add_argument(
        "--rest-seconds",
        type=float,
        default=AUTONOMOUS_REST_SECONDS,
        help="How long to wait between unattended runs "
        f"(default: {AUTONOMOUS_REST_SECONDS:.0f}).",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Print what the agent did while nobody was watching: what it "
        "finished, what it lost, what is waiting for your decision, what "
        "authority it used and what it spent. Reads the store; runs nothing.",
    )
    parser.add_argument(
        "--since-hours",
        type=float,
        default=24.0,
        help="How far back --report looks, in hours (default: 24). Open items "
        "— paused missions, unanswered questions — are always shown whatever "
        "the window.",
    )
    parser.add_argument(
        "--grants",
        action="store_true",
        help="List your standing capability grants — the destructive actions "
        "the agent may take without asking, and what bounds each one. Reads "
        "the store; runs nothing.",
    )
    parser.add_argument(
        "--grant",
        default=None,
        metavar="VERB",
        help="Delegate authority over one family of destructive action "
        f"({', '.join(sorted(GRANTABLE_VERBS))}) in advance, so an unattended "
        "run need not stop and ask. Bounded by --grant-app, --grant-target, "
        "--grant-uses and --grant-hours.",
    )
    parser.add_argument(
        "--grant-app",
        default=None,
        metavar="APP",
        help="Application the grant applies within. Required: a grant with no "
        "application covers the whole machine, which has to be typed as "
        f"--grant-app '{GRANT_ANY}' deliberately.",
    )
    parser.add_argument(
        "--grant-target",
        default=GRANT_ANY,
        metavar="GLOB",
        help="Glob matched against the accessibility title of the control, e.g. "
        f"'Move to Trash' or '*.tmp' (default: {GRANT_ANY!r}, any control in "
        "the app).",
    )
    parser.add_argument(
        "--grant-uses",
        type=int,
        default=1,
        help="How many times the grant may be used before it is spent "
        "(default: 1).",
    )
    parser.add_argument(
        "--grant-hours",
        type=float,
        default=24.0,
        help="How long the grant lives, in hours (default: 24).",
    )
    parser.add_argument(
        "--grant-note",
        default=None,
        metavar="TEXT",
        help="Why you granted it, in your words. Required: a list of standing "
        "permissions nobody can explain is one nobody will audit.",
    )
    parser.add_argument(
        "--revoke",
        default=None,
        metavar="GRANT_ID",
        help="Delete a standing capability grant.",
    )
    parser.add_argument(
        "--always",
        action="store_true",
        help="With --approve: also mint a capability grant from the parked "
        "action, so the same question is not asked again. Scoped to that "
        "action's app and control unless you widen it with the --grant-* flags.",
    )
    parser.add_argument(
        "--approvals",
        action="store_true",
        help="List the actions an unattended run parked for your decision, and "
        "the missions waiting on them. Reads the store; runs nothing.",
    )
    parser.add_argument(
        "--approve",
        default=None,
        metavar="REQUEST_ID",
        help="Approve a parked action and return its mission to the queue. The "
        "action is NOT performed now — the next run reaches it with your answer "
        "on record.",
    )
    parser.add_argument(
        "--deny",
        default=None,
        metavar="REQUEST_ID",
        help="Refuse a parked action. Its mission returns to the queue so the "
        "rest of the work can proceed without it.",
    )
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
        default=AutonomyLevel.FULL.value,
        help="Autonomy level 0-3 (default: 3 = full). Level 3 still asks "
        "about destructive actions unless --yes is given.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Trust mode: auto-approve every CONFIRM without prompting, so "
        "the agent runs uninterrupted like a human operator. BLOCK still "
        "blocks, and the kill-switch (Cmd+Shift+Escape / Ctrl-C / shake), "
        "budgets, verification and stuck-guard keep running. Every "
        "auto-approval is logged. This is delegation in advance — use it "
        "only on a machine you own.",
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help="Act on elements through the accessibility API instead of moving "
        "the cursor, so the agent can work in an app you keep in the background "
        "without stealing focus or the pointer. Falls back to an ordinary click "
        "wherever an element declines.",
    )
    parser.add_argument(
        "--mcp",
        action="store_true",
        help="Connect the MCP servers declared in ~/.computeruse/mcp.json and "
        "lend the agent their tools. Off by default: these are other people's "
        "programs, started as subprocesses.",
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
        "them (a finish marks the current sub-goal done); progress is saved "
        "to the mission store as it happens and checkpoints are written to "
        "--store/checkpoints. Resume with --resume <plan-id>; missions "
        "resume via remaining_goal.",
    )
    parser.add_argument(
        "--resume",
        default=None,
        metavar="PLAN_ID",
        help="Resume a previous --plan run from its checkpoint file "
        "(--store/checkpoints/<PLAN_ID>.json) instead of starting over.",
    )
    parser.add_argument(
        "--display",
        type=int,
        default=0,
        help="Which display to observe and act on (0 = main display). The "
        "coordinate bounds gate is evaluated against this display's own "
        "rectangle, so a click can never land on a different screen.",
    )
    parser.add_argument(
        "--marks",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Outline the accessibility elements on the screenshot the model "
        "sees (Set-of-Marks), so a numbered element in the AX list and a "
        "region on screen are visibly the same thing. Selecting a target by "
        "mark works either way; this only controls the drawing.",
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
    background: bool = False,
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
        provider=scaffolded_provider(
            model, app=app, max_steps=max_steps, background=background
        ),
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


def cli_confirm_handler(turn: AgentTurn, target_label: str | None) -> bool:
    """Prompt the user on stderr/stdin for actions that require confirmation.

    Reads exactly one line from stdin so the channel works both for an
    interactive terminal (typing y/N) and for the menu launcher's piped
    channel (the panel's Approve/Deny buttons write the answer — M6). Unlike
    ``input()``, ``readline()`` does not echo a prompt to stdout, which would
    leak into the panel's log stream. EOF (pipe closed / stopped run) is a
    denial — fail-closed (Law 5).
    """
    print(
        f"\nCONFIRMATION REQUIRED: Agent wants to perform [{turn.action.type}] for goal: {turn.sub_goal!r}",
        file=sys.stderr,
    )
    print(f"   Payload: {turn.action.model_dump(exclude_none=True)}", file=sys.stderr)
    if target_label:
        # What the *screen* says the click will hit, which is the fact a person
        # is actually being asked about.
        print(f"   Target: {target_label}", file=sys.stderr)
    try:
        line = sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        return False
    return line.strip().lower() in ("y", "yes")


def auto_confirm_handler(turn: AgentTurn, target_label: str | None) -> bool:
    """Trust-mode confirmation: approve without prompting, but log it.

    Used only when the operator passed --yes. The approval is recorded on
    stderr (and therefore in the panel log + trace) so an unattended run
    stays auditable: what was auto-approved, what it targeted, and why the
    guard had asked. Never used at Level 0 — the guard BLOCKs there before
    any handler is consulted.
    """
    print(
        f"trust mode (--yes): auto-approved [{turn.action.type}] for "
        f"{turn.sub_goal!r}"
        + (f" on {target_label!r}" if target_label else "")
        + f" payload={turn.action.model_dump(exclude_none=True)}",
        file=sys.stderr,
    )
    return True


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
    driver_recover: Callable[[], None] | None = None,
    parked_confirm_handler: Callable[[AgentTurn, str | None], bool] | None = None,
    on_plan_progress: Callable[[GoalPlan], None] | None = None,
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
            background=getattr(args, "background", False),
            stats_sink=stats_sink,
        )
        provider = binding.provider
        # Only a real model can audit its own completion claim against the
        # screen; a scripted provider's "success" is the test's own fixture
        # and must be taken at face value.
        completion_check = binding.completion_check
    else:
        provider = load_provider(args.provider, goal)
    # An unattended session never asks: there is nobody to ask. Reaching the
    # interactive handler there was the worst of the three possible endings —
    # ``cli_confirm_handler`` blocks on ``stdin.readline()``, and launched from
    # a terminal (``isatty()`` is true even when the human has gone home) that
    # is a session frozen mid-task, holding the machine, until someone returns.
    # ``parking_confirm_handler`` writes the question down and ends the run
    # instead, so the work is paused rather than hung or silently lost.
    # Trust mode (--yes) overrides both: the operator explicitly asked for
    # uninterrupted autonomy, so CONFIRM is auto-approved with a log line.
    trust_mode = bool(getattr(args, "yes", False))
    if trust_mode:
        confirm_handler = auto_confirm_handler
    else:
        confirm_handler = (
            parked_confirm_handler
            if parked_confirm_handler is not None
            else (
                cli_confirm_handler
                if sys.stdin.isatty() or os.environ.get("COMPUTERUSE_MENU") == "1"
                else None
            )
        )
    return AgentConfig(
        goal=goal,
        app=args.app,
        provider=provider,
        socket_path=args.socket,
        store_dir=Path(args.store),
        autonomy_level=AutonomyLevel(args.level),
        confirm_handler=confirm_handler,
        auto_approve=trust_mode,
        enable_visual_verification=resolve_verify(args),
        enable_vision=getattr(args, "vision", True),
        enable_set_of_marks=getattr(args, "marks", True),
        display_id=getattr(args, "display", 0),
        background_actuation=getattr(args, "background", False),
        enable_mcp=getattr(args, "mcp", False),
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
        driver_recover=driver_recover,
        on_plan_progress=on_plan_progress,
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


def _run_autonomous_session(
    args: argparse.Namespace, *, driver_recover: Callable[[], None] | None
) -> int:
    """Work unattended until the session's bounds are reached.

    The agent's own memory is what it works from, so the stores are opened once
    here and consulted before each run rather than snapshotted at the start:
    a run that distils a skill or records a failure should change what the next
    goal is, which is the entire point of doing this repeatedly.
    """
    store = Path(args.store).expanduser() if args.store else DEFAULT_STORE
    skills = SkillRegistry(store / "skills")
    episodes = EpisodicStore(store / "episodes")
    missions = MissionStore(store / "missions")
    approvals = ApprovalQueue(store / "approvals")
    rng = random.Random()
    attempted_goals: list[str] = []
    budget = RunBudget(
        deadline_seconds=args.deadline_seconds,
        max_tokens=args.max_tokens,
        max_cost_usd=args.max_cost,
    )

    def observe() -> MachineActivity:
        with ActuationClient(args.socket, connect_retries=2) as client:
            window = client.focused_window()
            idle = client.idle_seconds()
        return MachineActivity(
            cursor=(window.cursor_x, window.cursor_y),
            frontmost=window.app_name,
            idle_seconds=idle,
        )

    # Usage accumulates across the whole session, not per run. The flags read
    # as the session's ceiling — main() refuses to start an unattended session
    # without one, on the grounds that a process nobody is watching needs a
    # bound — so charging each run the full amount would let `--autonomous 10
    # --max-cost 5` spend fifty dollars against a limit the user wrote as five.
    session_started_at = time.monotonic()
    tokens = {"total": 0}
    cost = {"usd": 0.0}

    def stats_sink(call: object) -> None:
        tokens["total"] += int(getattr(call, "total_tokens", 0) or 0)
        cost["usd"] += float(getattr(call, "cost_usd", 0.0) or 0.0)

    def usage_since(
        mark: tuple[int, float, float]
    ) -> tuple[int, float, float]:
        """What has been spent since ``mark`` (tokens, dollars, seconds).

        The session's counters are cumulative because the *budget* is the
        session's, so a per-run record has to be a delta — charging each run
        the session total would make a ten-run night look like ten expensive
        runs instead of ten cheap ones.
        """
        return (
            tokens["total"] - mark[0],
            cost["usd"] - mark[1],
            time.monotonic() - mark[2],
        )

    def budget_guard() -> None:
        reason = budget_verdict(
            budget,
            RunUsage(
                elapsed_seconds=time.monotonic() - session_started_at,
                total_tokens=tokens["total"],
                cost_usd=cost["usd"],
            ),
        )
        if reason is not None:
            raise BudgetExceededError(reason)

    def execute(proposal: GoalProposal) -> None:
        attempted_goals.append(proposal.goal)
        mark = (tokens["total"], cost["usd"], time.monotonic())
        # The mission is opened *before* the run, so a session killed
        # mid-action still leaves a record that this work was started and how
        # far it got. Its attempt is spent here for the same reason.
        mission = mission_started(
            new_mission(
                goal=proposal.goal, app=proposal.app, plan=None, now=now_utc()
            ),
            now_utc(),
        )
        missions.save(mission)

        # The plan as of the last sub-goal transition. Held here because the
        # blocked path needs it *after* an exception, when the result object
        # that would otherwise carry it does not exist.
        progress: dict[str, GoalPlan | None] = {"plan": None}

        def record_progress(plan: GoalPlan) -> None:
            progress["plan"] = plan
            missions.save(mission.model_copy(update={"plan": plan}))

        def park(turn: AgentTurn, target_label: str | None) -> bool:
            """Write the question down and stop, instead of asking nobody."""
            request = approval_request_for(
                turn,
                goal=proposal.goal,
                mission_id=mission.mission_id,
                target_label=target_label,
                risk=classify_risk(turn, target_label=target_label).value,
                now=now_utc(),
            )
            approvals.submit(request)
            raise ApprovalRequiredError(request=request)

        config = build_config(
            args,
            goal=proposal.goal,
            activate_named_app=False,
            app_inferred=proposal.app is not None,
            stats_sink=stats_sink,
            budget_guard=None if budget.is_unset else budget_guard,
            driver_recover=driver_recover,
            parked_confirm_handler=park,
            on_plan_progress=record_progress,
        )
        if proposal.app is not None:
            config = replace(config, app=proposal.app)
        def record(run_id: str, outcome: str, steps: int) -> None:
            spent_tokens, spent_cost, spent_seconds = usage_since(mark)
            _record_usage(
                store,
                run_id=run_id,
                goal=proposal.goal,
                app=proposal.app or "unknown",
                outcome=outcome,
                steps=steps,
                tokens=spent_tokens,
                cost_usd=spent_cost,
                elapsed_seconds=spent_seconds,
            )

        try:
            result = Agent(config).run()
        except ApprovalRequiredError as exc:
            # Parked, not failed: the attempt is refunded and the mission waits
            # for a person rather than being retried into the same question.
            missions.save(
                mission_blocked(
                    mission,
                    plan=progress["plan"],
                    reason=str(exc),
                    approval_id=exc.request.request_id,
                    now=now_utc(),
                )
            )
            record(f"parked-{exc.request.request_id}", "blocked", 0)
            print(
                f"autonomous  : {proposal.goal!r} -> parked for approval "
                f"({exc.request.request_id})"
            )
            return
        except Exception:
            missions.save(
                mission_finished(
                    mission, plan=progress["plan"], succeeded=False, now=now_utc()
                )
            )
            record(f"unfinished-{mission.mission_id}", "failure", 0)
            raise
        missions.save(
            mission_finished(
                mission, plan=result.state.plan, succeeded=True, now=now_utc()
            )
        )
        record(result.run_id, "success", len(result.state.completed_steps))
        print(f"autonomous  : {proposal.goal!r} -> {len(result.state.completed_steps)} steps")

    def propose() -> GoalProposal | None:
        """Unfinished work first, then something new.

        A mission left half-done by a killed run is the most concrete thing
        memory holds — more concrete than a failed skill, because it is a task
        that was actually started — and resuming it is what makes work survive
        the session that began it. ``remaining_goal`` hands over what is left,
        never the original goal: re-running a completed sub-goal on a physical
        host is not merely wasteful, it repeats whatever that step did.
        """
        # A parked run is still recorded as a failed episode (its work is worth
        # keeping), and that record is what ``propose_goal`` reads — so without
        # this the next run re-proposes the goal it just parked and asks the
        # same question again. Its mission is already held back; this closes
        # the same hole in the episode channel.
        waiting = goals_awaiting_decision(approvals.requests())
        open_work = resumable(
            missions.missions(), max_attempts=DEFAULT_MAX_ATTEMPTS
        )
        for mission in open_work:
            if mission.goal in waiting:
                continue
            return GoalProposal(
                goal=remaining_goal(mission),
                app=mission.app,
                reason=(
                    f"mission {mission.mission_id} was started and never "
                    f"finished ({mission.attempts} attempt(s) so far)"
                ),
            )
        proposal = propose_goal(skills, episodes, rng=rng)
        if proposal is not None and proposal.goal in waiting:
            LOGGER.info(
                "autonomous: %r is already waiting on a decision; nothing else to do",
                proposal.goal,
            )
            return None
        return proposal

    done = run_autonomously(
        SessionLimits(
            max_runs=args.autonomous,
            idle_seconds=args.idle_seconds,
            rest_seconds=args.rest_seconds,
        ),
        observe=observe,
        propose=propose,
        execute=execute,
        stop=lambda: False,
    )
    print(f"autonomous  : {done} run(s) attempted")
    for goal in attempted_goals:
        print(f"  - {goal}")
    parked = pending_requests(approvals.requests())
    if parked:
        print(f"awaiting you: {len(parked)} action(s) need a decision")
        for request in parked:
            target = f" on {request.target_label!r}" if request.target_label else ""
            print(f"  - [{request.request_id}] {request.action_type}{target} — {request.sub_goal}")
        print("  review with: computeruse --approvals")
    return 0


def _grant_from_request(
    args: argparse.Namespace, request: ApprovalRequest, store: Path
) -> CapabilityGrant | None:
    """Mint a grant covering the action a human just approved.

    Returns ``None`` when the parked action carries no destructive verb to
    delegate, which can happen: an action reaches the queue because the *guard*
    said CONFIRM, and a routine-marker confirmation ("Save", "Close") is not a
    family anyone can be granted. Saying so beats writing a grant that matches
    nothing.
    """
    action = action_from_payload(request.action)
    if action is None:
        return None
    verbs = action_verbs(
        action, sub_goal=request.sub_goal, target_label=request.target_label
    )
    if not verbs:
        return None
    now = now_utc()
    grant = new_grant(
        # The narrowest family the action fell into, so "delete and send"
        # delegates one of them rather than silently both.
        verb=min(verbs),
        app=args.grant_app or args.app or GRANT_ANY,
        target_pattern=args.grant_target
        if args.grant_target != GRANT_ANY
        else (request.target_label or GRANT_ANY),
        max_invocations=args.grant_uses,
        expires_at=now + timedelta(hours=args.grant_hours),
        note=args.grant_note or f"approved once for: {request.sub_goal}",
        now=now,
    )
    GrantStore(store / "grants").save(grant)
    return grant


def _record_usage(
    store: Path,
    *,
    run_id: str,
    goal: str,
    app: str,
    outcome: str,
    steps: int,
    tokens: int,
    cost_usd: float,
    elapsed_seconds: float,
) -> None:
    """Write what a run consumed, whatever ending it had.

    Best effort by contract: a run that did its work and then failed to write
    its own receipt should not report failure for that reason, so a store that
    cannot be written is logged and skipped. The counters are otherwise lost
    with the terminal — they only ever existed in this process.
    """
    try:
        UsageStore(store / "usage").record(
            UsageRecord(
                run_id=run_id,
                goal=goal,
                app=app,
                outcome=outcome,
                steps=steps,
                total_tokens=tokens,
                cost_usd=cost_usd,
                elapsed_seconds=elapsed_seconds,
                recorded_at=now_utc(),
            )
        )
    except (OSError, ValueError) as exc:
        LOGGER.warning("could not record usage for run %s: %s", run_id, exc)


def _print_report(args: argparse.Namespace) -> int:
    """Read the five stores together and print what happened (I/O + pure render)."""
    store = Path(args.store).expanduser() if args.store else DEFAULT_STORE
    report = summarize(
        episodes=tuple(EpisodicStore(store / "episodes").episodes()),
        usage=UsageStore(store / "usage").records(),
        missions=MissionStore(store / "missions").missions(),
        approvals=ApprovalQueue(store / "approvals").requests(),
        grants=GrantStore(store / "grants").grants(),
        period=period_ending(now_utc(), hours=args.since_hours),
    )
    print(render(report), end="")
    return 0


def _review_grants(args: argparse.Namespace) -> int:
    """List, mint, or revoke standing capability grants (reads/writes the store).

    Minting is deliberately verbose. Every bound — the app, the controls, the
    count, the expiry — has to be typed or defaulted on purpose, because the
    whole safety argument for delegating authority in advance is that the
    delegation is *narrow*. A grant nobody had to think about is one nobody
    remembers giving.
    """
    store = Path(args.store).expanduser() if args.store else DEFAULT_STORE
    grants = GrantStore(store / "grants")
    now = now_utc()

    if args.revoke is not None:
        try:
            grants.revoke(args.revoke)
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"{args.revoke}: revoked")
        return 0

    if args.grant is not None:
        if args.grant_app is None:
            print(
                "error: --grant needs --grant-app. A grant with no application "
                f"covers the whole machine; type --grant-app {GRANT_ANY!r} if "
                "that is really what you mean.",
                file=sys.stderr,
            )
            return 2
        if not args.grant_note:
            print(
                "error: --grant needs --grant-note saying why. A standing "
                "permission you cannot explain later is one you cannot audit.",
                file=sys.stderr,
            )
            return 2
        try:
            grant = new_grant(
                verb=args.grant,
                app=args.grant_app,
                target_pattern=args.grant_target,
                max_invocations=args.grant_uses,
                expires_at=now + timedelta(hours=args.grant_hours),
                note=args.grant_note,
                now=now,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        grants.save(grant)
        print(f"granted {grant.grant_id}")
        print(f"  {grant.verb} in {grant.app}, controls matching {grant.target_pattern!r}")
        print(f"  {grant.max_invocations} use(s), expires {grant.expires_at.isoformat(timespec='seconds')}")
        return 0

    live = active_grants(grants.grants(), now)
    if not live:
        print("no standing grants; every destructive action will ask")
        return 0
    print(f"{len(live)} standing grant(s):\n")
    for grant in live:
        print(f"  [{grant.grant_id}]")
        print(f"    may      : {grant.verb} in {grant.app}")
        print(f"    controls : {grant.target_pattern}")
        print(f"    left     : {grant.remaining} of {grant.max_invocations}")
        print(f"    expires  : {grant.expires_at.isoformat(timespec='seconds')}")
        print(f"    because  : {grant.note}")
        print()
    print("revoke with: computeruse --revoke <id>")
    return 0


def _review_approvals(args: argparse.Namespace) -> int:
    """List parked actions, or record a decision on one (reads/writes the store).

    Deliberately does not perform the approved action. An approval is a
    recorded answer, not a remote control: the next run reaches that step
    itself, with the guard consulting what the human said. Performing it here
    would act on a screen nobody has looked at since the question was asked.
    """
    store = Path(args.store).expanduser() if args.store else DEFAULT_STORE
    approvals = ApprovalQueue(store / "approvals")
    missions = MissionStore(store / "missions")

    decision_id = args.approve or args.deny
    if decision_id is not None:
        try:
            answered = approvals.resolve(
                decision_id, approved=args.approve is not None, now=now_utc()
            )
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"{answered.request_id}: {answered.decision}")
        if args.always and args.approve is not None:
            # "Yes, and stop asking" — the natural moment to delegate, because
            # the person is looking at exactly what they are delegating. The
            # grant is scoped to the action they just read: its verb family,
            # its app, and the control it names. Widening that is possible but
            # has to be typed.
            grant = _grant_from_request(args, answered, store)
            if grant is None:
                print(
                    "  (no destructive verb to delegate; nothing granted)",
                    file=sys.stderr,
                )
            else:
                print(f"  granted {grant.grant_id}: {grant.verb} in {grant.app}, "
                      f"{grant.max_invocations} use(s), controls matching "
                      f"{grant.target_pattern!r}")
        if answered.mission_id is not None:
            try:
                mission = missions.load(answered.mission_id)
            except KeyError:
                # The queue outlives the mission store's contents; a decision
                # is still worth recording even when its mission is gone.
                print(f"  (mission {answered.mission_id} no longer in the store)")
                return 0
            missions.save(mission_unblocked(mission, now_utc()))
            print(f"  mission {mission.mission_id} is back in the queue")
        return 0

    parked = pending_requests(approvals.requests())
    if not parked:
        print("no actions are waiting for a decision")
        return 0
    print(f"{len(parked)} action(s) waiting for a decision:\n")
    for request in parked:
        print(f"  [{request.request_id}]")
        print(f"    goal     : {request.goal}")
        print(f"    step     : {request.sub_goal}")
        print(f"    action   : {request.action_type} {request.action}")
        if request.target_label:
            print(f"    target   : {request.target_label}")
        print(f"    risk     : {request.risk}")
        print(f"    parked   : {request.created_at.isoformat(timespec='seconds')}")
        print()
    print("approve with: computeruse --approve <id>   (or --deny <id>)")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    # Live step visibility: the runner logs every executed physical action at
    # INFO, and a real run takes seconds per LLM decision — without this the
    # terminal stays silent mid-run and the user sees "Chrome opened, nothing
    # happens". Stream the runner's lines to stderr so the run is observable
    # while the final summary block still lands on stdout at the end.
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    # Reviewing the queue is a store operation, not a run: it needs no driver,
    # no model and no goal, so it is dispatched before every check below.
    if args.report:
        return _print_report(args)
    if args.grants or args.grant is not None or args.revoke is not None:
        return _review_grants(args)
    if args.approvals or args.approve is not None or args.deny is not None:
        return _review_approvals(args)
    if args.autonomous is None and not args.goal:
        print("error: --goal is required unless --autonomous is given", file=sys.stderr)
        return 2
    if args.autonomous is not None:
        if args.autonomous < 1:
            print("error: --autonomous needs a positive run count", file=sys.stderr)
            return 2
        # An unattended process without a bound is not autonomy, it is a leak,
        # and a bound reached by forgetting a flag is not a bound. The run
        # count alone is not enough: one run can spend indefinitely.
        if (
            args.deadline_seconds is None
            and args.max_tokens is None
            and args.max_cost is None
        ):
            print(
                "error: --autonomous requires at least one of --deadline-seconds, "
                "--max-tokens or --max-cost. Nobody is watching an unattended run, "
                "so its ceiling has to be set before it starts.",
                file=sys.stderr,
            )
            return 2
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
    # ADR-1 promises the driver can die without taking the run with it, and
    # that promise is only kept by something that brings it back. Supervision
    # exists exactly when we started the process: a driver we merely attached
    # to belongs to whoever launched it, and respawning it behind their back
    # would leave two drivers fighting over one socket.
    driver_recover: Callable[[], None] | None = None
    try:
        # When a driver binary is given we always spawn it (stale sockets are
        # cleared first); only without --driver do we attach to a running one.
        if args.driver is not None:
            driver_binary = args.driver
            driver_socket = args.socket
            driver_real = args.real

            def _spawn_driver_again() -> subprocess.Popen[bytes]:
                return spawn_driver(driver_binary, driver_socket, real=driver_real)

            driver_process = _spawn_driver_again()
            driver_recover = supervisor_for(
                _spawn_driver_again, driver_process
            ).ensure_alive
        # Autonomous app resolution: the goal may carry an explicit `[App
        # Name]` prefix, or name the target implicitly ("Excel'de aç",
        # "YouTube'da arat"). Resolve it here so the provider sees the
        # cleaned goal (no bracket wrapper) and the run can bring the right
        # app to the front without the user passing --app — autonomy by
        # design, not by configuration.
        # Unattended work chooses its own goal per run, so none of the
        # goal-shaped setup below applies to it. Dispatching here rather than
        # later is the point: running any of it on an absent goal is what
        # crashed the first attempt.
        if args.autonomous is not None:
            return _run_autonomous_session(args, driver_recover=driver_recover)

        if getattr(args, "resume", None) is not None:
            # AUT-01: checkpoints were written but never read. --resume loads
            # the plan and continues from the first pending sub-goal, so an
            # interrupted --plan run does not restart completed work.
            from computeruse.orchestrator.planner import SessionCheckpoint

            store = Path(args.store).expanduser() if args.store else DEFAULT_STORE
            checkpoint_path = store / "checkpoints" / f"{args.resume}.json"
            if not checkpoint_path.is_file():
                # Also accept a full filename or path the user copied.
                alt = Path(args.resume)
                checkpoint_path = alt if alt.is_file() else checkpoint_path
            try:
                checkpoint = SessionCheckpoint.load(checkpoint_path)
            except Exception as exc:
                print(f"error: cannot load checkpoint {args.resume!r}: {exc}", file=sys.stderr)
                return 2
            pending = [
                sg.description
                for sg in checkpoint.plan.sub_goals
                if sg.status in ("pending", "in_progress")
            ]
            if not pending:
                print(f"checkpoint {checkpoint.session_id}: plan already complete", file=sys.stderr)
                return 0
            # Continue with what is left, never the original goal: re-running
            # a completed sub-goal on a physical host repeats its side effects.
            args.goal = " then ".join(pending)
            print(
                f"resuming checkpoint {checkpoint.session_id}: "
                f"{len(pending)} sub-goal(s) left",
                file=sys.stderr,
            )

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
            driver_recover=driver_recover,
        )
        # Spend is recorded on *both* endings. A run that failed still cost
        # what it cost, and that is exactly the run someone wants the number
        # for; recording only successes would make the report's total a
        # comfortable fiction.
        try:
            result = Agent(config).run()
        except BaseException:
            _record_usage(
                Path(args.store),
                run_id=f"unfinished-{int(time.time())}",
                goal=config.goal,
                app=config.app or "unknown",
                outcome="failure",
                steps=0,
                tokens=run_tokens["total"],
                cost_usd=run_cost["usd"],
                elapsed_seconds=time.monotonic() - run_started_at,
            )
            raise
        _record_usage(
            Path(args.store),
            run_id=result.run_id,
            goal=config.goal,
            app=result.app,
            outcome="success",
            steps=len(result.state.completed_steps),
            tokens=run_tokens["total"],
            cost_usd=run_cost["usd"],
            elapsed_seconds=time.monotonic() - run_started_at,
        )

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
