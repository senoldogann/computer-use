"""OODA execution loop — the orchestration spine.

The loop follows Law 6's split *honestly*:

* ``decide_step`` is the pure core. It consumes an immutable
  :class:`WorkingState` and a :class:`AgentTurn` decision, and returns a
  routed :class:`StepOutcome` — but it never performs I/O. Routing (is this a
  physical action for the driver, an internal ``wait``, a ``finish``, or an
  invalid ``load_skill`` reaching the driver?) is a pure classification.
  The ORIENT pure helpers (``target_point_of``, ``verification_region``,
  ``visual_failure_diagnostics``) decide *what* and *where* to verify.

* :class:`OodaRunner` is the imperative shell. It owns the side effects:
  asking a provider for the next decision, sleeping for internal ``wait``s,
  dispatching physical actions to the driver, OBSERVING the screen before and
  after a verifiable action (``sensor``), and folding a failure back into
  state so the provider can steer around it (Law 2 self-correction — the
  driver may ACK a click that landed on nothing; only the pixels know).

The provider is a callable returning :class:`AgentTurn` rather than a hard-coded
LLM, so weak and strong models — or a deterministic fake in tests — all flow
through identical scaffolding.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from computeruse.orchestrator.planner import GoalPlan, advance_plan
from computeruse.orchestrator.schemas import (
    Action,
    ActivateApp,
    AgentTurn,
    ClipboardPaste,
    Finish,
    LoadSkill,
    MouseClick,
    MouseDrag,
    MouseScroll,
    PressHotkey,
    TypeText,
    Wait,
)
from computeruse.security.autonomy import (
    PermissionConfirmationRequired,
    PermissionDecision,
    PermissionDeniedError,
)
from computeruse.security.killswitch import KillSwitch
from computeruse.skills.distiller import Trajectory
from computeruse.skills.registry import RelevanceMatch
from computeruse.skills.schemas import SkillDefinition, SkillSummary
from computeruse.vision.capture import ScreenCapture, verify_capture_region
from computeruse.vision.coordinates import (
    CoordinateOutOfBoundsError,
    Point,
    Rect,
    Size,
)
from computeruse.vision.diff import ChangeKind, ChangeVerdict
from computeruse.vision.focus import FocusedWindow, window_summary

if TYPE_CHECKING:
    # Type-only import: memory.schemas imports orchestrator.schemas, whose
    # package __init__ eagerly imports this module — importing it at runtime
    # here would create a cycle (loop -> memory -> orchestrator -> loop).
    from computeruse.memory.schemas import EpisodeOutcome

LOGGER: Final = logging.getLogger(__name__)

# Physical action types that must go to the driver, versus those the loop
# handles internally. A frozenset keeps the routing decision a pure lookup.
_PHYSICAL_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "mouse_move",
        "mouse_click",
        "mouse_drag",
        "mouse_scroll",
        "type_text",
        "clipboard_paste",
        "press_hotkey",
        "activate_app",
    }
)
_INTERNAL_ACTIONS: Final[frozenset[str]] = frozenset({"wait", "load_skill", "finish"})

# Stuck-loop guard thresholds (Law 2): after 3 consecutive identical physical
# actions the provider receives a corrective hint; the action that would be
# the 5th repeat is never executed — the loop always terminates even against
# a degenerate model.
REPEAT_WARN_AFTER: Final[int] = 2
REPEAT_ABORT_AFTER: Final[int] = 3

# Physical actions whose identical repetition signals a stuck loop. mouse_move
# is deliberately excluded: re-positioning the cursor to the same point is a
# normal navigation pattern, not a lost-model signal.
_REPETITION_SENSITIVE: Final[frozenset[str]] = frozenset(
    {"mouse_click", "mouse_drag", "mouse_scroll", "type_text", "clipboard_paste", "press_hotkey"}
)

# Law 3.2 minimum relevance for mounting a skill (see registry.search scoring:
# an app-token hit scores +2, a tag hit +1). Below 2 the match is a single
# weak tag coincidence and must not steer the run.
SKILL_MOUNT_MIN_SCORE: Final[int] = 2


@dataclass(frozen=True)
class WorkingState:
    """Immutable rolling scratchpad (Law 4: working context).

    Every transition produces a new instance; nothing is mutated in place.
    """

    goal: str
    completed_steps: tuple[str, ...] = ()
    last_error: str | None = None
    step_index: int = 0
    # Law 4.2: compact app-knowledge strings the provider sees every turn
    # (e.g. "[Safari] shortcut.fullscreen: Ctrl+Cmd+F"). Kept as short strings,
    # never full entries, to respect the minimal working-context rule.
    knowledge: tuple[str, ...] = ()
    # Law 2 OBSERVE: the focused-window summary (e.g. "Safari — GitHub
    # (cursor 420,300)") as of the most recent probe; None when no probe is
    # configured or the probe failed. Refreshed before every decision.
    active_window: str | None = None
    # ADR-2 grounding: compact one-line summaries of the app's actionable AX
    # elements (e.g. 'Button "Reload" at (232,68) 44x24'), so the provider
    # picks real coordinates instead of hallucinating them. Empty when no
    # probe is configured or the probe failed. Refreshed before every decision.
    ui_elements: tuple[str, ...] = ()
    # Law 3.2: the skill definition mounted into active context (Stage 2 —
    # full instructions on demand). None until the RETRIEVE step mounts a
    # scan match or the provider explicitly emits ``load_skill``.
    skill: SkillDefinition | None = None
    # Multimodal visual perception: base64-encoded PNG of current display
    screenshot_b64: str | None = None
    # Hierarchical strategic plan (Phase 3): the decomposed sub-goal roadmap
    # the provider sees every turn. None when planning is disabled or the
    # goal needs no decomposition. Typed, never ``object`` (Law 6.2).
    plan: GoalPlan | None = None


Routing = Literal["physical", "internal_wait", "internal_skill", "finish"]


@dataclass(frozen=True)
class StepOutcome:
    """Pure result of one decision reduction — never performs I/O.

    ``step_label`` is what the shell appends to ``completed_steps`` *only after
    the action has physically succeeded* — the core never records a step as
    completed before it ran (F2: no state pollution on error).
    """

    state: WorkingState
    action: Action
    route: Routing
    step_label: str


def _route_for(action: Action) -> Routing:
    """Classify an action's routing constraint (pure function).

    Raises :class:`ValueError` for the one genuinely invalid case — an internal
    action that cannot legally touch the driver.
    """
    if action.type == "mouse_move":
        return "physical"
    if action.type == "mouse_click":
        return "physical"
    if action.type == "mouse_drag":
        return "physical"
    if action.type == "mouse_scroll":
        return "physical"
    if action.type == "type_text":
        return "physical"
    if action.type == "clipboard_paste":
        return "physical"
    if action.type == "press_hotkey":
        return "physical"
    if action.type == "activate_app":
        return "physical"
    if action.type == "wait":
        return "internal_wait"
    if action.type == "finish":
        return "finish"
    if action.type == "load_skill":
        return "internal_skill"
    raise ValueError(f"unrecognized action {action.type!r}")


def repetition_sensitive(action: Action) -> bool:
    """Whether an action participates in the stuck-loop repetition guard.

    Discrete physical intents (clicks, drags, scrolls, typing, hotkeys) are
    guard-sensitive: performing the identical one repeatedly with no progress
    is exactly what a lost model does. ``mouse_move`` is not — cursor
    positioning to the same point is ordinary navigation (pure).
    """
    return action.type in _REPETITION_SENSITIVE


def same_physical_action(left: Action, right: Action) -> bool:
    """True when two actions are payload-identical (type + every parameter).

    ``model_dump`` of the typed Pydantic action is the stable comparison key:
    two clicks are "the same" only when their coordinates, button, and click
    count all match — a click 2px away is a different action (pure).
    """
    return left.model_dump(exclude_none=True) == right.model_dump(exclude_none=True)


def repetition_diagnostic(action: Action, repeats: int) -> str:
    """The LLM-facing corrective hint for the stuck-loop guard (Law 2).

    Folded into ``last_error`` so the next provider turn steers away from the
    loop: repeating the identical action cannot achieve the goal, and the
    only two exits are a genuine change of action or a ``finish`` (pure).
    """
    payload = action.model_dump(exclude_none=True)
    base_hint = (
        f"action repetition detected: you have performed the identical action "
        f"{repeats} times in a row ({payload}) with no visible progress. "
        f"Repeating it will not achieve the goal. "
    )
    # Action-specific guidance: give the model a concrete recovery path
    # instead of a generic "do something different". A model stuck clicking
    # the same spot usually needs to scroll or try a URL navigation.
    if isinstance(action, MouseClick):
        return (
            base_hint
            + "The click target may be wrong, off-screen, or the page may be "
            "scrolled. Try: (a) scroll up/down with mouse_scroll to find the "
            "real target, (b) navigate via the URL bar instead of clicking, "
            "(c) press Escape to dismiss any overlay, or (d) emit finish if "
            "the goal is already complete."
        )
    return (
        base_hint
        + "If the goal is already complete, emit finish immediately; otherwise "
        "pick a genuinely different action (different target or different type)."
    )


def target_point_of(action: Action) -> Point | None:
    """Return the primary visual target of a verifiable action (pure)."""
    if isinstance(action, MouseClick):
        return Point(action.x, action.y)
    if isinstance(action, MouseDrag):
        return Point(action.end_x, action.end_y)
    return None


def action_verification_kind(action: Action) -> Literal["region", "full", "window", "none"]:
    """Select the least-assumptive verification strategy for an action.

    * ``region`` — a local pixel diff around the action's target point
      (clicks, drags, scrolls). Cheap and sufficient for element-level UI
      changes (button highlight, menu open, checkbox toggle).
    * ``full`` — a full-screen pixel diff. Used for actions that change
      the entire visible page (submitting a search with Return, navigating
      via a URL paste+Return, closing a modal with Escape). A local region
      diff would miss the transition because the change is page-wide.
    * ``window`` — a focused-window semantic check (activate_app).
    * ``none`` — no pixel verification possible (plain modifier hotkeys
      without a visible state change, or actions where only AXValue applies).
    """
    if isinstance(action, (MouseClick, MouseDrag, MouseScroll)):
        return "region"
    if isinstance(action, ActivateApp):
        return "window"
    if isinstance(action, PressHotkey):
        # Return/Enter submits forms, searches, and dialogs — the entire
        # page transitions. Escape closes modals/autocompletes — also
        # full-screen. Other hotkeys (Cmd+L, Cmd+C, Cmd+V) are setup steps
        # whose effect is verified by the *next* action's own pre-check
        # (e.g. the paste that follows Cmd+L is verified by AXValue).
        key = action.key.lower().strip()
        if key in ("return", "enter"):
            return "full"
        if key == "escape":
            return "full"
        return "none"
    return "none"


def verification_region(target: Point, *, size: float = 48.0) -> Rect:
    """A square region (logical points) centred on an action's target.

    Deliberately generous: a small UI response (button highlight, tooltip,
    dialog) lands inside even when the click coordinate is a few points off.
    Regions that extend past the screen edge are fine — :func:`crop_luma`
    clamps to the captured frame.
    """
    if size <= 0:
        raise ValueError(f"region size must be positive, got {size}")
    half = size / 2.0
    return Rect(Point(target.x - half, target.y - half), Size(size, size))


def visual_failure_diagnostics(
    action_type: str,
    target: Point,
    region: Rect,
    verdict: ChangeVerdict,
) -> str:
    """The LLM-facing diagnostics when a verified action shows no change.

    Kept pure so the message text is testable without a runner; the shell
    folds this into ``last_error`` and the next provider turn steers around it
    (Law 2 error injection).
    """
    return (
        f"visual verification failed: {action_type} at ({target.x:.0f},{target.y:.0f}) "
        f"produced no visible change in region {region} "
        f"(mean_abs={verdict.mean_abs_change:.3f}, "
        f"changed_fraction={verdict.changed_fraction:.3f}); the action likely did "
        "not land — re-check the coordinates and current UI state before retrying"
    )


def decide_step(state: WorkingState, decision: AgentTurn) -> StepOutcome:
    """Reduce one decision onto the state (pure).

    Produces the routed outcome plus the next working state. This never calls
    the provider, never sleeps, never writes a byte to the OS — all of that
    lives in :class:`OodaRunner`. Crucially, it does *not* add the step to
    ``completed_steps``: that happens in the shell after physical success, so
    a failed action never pollutes the completed list (F2).
    """
    action = decision.action
    route = _route_for(action)
    step_label = f"step_{state.step_index}:{action.type}"
    next_state = WorkingState(
        goal=state.goal,
        completed_steps=state.completed_steps,
        last_error=state.last_error,
        step_index=state.step_index + 1,
        knowledge=state.knowledge,
        active_window=state.active_window,
        ui_elements=state.ui_elements,
        skill=state.skill,
        screenshot_b64=state.screenshot_b64,
        # The strategic plan is part of the rolling context: a decision must
        # never drop the roadmap the provider is executing against (Law 4.3).
        plan=state.plan,
    )
    return StepOutcome(state=next_state, action=action, route=route, step_label=step_label)


@dataclass
class OodaRunner:
    """Imperative shell: drives the OODA loop with the real side effects.

    ``provider`` yields the next decision from a state; ``execute_physical``
    physically runs a driver action and raises on failure; internal ``wait``s
    sleep; ``finish`` ends the loop. ``max_steps`` bounds it so a degenerate
    provider cannot spin forever; ``kill_switch`` (Law 5) is polled before
    every step so a human can reclaim control at any moment — a trip surfaces
    as :class:`KillSwitchTripped`.

    ``skill_scan``/``skill_loader`` (Law 3 RETRIEVE) are the two-stage
    retrieval seam: ``skill_scan(goal)`` returns the ranked summary index
    (Stage 1), and the top *same-app* match is loaded via ``skill_loader(id)``
    and mounted into the provider's context (Stage 2). The provider can also
    swap the mounted skill explicitly with a ``load_skill`` action.

    ``guard`` (Law 5.1) is the VALIDATE step: given a proposed decision, it
    returns a :class:`PermissionDecision`. ``BLOCK`` and ``CONFIRM`` raise the
    typed errors before any physical action is dispatched.

    ``sensor`` is the *single* capture source (Law 2 OBSERVE): a callable
    returning a :class:`ScreenCapture`. Two flags decide how it is used:
    ``verify_enabled`` drives the ORIENT step (capture *before* and *after* a
    verifiable action, raising :class:`VisualVerificationFailedError` if the
    target region did not change — the driver may ACK a click that landed on
    nothing, and only the pixels know); ``vision_enabled`` drives the
    multimodal OBSERVE (the same frame, downscaled to logical resolution,
    feeds the provider's screenshot). One source, two consumers — the dual
    ``sensor``/``screen_sensor`` split is gone. Without a sensor the loop
    behaves exactly as before (backwards compatible).

    ``window_probe`` (Law 2 OBSERVE) is a callable returning a
    :class:`FocusedWindow`; ``ax_probe`` returns the compact UI-element
    summaries of the frontmost app (ADR-2: AX *generates* the coordinates the
    provider decides with). When provided, both are refreshed into every
    provider state before each decision (and into ``last_error`` recovery
    states), so the provider always knows what it is looking at and where the
    actionable elements are. A probe failure degrades to the previous context
    with a warning — perception is best-effort, never fatal.

    ``app`` names the target application and ``on_complete`` is the DISTILL
    step (Law 3/4): every terminal ``finish`` fires it with the executed
    trajectory (successful actions only — the finish action itself excluded)
    and the run outcome. The caller decides what to do (persist an episode,
    distill a skill); the runner stays decoupled from the memory/skill stores.
    """

    provider: Callable[[WorkingState], AgentTurn]
    execute_physical: Callable[[Action], None]
    # Law 3 RETRIEVE: Stage 1 (scored matches for a query — the relevance gate
    # reads ``RelevanceMatch.score``) + Stage 2 (load a skill id into its full
    # definition, which the loop mounts into context).
    skill_scan: (
        Callable[[str], tuple[RelevanceMatch, ...]]
        | Callable[[str], tuple[SkillSummary, ...]]
        | None
    ) = None
    skill_loader: Callable[[str], SkillDefinition] | None = None
    kill_switch: KillSwitch | None = None
    guard: Callable[[AgentTurn], PermissionDecision] | None = None
    confirm_handler: Callable[[AgentTurn], bool] | None = None
    sensor: Callable[[], ScreenCapture] | None = None
    # Whether ``sensor`` is used for ORIENT verification (pre/post action
    # pixel diff). Off = actions are executed but not pixel-verified.
    verify_enabled: bool = False
    # Whether ``sensor`` is used for the multimodal OBSERVE screenshot that
    # the provider sees each turn. Off = no screenshot is attached.
    vision_enabled: bool = False
    window_probe: Callable[[], FocusedWindow] | None = None
    ax_probe: Callable[[], tuple[str, ...]] | None = None
    # Semantic postcondition probe for typed/pasted text: returns the focused
    # text field's current AXValue, or None when not determinable. When set,
    # type_text/clipboard_paste are verified against it after actuation — a
    # non-empty value that lacks the expected text is a real miss (ADR-2 AX
    # as the state source). None means "skip verification" (insufficient
    # evidence must never be claimed as success).
    focused_text_value: Callable[[], str | None] | None = None
    # Optional cancellation-aware executor. The legacy executor remains the
    # public fallback; this seam lets long actions poll the kill switch without
    # changing existing callers.
    execute_physical_cancellable: Callable[[Action, Callable[[], bool]], None] | None = None
    app: str = "unknown"
    on_complete: Callable[[Trajectory, EpisodeOutcome], None] | None = None
    knowledge: tuple[str, ...] = ()
    max_steps: int = 100
    # Phase 3: the hierarchical plan the loop executes. When set, a ``finish``
    # marks the CURRENT sub-goal done and the loop advances to the next one
    # (via ``advance_plan``) instead of terminating; the run ends only when
    # the whole plan is complete. ``on_sub_goal_complete`` fires on every
    # transition so a caller can checkpoint the session (resumability).
    plan: GoalPlan | None = None
    on_sub_goal_complete: Callable[[GoalPlan], None] | None = None

    def __post_init__(self) -> None:
        # Trajectory of *successfully executed* actions in the current run.
        # Shell state, not a constructor arg; reset at every ``run()`` so a
        # runner never leaks history between goals.
        self._executed: list[Action] = []
        self._sub_goals: list[str] = []
        # The skill mounted by RETRIEVE in the current run (Law 3.2).
        self._skill: SkillDefinition | None = None
        # Best-effort perception warnings are logged once per run, then
        # demoted to debug: a permanently-failing probe (e.g. consent missing)
        # must not spam one line per step, but the first failure is still loud.
        self._window_probe_warned: bool = False
        self._ax_probe_warned: bool = False
        self._screenshot_warned: bool = False
            # Stuck-loop guard (Law 2): a single streak counter that combines
        # "same action" + "no screen progress". When the same physical action
        # is repeated AND the screen fingerprint is unchanged, the streak
        # grows. After 2 (warn) the model gets a corrective hint; after 3
        # (abort) the run terminates — a lost agent must never click forever.
        # A different action OR a screen change resets the streak to 0.
        self._last_physical: Action | None = None
        self._stuck_streak: int = 0
        self._last_progress_fingerprint: tuple[str | None, tuple[str, ...], str | None] | None = None
        # Screenshot hash cache to avoid expensive redundant PNG re-encodings
        self._last_capture_hash: str | None = None
        self._last_screenshot_b64: str | None = None
        self._last_error: str | None = None

    def run(self, goal: str) -> WorkingState:
        state = WorkingState(goal=goal, knowledge=self.knowledge, plan=self.plan)
        self._executed = []
        self._sub_goals = []
        self._skill = None
        self._window_probe_warned = False
        self._ax_probe_warned = False
        self._screenshot_warned = False
        self._last_physical = None
        self._stuck_streak = 0
        self._last_progress_fingerprint = None
        self._last_capture_hash = None
        self._last_screenshot_b64 = None
        self._last_error = None
        for _ in range(self.max_steps):
            # Law 5: yield control to the human the instant a kill-switch trips,
            # even mid-workflow — never start a fresh action against a takeover.
            if self.kill_switch is not None and self.kill_switch.tripped():
                raise KillSwitchTripped(
                    f"human reclaimed control at step {state.step_index} for goal={goal!r}"
                )

            # Law 2 OBSERVE: refresh the focused-window and UI-element context
            # before the provider decides, so the decision is grounded in what
            # the host currently shows (ADR-2: AX/perception feeds generation).
            state = self._observe(state)
            # Law 3 RETRIEVE: scan the skill index and mount a relevant skill
            # into the context the provider decides against (two-stage).
            state = self._retrieve(state)
            if state.active_window or state.ui_elements:
                LOGGER.info(
                    "ooda observe: window=%r, ax_elements=%d",
                    state.active_window or "unknown",
                    len(state.ui_elements),
                )
            # Anti-hallucination guard: if the screen sensor is configured
            # (real vision mode) but returned no screenshot, inject a warning
            # so the model knows it is flying blind.
            # Only fires after step 0 (the first probe may lag).
            has_vision_configured = self.sensor is not None and self.vision_enabled
            if (
                has_vision_configured
                and not state.screenshot_b64
                and state.step_index > 0
            ):
                blind_warning = (
                    "perception unavailable: live screenshot is unavailable. "
                    "Grant Screen Recording consent (System Settings > Privacy & Security > "
                    "Screen & System Audio Recording) or emit finish if the goal cannot be "
                    "accomplished without visual grounding."
                )
                if state.last_error is None:
                    state = WorkingState(
                        goal=state.goal,
                        completed_steps=state.completed_steps,
                        last_error=blind_warning,
                        step_index=state.step_index,
                        knowledge=state.knowledge,
                        active_window=state.active_window,
                        ui_elements=state.ui_elements,
                        skill=state.skill,
                        screenshot_b64=state.screenshot_b64,
                        plan=state.plan,
                    )
            decision = self.provider(state)
            if decision.thought:
                LOGGER.info("ooda thought: %s", decision.thought)
            if decision.sub_goal:
                LOGGER.info("ooda sub_goal: %s", decision.sub_goal)
            # Law 5.1 VALIDATE: the permission guard sees every proposed action
            # *before* it becomes physical, and can hard-stop a dangerous move.
            self._validate(decision)
            outcome = decide_step(state, decision)
            # Pure projection only advances step_index; completed_steps is still
            # the pre-action list, so a failure below cannot pollute it (F2).
            state = outcome.state

            # Law 2 stuck-loop abort: refuse to execute the action that would
            # be the 3rd consecutive identical repeat with no screen progress.
            # The corrective hint already went into last_error after the 2nd,
            # so a model that still repeats is not going to recover by clicking
            # again. A screen change resets the streak — a repeated action
            # against a *changing* screen is legitimate (e.g. clicking through
            # a multi-step flow).
            if outcome.route == "physical":
                would_stuck = (
                    self._stuck_streak + 1 if self._same_physical(outcome.action) else 0
                )
                if would_stuck >= REPEAT_ABORT_AFTER:
                    raise StuckLoopError(
                        action=outcome.action,
                        repeats=would_stuck,
                        goal=goal,
                    )

            try:
                if outcome.route == "physical":
                    # Capture meaningful actions before and after actuation;
                    # semantic window checks take precedence where available.
                    observed = self._observe_target(outcome.action)
                    if observed is not None:
                        before, target = observed
                        # Fail-closed coordinate gate: a point outside the
                        # observed display is rejected BEFORE any physical
                        # effect (never clamped — Law 6.3).
                        self._validate_bounds(outcome.action, before)
                    self._execute_physical(outcome.action)
                    # Any physical action may have changed the screen.
                    # Invalidate the screenshot cache so the next OBSERVE
                    # always captures a fresh frame — the model must never
                    # see a stale pre-action screenshot as "current state".
                    self._last_capture_hash = None
                    self._last_screenshot_b64 = None
                    if observed is not None:
                        before, target = observed
                        self._orient(before, target, outcome.action)
                    elif self.window_probe is not None and isinstance(outcome.action, ActivateApp):
                        self._verify_activation(outcome.action)
                    elif isinstance(outcome.action, (TypeText, ClipboardPaste)):
                        self._verify_text_insertion(outcome.action)
                    # Even without --verify, page-navigation actions
                    # (Return, Escape) need a settle delay before the next
                    # OBSERVE captures — otherwise the model sees the
                    # pre-navigation frame and acts on stale state.
                    elif (
                        not self.verify_enabled
                        and action_verification_kind(outcome.action) == "full"
                    ):
                        self._wait_for_settle()
                elif outcome.route == "internal_wait":
                    self._sleep_for(outcome.action)
                elif outcome.route == "internal_skill":
                    # Explicit Stage 2: the provider asked for this skill by id;
                    # mount it (replacing any auto-retrieved one).
                    self._skill = self._load_skill_for(outcome.action)
            except KillSwitchTripped:
                # Physical drivers may also raise a trip (e.g. during a long
                # type/drag); propagate it out cleanly rather than folding it
                # into a generic failure.
                raise
            except Exception as exc:  # noqa: BLE001 - shell must survive provider/OS faults
                # Law 2: fold the failure into state so the provider steers
                # around it on the next iteration instead of dying. The failed
                # step is deliberately NOT added to completed_steps (F2); the
                # next provider turn sees an accurate picture of what ran.
                failure_message = f"{type(exc).__name__}: {exc}"
                self._last_error = failure_message
                state = WorkingState(
                    goal=state.goal,
                    completed_steps=state.completed_steps,
                    last_error=failure_message,
                    step_index=state.step_index,
                    knowledge=state.knowledge,
                    active_window=state.active_window,
                    ui_elements=state.ui_elements,
                    skill=state.skill,
                    screenshot_b64=state.screenshot_b64,
                    plan=state.plan,
                )
                LOGGER.warning("ooda step %s failed: %s", outcome.action.type, state.last_error)
                continue

            # The action succeeded: only now does the step enter the completed
            # history, keeping the trace honest for the next ORIENT (F2). The
            # typed action joins the distilled trajectory too — except the
            # finish itself, which is orchestrator-internal and would pollute
            # the flow signature (every workflow would end with "finish").
            if outcome.route != "finish":
                self._executed.append(outcome.action)
                self._sub_goals.append(decision.sub_goal or outcome.step_label)
                if outcome.route == "physical":
                    # Live step visibility: a real run takes seconds per LLM
                    # decision, and a silent terminal reads as "nothing is
                    # happening". Log every executed physical action with its
                    # payload so an interactive user sees the agent working.
                    LOGGER.info(
                        "ooda %s: %s", outcome.step_label, outcome.action.model_dump(exclude_none=True)
                    )
            # Stuck-loop warn: after REPEAT_WARN_AFTER identical executions
            # with no screen progress, fold the corrective hint into the next
            # provider state so the model sees it *before* it repeats again.
            repeat_hint: str | None = None
            if outcome.route == "physical":
                fingerprint = (state.active_window, state.ui_elements, state.screenshot_b64)
                # First iteration: no previous fingerprint, so we can't claim
                # the screen changed — treat as unchanged (conservative: the
                # streak grows, which is the safe direction for a stuck model).
                screen_changed = (
                    self._last_progress_fingerprint is not None
                    and self._last_progress_fingerprint != fingerprint
                )
                self._last_progress_fingerprint = fingerprint
                repeat_hint = self._register_executed(outcome.action, screen_changed)
            # A successful action clears obsolete recovery diagnostics. Preserve
            # the most recent failure only when this action itself emitted a
            # fresh repetition hint; otherwise a verified success restores a
            # clean working context.
            state = WorkingState(
                goal=state.goal,
                completed_steps=state.completed_steps + (outcome.step_label,),
                last_error=repeat_hint if repeat_hint is not None else state.last_error,
                step_index=state.step_index,
                knowledge=state.knowledge,
                active_window=state.active_window,
                ui_elements=state.ui_elements,
                # Authoritative runner skill state: reflects an explicit
                # load_skill mounted earlier in this same iteration.
                skill=self._skill,
                screenshot_b64=state.screenshot_b64,
                plan=state.plan,
            )

            if outcome.route == "finish":
                if (
                    isinstance(outcome.action, Finish)
                    and outcome.action.status == "success"
                    and (
                        self.sensor is not None
                        or self.window_probe is not None
                        or self.ax_probe is not None
                    )
                ):
                    self._verify_finish(state)
                # Terminal decisions are a fresh control boundary: repetition
                # diagnostics are useful while selecting an action, but must
                # not survive a valid completion decision.
                state = WorkingState(
                    goal=state.goal,
                    completed_steps=state.completed_steps,
                    last_error=self._last_error,
                    step_index=state.step_index,
                    knowledge=state.knowledge,
                    active_window=state.active_window,
                    ui_elements=state.ui_elements,
                    skill=self._skill,
                    screenshot_b64=state.screenshot_b64,
                    plan=state.plan,
                )
                # Phase 3: when executing a hierarchical plan, a ``finish``
                # means the CURRENT sub-goal is done — advance to the next one
                # instead of terminating. The loop ends only when the whole
                # plan is complete (no pending/in-progress sub-goal remains).
                if state.plan is not None:
                    # The route says "finish", so the action must be a Finish;
                    # narrow through the union explicitly (Law 6.3: never
                    # getattr-bypass a routed invariant).
                    if not isinstance(outcome.action, Finish):
                        raise RuntimeError(
                            f"OODA coding error: finish branch received "
                            f"{type(outcome.action).__name__}"
                        )
                    success = outcome.action.status == "success"
                    advanced = advance_plan(
                        state.plan,
                        success=success,
                        error=outcome.action.summary if not success else None,
                    )
                    if advanced.current_sub_goal is not None:
                        state = WorkingState(
                            goal=state.goal,
                            completed_steps=state.completed_steps,
                            last_error=state.last_error,
                            step_index=state.step_index,
                            knowledge=state.knowledge,
                            active_window=state.active_window,
                            ui_elements=state.ui_elements,
                            skill=self._skill,
                            screenshot_b64=state.screenshot_b64,
                            plan=advanced,
                        )
                        if self.on_sub_goal_complete is not None:
                            self.on_sub_goal_complete(advanced)
                        next_sub_goal = advanced.current_sub_goal
                        assert next_sub_goal is not None
                        LOGGER.info(
                            "ooda sub-goal complete; next: %r (%s)",
                            next_sub_goal.description,
                            next_sub_goal.success_criteria,
                        )
                        continue

                LOGGER.info("ooda finished goal=%r at step %s", goal, state.step_index)
                # OODA step 7 (DISTILL): hand the executed trajectory to the
                # caller so a successful run can become a reusable skill and
                # every terminal run can be remembered (Law 3 + Law 4).
                self._finalize(state, outcome.action)
                return state

        LOGGER.warning("ooda hit max_steps=%s on goal=%r", self.max_steps, goal)
        # A truncated run is a failure the caller must see, not a silent stop:
        # no DISTILL, no episode — and no ambiguity about why the run ended.
        raise MaxStepsError(steps=self.max_steps, goal=goal)

    def _execute_physical(self, action: Action) -> None:
        """Execute a physical action while honoring the emergency stop."""
        def cancelled() -> bool:
            return self.kill_switch is not None and self.kill_switch.tripped()

        if cancelled():
            raise KillSwitchTripped("human reclaimed control before physical action")
        if self.execute_physical_cancellable is not None:
            self.execute_physical_cancellable(action, cancelled)
        else:
            self.execute_physical(action)
        if cancelled():
            raise KillSwitchTripped("human reclaimed control during physical action")

    def _same_physical(self, action: Action) -> bool:
        """Whether an action continues the current identical-repeat streak."""
        return self._last_physical is not None and same_physical_action(
            self._last_physical, action
        )

    def _register_executed(self, action: Action, screen_changed: bool) -> str | None:
        """Update the stuck-loop streak after a physical action succeeded.

        Single streak counter: the streak grows only when the action is
        identical to the previous one AND the screen did not change. Any
        other action or any screen change resets to 0. Returns the corrective
        hint once the streak reaches ``REPEAT_WARN_AFTER`` (the runner folds
        it into ``last_error``); non-guard-sensitive actions reset the streak.
        """
        if not repetition_sensitive(action):
            self._last_physical = None
            self._stuck_streak = 0
            return None
        is_same = self._same_physical(action)
        if is_same and not screen_changed:
            self._stuck_streak += 1
        else:
            self._stuck_streak = 0
        self._last_physical = action
        if self._stuck_streak >= REPEAT_WARN_AFTER:
            return repetition_diagnostic(action, self._stuck_streak)
        return None

    def _observe(self, state: WorkingState) -> WorkingState:
        """Law 2 OBSERVE: refresh window + UI-element context before a decision.

        Best-effort by design: a probe failure (driver hiccup, consent revoked)
        degrades to the previous context with a logged warning — a perception
        gap must not abort the whole workflow, but it is never swallowed
        silently (Law 6.3). Both probes are folded in one pure transition so
        the state never flickers through a half-refreshed intermediate.
        """
        active_window = state.active_window
        ui_elements = state.ui_elements
        if self.window_probe is not None:
            try:
                active_window = window_summary(self.window_probe())
            except Exception as exc:  # noqa: BLE001 - probe is best-effort perception
                if self._window_probe_warned:
                    LOGGER.debug("focused-window probe still failing: %s", exc)
                else:
                    self._window_probe_warned = True
                    LOGGER.warning("focused-window probe failed: %s", exc)
        if self.ax_probe is not None:
            try:
                ui_elements = self.ax_probe()
            except Exception as exc:  # noqa: BLE001 - probe is best-effort perception
                if self._ax_probe_warned:
                    LOGGER.debug("ui-element probe still failing: %s", exc)
                else:
                    self._ax_probe_warned = True
                    LOGGER.warning("ui-element probe failed: %s", exc)
        screenshot_b64 = state.screenshot_b64
        if self.sensor is not None and self.vision_enabled:
            screenshot_b64 = self._probe_screenshot() or state.screenshot_b64
        if (
            active_window == state.active_window
            and ui_elements == state.ui_elements
            and screenshot_b64 == state.screenshot_b64
        ):
            return state
        return WorkingState(
            goal=state.goal,
            completed_steps=state.completed_steps,
            last_error=state.last_error,
            step_index=state.step_index,
            knowledge=state.knowledge,
            active_window=active_window,
            ui_elements=ui_elements,
            skill=state.skill,
            screenshot_b64=screenshot_b64,
            plan=state.plan,
        )

    def _validate(self, decision: AgentTurn) -> None:
        """Law 5.1 VALIDATE: enforce the autonomy guard before actuating.

        A ``BLOCK`` means the policy forbids it outright (e.g. destructive at
        Level 0), and a ``CONFIRM`` means a guarded/supervised human must sign
        off first. Both raise before the physical layer is ever reached.
        """
        if self.guard is None:
            return
        verdict = self.guard(decision)
        if verdict is PermissionDecision.BLOCK:
            raise PermissionDeniedError(
                f"action {decision.action.type!r} for goal {decision.sub_goal!r} "
                "was blocked by the autonomy guard (Law 5)"
            )
        if verdict is PermissionDecision.CONFIRM:
            if self.confirm_handler is not None:
                if self.confirm_handler(decision):
                    return
                raise PermissionDeniedError(
                    f"action {decision.action.type!r} ({decision.action.model_dump(exclude_none=True)}) "
                    "was rejected during human confirmation"
                )
            raise PermissionConfirmationRequired(
                f"action {decision.action.type!r} ({decision.action.model_dump(exclude_none=True)}) "
                "requires human confirmation before it can run"
            )

    def _probe_screenshot(self) -> str | None:
        """Capture, downscale to logical resolution, and encode as base64 PNG.

        Uses the screenshot cache: if the frame fingerprint matches the
        previous capture, reuses the cached base64 string — avoids the full
        PNG encode on an idle screen (the dominant case).
        """
        try:
            from computeruse.vision.capture import (
                capture_to_base64_png,
                fallback_screencapture_b64,
                frame_fingerprint,
                to_logical_resolution,
            )

            capture = self.sensor()  # type: ignore[misc]
            fingerprint = frame_fingerprint(capture)
            if fingerprint == self._last_capture_hash and self._last_screenshot_b64 is not None:
                return self._last_screenshot_b64
            screenshot_b64 = capture_to_base64_png(to_logical_resolution(capture))
            self._last_capture_hash = fingerprint
            self._last_screenshot_b64 = screenshot_b64
            return screenshot_b64
        except Exception as exc:  # noqa: BLE001
            from computeruse.vision.capture import fallback_screencapture_b64

            fallback_b64 = fallback_screencapture_b64()
            if fallback_b64 is not None:
                self._last_screenshot_b64 = fallback_b64
                return fallback_b64
            if not self._screenshot_warned:
                self._screenshot_warned = True
                LOGGER.warning("screenshot capture failed during observe: %s", exc)
            else:
                LOGGER.debug("screenshot capture still failing: %s", exc)
            return None

    def _validate_bounds(self, action: Action, capture: ScreenCapture) -> None:
        """Fail-closed: reject coordinates outside the observed main display.

        A model can hallucinate coordinates; the schema only enforces
        non-negative values. The captured frame's logical size (physical px /
        scale) is the main display's bounds — a point beyond it (a phantom
        coordinate, or a secondary-display target, which is not yet supported)
        is rejected before any physical effect instead of silently clicking
        somewhere the agent did not intend. The failure folds into
        ``last_error`` so the next turn re-derives the coordinate from the
        screenshot (ADR-2: perception grounds generation).
        """
        logical_w = capture.width / capture.scale
        logical_h = capture.height / capture.scale
        targets: list[tuple[str, int, int]] = []
        if isinstance(action, MouseClick):
            targets.append(("click", action.x, action.y))
        elif isinstance(action, MouseDrag):
            targets.append(("drag start", action.start_x, action.start_y))
            targets.append(("drag end", action.end_x, action.end_y))
        for label, x, y in targets:
            if not (0 <= x < logical_w and 0 <= y < logical_h):
                raise CoordinateOutOfBoundsError(
                    f"{action.type} {label} coordinate ({x},{y}) is outside the "
                    f"observed main display {logical_w:.0f}x{logical_h:.0f} logical "
                    "points; rejecting before actuation (fail-closed; multi-display "
                    "targets are not yet supported). Re-derive the coordinate from "
                    "the current screenshot."
                )

    def _observe_target(self, action: Action) -> tuple[ScreenCapture, Point] | None:
        """Capture before verifiable actions.

        ``region`` actions get a local pre/post diff around their target
        point. ``full`` actions (Return, Escape) get a full-screen pre/post
        diff — the transition is page-wide, so a local region would miss it.
        Scrolling is included: it has no point target, but the whole frame
        is the observable region and a visible content transition is the
        strongest available deterministic evidence.
        """
        if self.sensor is None or not self.verify_enabled:
            return None
        kind = action_verification_kind(action)
        if kind == "region":
            target = target_point_of(action)
            if target is None:
                if isinstance(action, MouseScroll):
                    return (self.sensor(), Point(0, 0))
                return None
            return (self.sensor(), target)
        if kind == "full":
            # Full-screen diff: target is (0,0) and the orient step uses the
            # entire frame as the region. A Return key submits a search →
            # the page changes; an Escape closes a modal → the screen changes.
            return (self.sensor(), Point(0, 0))
        return None

    def _orient(self, before: ScreenCapture, target: Point, action: Action) -> None:
        """Law 2 ORIENT: verify the action changed its target region on screen.

        Captures after the action, diffs the region around ``target``, and
        raises :class:`VisualVerificationFailedError` (with rich diagnostics
        for the next provider turn) when the region did not change. The generic
        handler in :meth:`run` folds that failure into ``last_error`` and the
        failed step never enters ``completed_steps`` (F2).

        ``full`` verification kinds (Return, Escape) diff the entire frame:
        the transition is page-wide, so a 48pt local region would miss it.
        ``region`` kinds diff a generous square around the target point.
        """
        sensor = self.sensor
        if sensor is None:
            raise RuntimeError("ORIENT requires a sensor; this is a coding error")
        # Post-action settle: page-navigation actions (Return submitting a
        # search, Escape closing a modal) need the host to render before the
        # after-capture is meaningful. A fixed delay is fragile — Chrome on
        # a fast machine renders in 200ms, on a slow one in 2s. Instead,
        # poll the focused-window title: if it changes (URL changed, page
        # title changed), the navigation landed and we capture immediately.
        # If the title is stable for the whole window, fall back to a final
        # capture — the diff itself is the ultimate arbiter.
        kind = action_verification_kind(action)
        if kind == "full":
            self._wait_for_settle()
        after = sensor()
        if before.display_id != after.display_id:
            raise VisualVerificationFailedError(
                action_type=action.type,
                target=target,
                region=verification_region(target),
                verdict=ChangeVerdict(kind=ChangeKind.UNCHANGED, mean_abs_change=0.0, changed_fraction=0.0),
            )
        # Full-screen diff for Return/Escape; local region for clicks/scrolls.
        if kind == "full" or isinstance(action, MouseScroll):
            region = Rect(Point(0, 0), Size(float(before.width), float(before.height)))
        else:
            region = verification_region(target)
        verification = verify_capture_region(before, after, region)
        if verification.changed:
            LOGGER.info(
                "ooda verified %s at (%s,%s) -> %s",
                action.type,
                target.x,
                target.y,
                verification.verdict.kind.value,
            )
            # Invalidate the screenshot cache: the screen changed, so the
            # next OBSERVE must capture and encode a fresh frame rather than
            # reuse the pre-action cached one.
            self._last_capture_hash = None
            self._last_screenshot_b64 = None
            return
        raise VisualVerificationFailedError(
            action_type=action.type,
            target=target,
            region=region,
            verdict=verification.verdict,
        )

    def _wait_for_settle(self, *, max_polls: int = 10, interval_s: float = 0.15) -> None:
        """Wait for the host to settle after a page-transition action.

        Polls the focused-window title: if it changes (URL navigated, page
        title changed), the transition landed and we return immediately —
        no need to wait the full budget. If the window probe is unavailable
        or the title never changes, we wait the full ``max_polls * interval_s``
        (1.5s) as a fallback so the after-capture still has a chance to see
        a rendered page rather than a half-loaded one.
        """
        import time

        if self.window_probe is None:
            time.sleep(max_polls * interval_s)
            return
        try:
            before_title = self.window_probe().window_title
        except Exception:  # noqa: BLE001 - probe is best-effort
            time.sleep(max_polls * interval_s)
            return
        for _ in range(max_polls):
            time.sleep(interval_s)
            try:
                after_title = self.window_probe().window_title
            except Exception as exc:  # noqa: BLE001 - probe is best-effort
                LOGGER.debug("window probe failed during settle poll: %s", exc)
                continue
            if after_title != before_title:
                return  # Title changed — navigation landed.

    def _verify_text_insertion(self, action: TypeText | ClipboardPaste) -> None:
        """Verify typed/pasted text visibly landed in the focused input.

        Uses the AXValue of the focused text field (ADR-2 state source). When
        the probe is unavailable or the value is not determinable, verification
        is skipped — absence of evidence must not fail a valid action (Law 2).
        Only a *contradictory* value (non-empty yet lacking the expected text)
        raises, because that is conclusive: the text did not land where the
        agent thinks it did.
        """
        if self.focused_text_value is None or not action.text:
            return
        try:
            current = self.focused_text_value()
        except Exception as exc:  # noqa: BLE001 - a probe failure is a perception gap
            LOGGER.debug("text-value probe failed; skipping semantic check: %s", exc)
            return
        if current is None or not current:
            # Insufficient evidence (no focused text field, empty/redacted
            # value): do not claim verification either way.
            return
        if action.text not in current:
            raise SemanticVerificationFailedError(
                f"text insertion verification failed: expected {action.text!r} to appear "
                f"in the focused input field, but its current value is {current!r}. "
                "The typing/paste did not land in the expected field — re-check which "
                "field holds focus and where the text went before retrying."
            )

    def _verify_activation(self, action: ActivateApp) -> None:
        """Verify that the requested app owns the focused window."""
        if self.window_probe is None:
            raise RuntimeError(
                f"activation verification unavailable for {action.app!r}: "
                "no focused-window probe is configured"
            )
        focused = self.window_probe()
        if focused.app_name.casefold() != action.app.casefold():
            raise RuntimeError(
                f"activation verification failed: requested {action.app!r}, "
                f"focused {focused.app_name!r}"
            )

    def _verify_finish(self, state: WorkingState) -> None:
        """Require fresh observable evidence before accepting finish(success).

        A configured screenshot or focused-window probe is evidence that the
        final decision was made against a current host state. With neither
        available, accepting success would turn a model claim into an
        unverified completion.
        """
        if self.sensor is None and self.window_probe is None and self.ax_probe is None:
            raise RuntimeError(
                "finish verification unavailable: no screenshot, focused-window, or AX probe"
            )
        if self.vision_enabled and not state.screenshot_b64:
            raise RuntimeError(
                "finish verification unavailable: the latest screenshot is missing"
            )
        # The normal pre-decision observe already refreshed the authoritative
        # state. Avoid an extra capture here: verification must not consume a
        # second sensor frame or make finish depend on an unbounded probe.
        if self.window_probe is not None or self.ax_probe is not None:
            # Probes are best-effort perception sources; their failures are
            # already diagnosed by OBSERVE and must not make finish handling
            # less reliable than ordinary turns.
            try:
                if self.window_probe is not None:
                    self.window_probe()
                elif self.ax_probe is not None:
                    self.ax_probe()
            except Exception as exc:  # noqa: BLE001 - perception degradation is recoverable
                LOGGER.warning("finish verification probe unavailable: %s", exc)

    def _finalize(self, state: WorkingState, finish: Action) -> None:
        """OODA step 7: trigger the DISTILL/remember hooks on a terminal run.

        Only fires when at least one action actually executed — an empty run
        carries no trajectory to distil and no episode worth remembering.
        Aborted runs (``max_steps``) and kill-switch takeovers never reach
        here: a truncated trace would teach the skill store noise.
        """
        if self.on_complete is None or not self._executed:
            return
        if finish.type != "finish":
            # Internal invariant: only called from the finish branch.
            raise RuntimeError("DISTILL requires a finish action; this is a coding error")
        outcome: EpisodeOutcome = "success" if finish.status == "success" else "failure"
        # step_descriptions: the completed-steps labels ("<sub_goal> -> <type>")
        # for the executed actions. The distiller folds the intent part into the
        # flow signature, so two runs that click the same coordinates for
        # DIFFERENT reasons no longer collide into one skill (Law 3.3).
        self.on_complete(
            Trajectory(
                app=self.app,
                description=state.goal,
                steps=tuple(self._executed),
                step_descriptions=tuple(self._sub_goals),
            ),
            outcome,
        )

    @property
    def executed_trajectory(self) -> tuple[Action, ...]:
        """The actions that physically succeeded in the most recent run.

        Finishes and failed steps are excluded, so a caller auditing or
        distilling from this never records work that did not happen.
        """
        return tuple(self._executed)

    def _sleep_for(self, action: Action) -> None:
        # The route says "internal_wait", so the action must be a Wait. Narrow
        # through the union explicitly instead of getattr-bypassing the type
        # system: if routing ever drifts, this fails loudly as a coding error
        # rather than poisoning the shell with a wrong-typed action (Law 6.3).
        if not isinstance(action, Wait):
            raise RuntimeError(  # noqa: TRY004 - coding-invariant error, not a type error
                f"OODA coding error: _sleep_for received {type(action).__name__}, "
                "expected Wait (route/internal action mismatch)"
            )
        duration_ms = action.duration_ms
        LOGGER.info("ooda wait %sms (%s)", duration_ms, action.reason)
        # NOTE: time.sleep lives here, the shell; tests inject zero so it stays fast.
        import time

        time.sleep(duration_ms / 1000.0)

    def _load_skill_for(self, action: Action) -> SkillDefinition:
        """Law 3 Stage 2: load the requested skill's full definition.

        Same discipline as _sleep_for: the route says "internal_skill", so the
        action must be a LoadSkill — narrow, never getattr (Law 6.3). The
        loaded definition is what the caller mounts into the working context.
        """
        if not isinstance(action, LoadSkill):
            raise RuntimeError(  # noqa: TRY004 - coding-invariant error, not a type error
                f"OODA coding error: _load_skill_for received {type(action).__name__}, "
                "expected LoadSkill (route/internal action mismatch)"
            )
        skill_id = action.skill_id
        if self.skill_loader is None:
            raise RuntimeError(f"load_skill requested ({skill_id}) but no loader configured")
        return self.skill_loader(skill_id)

    def _retrieve(self, state: WorkingState) -> WorkingState:
        """OODA step 3 (Law 3): two-stage skill retrieval before a decision.

        Stage 1 scans the summary index with the goal as the query; Stage 2
        loads the top *same-app* match (skills are app-scoped — another app's
        workflow must not mount) and carries its full instructions into the
        provider context. Runs at most once per run: after a mount, only an
        explicit ``load_skill`` swaps the mounted skill. Best-effort: a scan
        or load failure degrades to the unmounted context with a warning,
        never aborts the workflow (Law 6.3).
        """
        if self.skill_scan is None or self.skill_loader is None:
            return state
        if self._skill is not None:
            return state
        try:
            matches = self.skill_scan(state.goal)
        except Exception as exc:  # noqa: BLE001 - retrieval is best-effort
            LOGGER.warning("skill scan failed: %s", exc)
            return state
        if not matches:
            return state
        top = matches[0]
        summary = top.summary if isinstance(top, RelevanceMatch) else top
        score = top.score if isinstance(top, RelevanceMatch) else SKILL_MOUNT_MIN_SCORE
        if score < SKILL_MOUNT_MIN_SCORE:
            LOGGER.info(
                "skill scan: top match %r too weak (score %d < %d); not mounting",
                summary.skill_id,
                score,
                SKILL_MOUNT_MIN_SCORE,
            )
            return state
        if summary.app != self.app:
            return state
        try:
            self._skill = self.skill_loader(summary.skill_id)
        except Exception as exc:  # noqa: BLE001 - retrieval is best-effort
            LOGGER.warning("skill load failed: %s", exc)
            return state
        return WorkingState(
            goal=state.goal,
            completed_steps=state.completed_steps,
            last_error=state.last_error,
            step_index=state.step_index,
            knowledge=state.knowledge,
            active_window=state.active_window,
            ui_elements=state.ui_elements,
            skill=self._skill,
            plan=state.plan,
        )


class KillSwitchTripped(RuntimeError):
    """Raised when the human reclaims control of the physical host (Law 5).

    Raised by the OODA loop and physical drivers when a kill-switch trips; a    caller distinguishes a user takeover from a generic action failure.
    """


class StuckLoopError(RuntimeError):
    """The provider repeated one physical action with no progress (Law 2).

    Raised *before* the repeat that would exceed ``REPEAT_ABORT_AFTER``, so the
    run terminates with at most ``REPEAT_ABORT_AFTER - 1`` executions of the
    same action. Guarantees the loop always ends even against a degenerate
    model — a lost agent must never click the same spot forever.
    """

    def __init__(self, *, action: Action, repeats: int, goal: str) -> None:
        self.action = action
        self.repeats = repeats
        self.goal = goal
        super().__init__(
            f"stuck loop: the agent repeated the identical {action.type} action "
            f"{repeats} times for goal={goal!r} with no progress; aborting. "
            "Make the goal more specific, or run with --verify so ORIENT can "
            "confirm whether actions actually land."
        )


class MaxStepsError(RuntimeError):
    """The loop exhausted ``max_steps`` without a finish (bounded termination).

    Raised instead of silently returning: a truncated run carries no trajectory
    to distil and no episode to remember, so it must surface as a typed
    failure the caller can report, not a quiet exit.
    """

    def __init__(self, *, steps: int, goal: str) -> None:
        self.steps = steps
        self.goal = goal
        super().__init__(
            f"run exceeded max_steps={steps} on goal={goal!r} without finishing"
        )


class SemanticVerificationFailedError(RuntimeError):
    """A text action was ACKed but the focused field shows no trace of it.

    The ADR-2 AXValue postcondition contradicted the intended outcome: the
    focused input's value is non-empty yet lacks the typed/pasted text. The
    message is the LLM-facing hint folded into ``last_error`` so the next turn
    re-targets instead of repeating the same miss.
    """


class VisualVerificationFailedError(RuntimeError):
    """An action was ACKed by the driver but produced no visible change.

    The ORIENT step's verdict: the pixels say the action did not land. Carries
    the rich context Law 6.3 demands (target, region, both diff signals) so a
    caller can log structured diagnostics; the message itself is the
    LLM-facing hint folded into ``last_error`` for the next provider turn.
    """

    def __init__(
        self,
        *,
        action_type: str,
        target: Point,
        region: Rect,
        verdict: ChangeVerdict,
    ) -> None:
        self.action_type = action_type
        self.target = target
        self.region = region
        self.verdict = verdict
        super().__init__(
            visual_failure_diagnostics(action_type, target, region, verdict)
        )
