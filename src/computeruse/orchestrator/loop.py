"""The autonomy cycle — the orchestration spine.

One pass is ``OBSERVE -> UNDERSTAND -> PLAN -> VALIDATE -> ACT -> VERIFY ->
RECOVER``, and the module keeps Law 6's split honestly:

* ``decide_step`` is the pure core. It consumes an immutable
  :class:`WorkingState` and an :class:`AgentTurn` decision and returns a routed
  :class:`StepOutcome`, without performing any I/O. Routing (physical action,
  internal ``wait``, ``finish``, or ``load_skill``) is a pure classification.
  The pure verification helpers live in
  :mod:`computeruse.orchestrator.evidence` (what should become observably
  true) and :mod:`computeruse.orchestrator.failures` (what to do when it did
  not).

* :class:`OodaRunner` is the imperative shell. It owns the side effects: taking
  one :class:`Observation` per cycle, asking a provider for the next decision,
  gating that decision (permissions, coordinate space, display bounds, focus
  drift, staleness), dispatching to the driver, polling the witnesses that say
  whether the action landed, and folding a classified failure back into state
  so the provider can steer around it.

Two invariants carry most of the reliability:

* **One coordinate space.** :class:`~computeruse.vision.coordinates.ScreenMap`
  owns both directions between the model's screenshot map and logical screen
  points. Perception converts *into* image space once; actuation converts
  *out of* it once. Nothing else does coordinate arithmetic.
* **No verdict without evidence.** An action is declared failed only when a
  witness directly denies it, or two independent witnesses agree nothing
  happened. Silence is reported as silence — the loop never invents a failure
  it cannot substantiate, and never accepts a success it cannot observe.

The provider is a callable returning :class:`AgentTurn` rather than a hard-coded
LLM, so weak and strong models — or a deterministic fake in tests — all flow
through identical scaffolding.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, Literal

from computeruse.orchestrator.budget import BudgetExceededError
from computeruse.orchestrator.evidence import (
    ActionExpectation,
    CompletionVerdict,
    Evidence,
    app_evidence,
    ax_surface_evidence,
    combine,
    expectation_for,
    target_focus_evidence,
    text_evidence,
    verification_diagnostic,
)
from computeruse.orchestrator.failures import (
    MAX_CONSECUTIVE_FAILURES,
    RecoveryAction,
    UnrecoverableFailureError,
    classify_failure,
    recovery_for,
    recovery_hint,
)
from computeruse.orchestrator.planner import GoalPlan, advance_plan
from computeruse.orchestrator.schemas import (
    Action,
    ActivateApp,
    AgentTurn,
    ClickMark,
    Finish,
    LoadSkill,
    MouseClick,
    MouseDrag,
    MouseMove,
    MouseScroll,
    Wait,
)
from computeruse.orchestrator.trace import StepTrace
from computeruse.security.killswitch import KillSwitch
from computeruse.security.permissions import (
    PermissionConfirmationRequired,
    PermissionDecision,
    PermissionDeniedError,
)
from computeruse.skills.distiller import Trajectory
from computeruse.skills.registry import RelevanceMatch
from computeruse.skills.schemas import SkillDefinition, SkillSummary
from computeruse.vision.ax import (
    summaries_to_image_space,
    summaries_within,
    summary_covering,
    summary_label,
)
from computeruse.vision.capture import (
    SCREENSHOT_MAP_MAX_SIDE,
    ScreenCapture,
    capture_to_base64_png,
    coarse_fingerprint,
    downscale_to_max_side,
    frame_fingerprint,
    screen_map_of,
    to_logical_resolution,
    verify_capture_region,
)
from computeruse.vision.coordinates import (
    CoordinateOutOfBoundsError,
    Point,
    Rect,
    ScreenMap,
    Size,
    point_in_frame,
)
from computeruse.vision.focus import FocusedWindow, window_summary
from computeruse.vision.som import (
    MarkElement,
    annotate_set_of_marks,
    parse_ax_elements_to_marks,
)

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

# Stuck-loop guard thresholds (Law 2): after 3 consecutive equivalent
# physical actions (same intent within STUCK_REPEAT_TOLERANCE_PX) with no
# screen progress the provider receives a corrective hint; the action that
# would exceed REPEAT_ABORT_AFTER is never executed — the loop always
# terminates even against a degenerate model.
REPEAT_WARN_AFTER: Final[int] = 2
REPEAT_ABORT_AFTER: Final[int] = 3

# Post-action settle budget. The host needs time to render before the
# after-observation is meaningful: capturing immediately reads a half-drawn
# frame and reports "nothing changed" for an action that landed perfectly.
# Polling the window title (see ``_wait_for_settle``) returns early on a fast
# app, so the full budget is only ever paid when nothing observable moves.
SETTLE_MAX_POLLS: Final[int] = 8
SETTLE_INTERVAL_S: Final[float] = 0.1

# How many times a claimed completion may be rejected by the auditor before
# the loop accepts the model's own verdict. Without a cap, an auditor and an
# actor that permanently disagree would trade turns until the step budget
# ran out — a stalemate is a worse outcome than an honestly-reported finish.
MAX_FINISH_REJECTIONS: Final[int] = 2

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

# Two pointer actions count as "the same" (stuck-loop guard) when their
# coordinates are within this many screen points of each other. A lost model
# stuck on one target jitters its click coordinates by a few pixels each
# repeat (observed in the field: 2-60px drift) — byte-identical comparison
# never caught it. 32 points is roughly one UI row: two clicks this close are
# the same intent, while genuinely different targets (tested) stay distinct.
STUCK_REPEAT_TOLERANCE_PX: Final[int] = 32


@dataclass(frozen=True)
class AxProbeResult:
    """Result of an AX-tree probe: element summaries + open browser tabs.

    Returned as a single object so the driver's ``ax_snapshot`` RPC is
    called exactly once per probe cycle (Law 4.3: minimal context budget).
    """

    summaries: tuple[str, ...] = ()
    open_tabs: tuple[str, ...] = ()
    #: The app's visible text, for verification only — never shown to the
    #: model. Kept separate from ``summaries`` so noticing that a label changed
    #: does not cost the model any of its element budget.
    content: tuple[str, ...] = ()


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
    # Browser tab awareness: open tab titles extracted from the AX tree.
    # The agent needs this to detect stray tabs (accidental background-tab
    # opens from Cmd+click or similar) and to decide whether to close or
    # switch tabs. Empty for non-browser apps or when no probe is configured.
    open_tabs: tuple[str, ...] = ()
    # Law 3.2: the skill definition mounted into active context (Stage 2 —
    # full instructions on demand). None until the RETRIEVE step mounts a
    # scan match or the provider explicitly emits ``load_skill``.
    skill: SkillDefinition | None = None
    # Multimodal visual perception: base64-encoded PNG of current display
    screenshot_b64: str | None = None
    #: Machine-observed facts from earlier in this run, oldest first — one
    #: entry per window the agent has actually been looking at, carrying that
    #: window's visible text at the time. This is *observed state*, not the
    #: model's account of it, which is why the completion auditor may read it:
    #: a goal spanning two applications has evidence that cannot all be on
    #: screen at once (a calculator covers the page whose number it used), and
    #: without a record of what the machine showed, such a goal is unprovable.
    observed_trail: tuple[str, ...] = ()
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

    @property
    def step_index(self) -> int:
        """Index of the step this outcome describes.

        ``state`` is the *post*-decision projection, whose ``step_index`` has
        already advanced past this step — so the step being described is the
        one before it.
        """
        return self.state.step_index - 1


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
    count all match — a click 2px away is a different action (pure). The
    stuck-loop guard uses :func:`equivalent_action` on top of this so small
    coordinate jitter cannot defeat it.
    """
    return left.model_dump(exclude_none=True) == right.model_dump(exclude_none=True)


def map_action_to_screen(action: Action, screen_map: ScreenMap) -> Action:
    """Map a model-emitted coordinate action into global screen points.

    The VLM perceives the screenshot as a scaled-down map (max 512px, see
    :func:`computeruse.vision.capture.downscale_to_max_side`) of *one* display,
    so every coordinate it reports is in image pixels measured from that
    display's corner — while the driver clicks in global logical points
    measured from the desktop's. :class:`ScreenMap` owns both halves of that
    conversion (the scale and the display's origin), so this gate cannot apply
    one and forget the other: a bare factor was enough while everything ran on
    the primary display and silently wrong the moment it did not.

    Non-coordinate actions pass through unchanged (pure).
    """
    if screen_map.is_identity:
        return action

    def mapped(x: int, y: int) -> tuple[int, int]:
        point = screen_map.to_screen(Point(float(x), float(y)))
        return round(point.x), round(point.y)

    if isinstance(action, (MouseClick, MouseMove)):
        x, y = mapped(action.x, action.y)
        return action.model_copy(update={"x": x, "y": y})
    if isinstance(action, MouseDrag):
        start_x, start_y = mapped(action.start_x, action.start_y)
        end_x, end_y = mapped(action.end_x, action.end_y)
        return action.model_copy(
            update={
                "start_x": start_x,
                "start_y": start_y,
                "end_x": end_x,
                "end_y": end_y,
            }
        )
    return action


def resolve_mark(action: Action, marks: tuple[MarkElement, ...]) -> Action:
    """Turn a mark selection into a click on that element's centre (pure).

    The point of the mark channel: the returned click is already in logical
    screen points, taken from the accessibility rect itself, so it never passes
    through the image-space conversion the model's own coordinates need. On a
    Retina display that conversion is ~3.3 points per image pixel, which is
    most of a link's height — the single largest source of near-misses.

    Non-mark actions pass through untouched. An unknown mark raises rather
    than clicking somewhere plausible: the model picked a number that does not
    name anything, and guessing which element it meant is how an agent clicks
    the wrong thing confidently.
    """
    if not isinstance(action, ClickMark):
        return action
    for mark in marks:
        if mark.index == action.mark:
            centre_x = mark.rect.origin.x + mark.rect.size.width / 2
            centre_y = mark.rect.origin.y + mark.rect.size.height / 2
            return MouseClick(
                type="mouse_click",
                x=max(0, round(centre_x)),
                y=max(0, round(centre_y)),
                button=action.button,
                click_count=action.click_count,
            )
    raise UnknownMarkError(requested=action.mark, available=len(marks))


def equivalent_action(left: Action, right: Action, *, tolerance: int = STUCK_REPEAT_TOLERANCE_PX) -> bool:
    """True when two actions are the *same intent* within a coordinate tolerance.

    A lost model repeats a click at the same spot with small coordinate
    jitter, which defeats :func:`same_physical_action`'s byte-identical
    comparison and lets it click forever with zero progress (observed: 8
    identical-intent clicks that never tripped the guard). Pointer actions
    are equivalent when their type and non-coordinate parameters match and
    every coordinate is within ``tolerance`` screen points — one UI row, so
    two clicks that close are the same target. Non-pointer actions keep the
    exact payload comparison (pure).
    """
    if left.type != right.type:
        return False
    if isinstance(left, MouseClick) and isinstance(right, MouseClick):
        return (
            abs(left.x - right.x) <= tolerance
            and abs(left.y - right.y) <= tolerance
            and left.button == right.button
            and left.click_count == right.click_count
        )
    if isinstance(left, MouseMove) and isinstance(right, MouseMove):
        return (
            abs(left.x - right.x) <= tolerance
            and abs(left.y - right.y) <= tolerance
            and left.duration_ms == right.duration_ms
        )
    if isinstance(left, MouseDrag) and isinstance(right, MouseDrag):
        return (
            abs(left.start_x - right.start_x) <= tolerance
            and abs(left.start_y - right.start_y) <= tolerance
            and abs(left.end_x - right.end_x) <= tolerance
            and abs(left.end_y - right.end_y) <= tolerance
            and left.duration_ms == right.duration_ms
        )
    return same_physical_action(left, right)


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


def target_element_label(action: Action, observation: Observation) -> str | None:
    """The title of the accessibility element a positional action targets (pure).

    This is what turns the autonomy guard from a check on the model's narration
    into a check on the machine: a click lands on a control with a name, and
    that name is the honest description of what the click will do.

    Reads ``raw_ui_elements`` deliberately — the summaries in *logical screen
    points*. The guard runs after the coordinate gate, so the action's
    coordinates are screen points too; looking them up in the image-space list
    (``ui_elements``, roughly 3x smaller numbers) would return whichever
    unrelated element happened to sit at the scaled-down position, which is a
    safety check answering about the wrong button.

    ``None`` for non-positional actions and whenever no summarised element
    covers the point — the element list is budget-capped, so absence is "no
    information", never "nothing is there".
    """
    target = target_point_of(action)
    if target is None:
        return None
    line = summary_covering(observation.raw_ui_elements, target.x, target.y)
    if line is None:
        return None
    return summary_label(line)


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


#: How many distinct windows of observed evidence to carry. Enough to span a
#: two- or three-application task, small enough that the audit prompt stays a
#: second opinion rather than a transcript.
TRAIL_MAX_ENTRIES: Final[int] = 6
#: Characters of visible text kept per window. A count, a title or a status
#: line fits comfortably; a whole article does not, and should not.
TRAIL_MAX_CHARS: Final[int] = 600


def _extend_trail(
    trail: tuple[str, ...],
    window: FocusedWindow | None,
    content: tuple[str, ...],
    max_entries: int,
) -> tuple[str, ...]:
    """Append this observation's evidence, one entry per window (pure).

    Keyed by window so revisiting an app *replaces* its entry rather than
    appending a near-duplicate: the agent switches back and forth, and a trail
    full of the same two windows would push out the very evidence it exists to
    preserve. The newest observation of a window wins, and the window keeps its
    original position so the order still reads as the order things were seen.
    """
    if window is None or not content:
        return trail
    title = window.window_title or window.app_name
    if not title:
        return trail
    text = " | ".join(content)[:TRAIL_MAX_CHARS]
    entry = f"{title}: {text}"
    prefix = f"{title}: "
    replaced = tuple(entry if line.startswith(prefix) else line for line in trail)
    if replaced != trail or any(line.startswith(prefix) for line in trail):
        return replaced
    return (*trail, entry)[-max_entries:]


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
        open_tabs=state.open_tabs,
        skill=state.skill,
        screenshot_b64=state.screenshot_b64,
        # The strategic plan is part of the rolling context: a decision must
        # never drop the roadmap the provider is executing against (Law 4.3).
        plan=state.plan,
    )
    return StepOutcome(state=next_state, action=action, route=route, step_label=step_label)


@dataclass(frozen=True)
class Observation:
    """One complete OBSERVE cycle, stamped so staleness is detectable.

    The loop used to scatter perception across half a dozen runner fields
    (``_last_sensor_frame``, ``_last_capture_hash``, ``_screen_map_factor``,
    the state's ``ui_elements`` …) that were invalidated one at a time. Any
    path that forgot one of them left the next decision reading a frame from
    before the last action — the "stale screenshot" class of failure. Binding
    the whole cycle into one immutable value means perception is replaced
    wholesale or not at all.
    """

    #: Raw frame at capture resolution; ``None`` when no sensor is configured.
    frame: ScreenCapture | None
    #: Base64 PNG of the screenshot map the model perceives, if vision is on.
    screenshot_b64: str | None
    #: The authoritative image-space <-> screen-space conversion for ``frame``.
    screen_map: ScreenMap | None
    #: Frontmost window as of this cycle, when the probe answered.
    window: FocusedWindow | None
    #: AX summaries rewritten into the model's image space — what the provider
    #: reads coordinates from.
    ui_elements: tuple[str, ...]
    #: The AX probe's own output, in logical points. Verification compares
    #: this against a fresh probe: comparing the rescaled form against a raw
    #: one made every action look like it changed the UI, which silently
    #: turned the AX witness into an unconditional "confirmed".
    raw_ui_elements: tuple[str, ...]
    #: The app's visible text at OBSERVE time. Verification compares it against
    #: a fresh probe to notice content changes — a display, a status line, a
    #: result count — that move neither the element list nor enough pixels.
    content: tuple[str, ...]
    open_tabs: tuple[str, ...]
    #: Change-tolerant layout signature used for progress and staleness.
    signature: str
    #: The AX elements as numbered marks, in *logical screen points*, indexed
    #: exactly as the model sees them listed. This is what a ``click_mark``
    #: resolves against, so a selected target lands on the element's own centre
    #: rather than on a coordinate estimated from a 3x-downscaled image.
    marks: tuple[MarkElement, ...] = ()

    @property
    def app_name(self) -> str | None:
        return self.window.app_name if self.window is not None else None


EMPTY_OBSERVATION: Final = Observation(
    frame=None,
    screenshot_b64=None,
    screen_map=None,
    window=None,
    ui_elements=(),
    raw_ui_elements=(),
    content=(),
    open_tabs=(),
    signature="",
)


def observation_signature(observation: Observation) -> str:
    """Collapse an observation into the identity used to detect progress (pure).

    Composite by necessity: no single channel answers "did the screen move?"
    in every configuration. The coarse frame fingerprint is silent when vision
    is off; the window title is silent within a single page; the AX surface is
    silent when consent is missing. The old signature used the raw screenshot
    base64, which differs on *every* capture of a live desktop — so "the screen
    changed" was always true and the stuck-loop guard could never fire — and
    with vision off it was constant, so the guard fired on legitimate work.
    """
    window = observation.window
    title = f"{window.app_name}|{window.window_title}" if window is not None else ""
    return f"{observation.signature}|{title}|{hash(observation.raw_ui_elements)}"


@dataclass
class OodaRunner:
    """Imperative shell: drives the full autonomy cycle with real side effects.

    One pass of the cycle is::

        OBSERVE -> UNDERSTAND -> PLAN -> VALIDATE -> ACT -> VERIFY -> RECOVER

    * **OBSERVE** — :meth:`_observe` takes one :class:`Observation` (frame,
      screenshot map, focused window, AX elements) and stamps it. Every
      downstream step reads that single snapshot, so no two steps can disagree
      about what the screen showed.
    * **UNDERSTAND / PLAN** — ``skill_scan``/``skill_loader`` mount a relevant
      workflow (Law 3), and the provider turns the observation into a decision.
    * **VALIDATE** — the autonomy ``guard`` (Law 5.1), the coordinate gate, the
      display-bounds check, the focus check, and the staleness check all run
      *before* anything physical happens. A decision that fails any of them
      never reaches the host.
    * **ACT** — ``execute_physical`` dispatches to the driver, with the
      kill-switch polled before, during, and after.
    * **VERIFY** — :meth:`_verify` collects independent witnesses (pixels, AX
      state, focused-field value, frontmost app) and asks
      :mod:`computeruse.orchestrator.evidence` whether they corroborate the
      action. Silence is never treated as failure.
    * **RECOVER / REPLAN** — a failure is classified
      (:mod:`computeruse.orchestrator.failures`), counted per signature, and
      folded back into ``last_error`` with escalating guidance. The ladder ends
      in :class:`~computeruse.orchestrator.failures.UnrecoverableFailureError`,
      so the loop can neither repeat one mistake forever nor quit on the first.

    ``completion_check`` is the honesty gate on ``finish``: a model claiming
    success is re-asked, against a *fresh* observation, whether the goal is
    actually satisfied. Without it a hallucinated success ends the run
    unchallenged.
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
    # VALIDATE (Law 5.1). Takes the observation as well as the decision:
    # a safety verdict about a click has to be able to look at what is
    # under the pointer, not only at how the model described the click.
    guard: Callable[[AgentTurn, Observation], PermissionDecision] | None = None
    confirm_handler: Callable[[AgentTurn], bool] | None = None
    sensor: Callable[[], ScreenCapture] | None = None
    # Whether ``sensor`` is used for VERIFY (pre/post action pixel comparison).
    # Off = actions still get their AX/window witnesses, just not pixel ones.
    verify_enabled: bool = False
    # Whether ``sensor`` is used for the multimodal OBSERVE screenshot that
    # the provider sees each turn. Off = no screenshot is attached.
    vision_enabled: bool = False
    # Whether the OBSERVE screenshot is annotated with the AX element boxes
    # (Set-of-Marks). Independent of ``click_mark``, which works off the
    # element list and stays available with vision off entirely — this only
    # controls what is drawn onto the picture.
    set_of_marks_enabled: bool = True
    window_probe: Callable[[], FocusedWindow] | None = None
    ax_probe: Callable[[], AxProbeResult] | None = None
    # Semantic postcondition probe for typed/pasted text: returns the focused
    # text field's current AXValue, or None when not determinable.
    focused_text_value: Callable[[], str | None] | None = None
    # Optional cancellation-aware executor. The legacy executor remains the
    # public fallback; this seam lets long actions poll the kill switch without
    # changing existing callers.
    execute_physical_cancellable: Callable[[Action, Callable[[], bool]], None] | None = None
    app: str = "unknown"
    # Whether the run is pinned to ``app``: the focus guard re-asserts it
    # before positional actions. False when the app was merely discovered from
    # whatever was frontmost, where "drift" is not a meaningful concept.
    app_is_pinned: bool = False
    # DISTILL / remember. Fires on every terminal run, successful or not: the
    # third argument is the retrospective, and it is the whole point of firing
    # on a failure — the trajectory says what was tried, the retrospective says
    # why it stopped. The caller decides what to do with each outcome (Law 3.1
    # only ever wanted a *successful* flow distilled into a skill).
    on_complete: (
        Callable[[Trajectory, EpisodeOutcome, str | None], None] | None
    ) = None
    # Goal-completion auditor (VERIFY, terminal): given the final state and the
    # model's own summary, decide whether the goal is *observably* satisfied.
    completion_check: Callable[[WorkingState, str], CompletionVerdict] | None = None
    #: Optional quiet actuation path: activate the element under a point
    #: directly, returning whether it accepted. When it declines, the ordinary
    #: synthetic click runs instead, so this can only ever add reach.
    quiet_press: Callable[[Point], bool] | None = None
    knowledge: tuple[str, ...] = ()
    # Post-action settle budget, in polls of ``settle_interval_s``. The
    # runner is a mechanism and defaults to no wait; pacing is a product
    # decision, and ``AgentConfig`` supplies the real host's budget
    # (:data:`SETTLE_MAX_POLLS`). A caller driving a backend that never
    # renders — the simulated driver, or an injected fake sensor — would
    # otherwise pay a rendering delay for a screen that cannot change.
    settle_max_polls: int = 0
    settle_interval_s: float = SETTLE_INTERVAL_S
    max_steps: int = 100
    # Phase 3: the hierarchical plan the loop executes. When set, a ``finish``
    # marks the CURRENT sub-goal done and the loop advances to the next one
    # (via ``advance_plan``) instead of terminating; the run ends only when
    # the whole plan is complete. ``on_sub_goal_complete`` fires on every
    # transition so a caller can checkpoint the session (resumability).
    plan: GoalPlan | None = None
    on_sub_goal_complete: Callable[[GoalPlan], None] | None = None
    #: Identity of this run, stamped onto every traced step. Empty when the
    #: caller did not name one — the loop never invents an id it would then be
    #: the only holder of.
    run_id: str = ""
    #: Observability sink: one record per step, whatever the step's outcome.
    #: A callable rather than a writer object so the loop stays free of file
    #: handles (Law 6.1) and a test can assert on the records directly.
    trace: Callable[[StepTrace], None] | None = None
    #: Run-ceiling check, called once per step before anything is decided or
    #: actuated; raises :class:`BudgetExceededError` when the run has spent its
    #: allowance. A callable rather than a budget object because the counters
    #: it reads (wall clock, tokens, cost) are the composition root's, and the
    #: loop should not learn to keep them.
    budget_guard: Callable[[], None] | None = None

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
        # The single authoritative perception snapshot for the current turn.
        self._observation: Observation = EMPTY_OBSERVATION
        # Identity of the window the provider last decided against; the
        # staleness gate compares it against a reading taken at actuation time.
        self._decision_window: tuple[str, str] | None = None
        self._stale_rejections: int = 0
        # Stuck-loop guard (Law 2): the same physical intent repeated while
        # the layout signature does not move.
        self._last_physical: Action | None = None
        self._stuck_streak: int = 0
        # The action awaiting a progress verdict, and the observation
        # signature captured just before it ran.
        self._pending_action: Action | None = None
        self._pre_action_signature: str = ""
        # Screenshot encode cache, keyed by the exact frame fingerprint.
        self._last_capture_hash: str | None = None
        self._last_screenshot_b64: str | None = None
        self._last_error: str | None = None
        # RECOVER: consecutive failures per failure signature, plus the total
        # consecutive-failure count across all signatures.
        self._failure_streaks: dict[str, int] = {}
        self._consecutive_failures: int = 0
        # Rejected finish claims, so a model that cannot prove completion still
        # terminates instead of arguing with the auditor forever.
        self._rejected_finishes: int = 0
        # Whether a physical action ran since ``_observation`` was taken.
        self._physical_since_capture: bool = False
        # The most recent working state the stepping loop produced. Mirrored
        # here only so an abnormal ending can be remembered against the state
        # the run actually reached, not against the empty one it started from.
        self._last_state: WorkingState = WorkingState(goal="")

    def run(self, goal: str) -> WorkingState:
        state = WorkingState(goal=goal, knowledge=self.knowledge, plan=self.plan)
        self._executed = []
        self._sub_goals = []
        self._skill = None
        self._window_probe_warned = False
        self._ax_probe_warned = False
        self._screenshot_warned = False
        self._observation = EMPTY_OBSERVATION
        self._decision_window = None
        self._stale_rejections = 0
        self._last_physical = None
        self._stuck_streak = 0
        self._pending_action = None
        self._pre_action_signature = ""
        self._last_capture_hash = None
        self._last_screenshot_b64 = None
        self._last_error = None
        self._failure_streaks = {}
        self._consecutive_failures = 0
        self._rejected_finishes = 0
        self._physical_since_capture = False
        self._last_state = state
        # Every abnormal ending — the step budget, an exhausted recovery
        # ladder, a human takeover — is remembered as a failed episode before
        # the typed error propagates. A run that worked for twenty steps and
        # then hit a wall used to leave nothing behind at all, which made it
        # indistinguishable from a run that never started (Law 4.1).
        try:
            return self._step_until_finished(state, goal)
        except (
            MaxStepsError,
            UnrecoverableFailureError,
            KillSwitchTripped,
            BudgetExceededError,
        ) as exc:
            self._finalize(
                self._last_state,
                outcome="failure",
                retrospective=failure_retrospective(exc),
            )
            raise

    def _step_until_finished(self, state: WorkingState, goal: str) -> WorkingState:
        """Run the cycle to a terminal outcome (the shell's stepping loop)."""
        for _ in range(self.max_steps):
            self._last_state = state
            # Law 5: yield control to the human the instant a kill-switch trips,
            # even mid-workflow — never start a fresh action against a takeover.
            if self.kill_switch is not None and self.kill_switch.tripped():
                raise KillSwitchTripped(
                    f"human reclaimed control at step {state.step_index} for goal={goal!r}"
                )
            # Run ceilings are checked *between* steps, never mid-action: a run
            # that stops with the cursor halfway through a drag is a worse
            # outcome than one that overshoots its budget by a single step.
            if self.budget_guard is not None:
                self.budget_guard()

            # OBSERVE: one snapshot feeds the decision, the gates, and VERIFY.
            state = self._observe(state)
            # The fresh observation is also the verdict on the previous
            # action: did anything actually move? (Stuck-loop guard.)
            state = self._settle_progress(state)
            # UNDERSTAND / RETRIEVE: mount a relevant skill (Law 3, two-stage).
            state = self._retrieve(state)
            if state.active_window or state.ui_elements:
                LOGGER.info(
                    "ooda observe: window=%r, ax_elements=%d",
                    state.active_window or "unknown",
                    len(state.ui_elements),
                )
            state = self._warn_if_blind(state)
            # The provider decides against exactly this snapshot; remember the
            # window it describes so ACT can detect the host moving underneath.
            self._decision_window = self._decision_window_of(self._observation)
            try:
                decision = self.provider(state)
            except KillSwitchTripped:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad turn is recoverable
                # A model that returns nothing usable is a failure like any
                # other, not the end of the run. The scaffolding already
                # retries with corrective hints; when even those are exhausted
                # the ladder gets its turn — RETRY, then ALTERNATE, then
                # REPLAN, then ABORT. Observed before this: one malformed reply
                # on step 20 of a 30-step run killed the process with a
                # traceback, discarding twenty steps of correct work.
                hint = self._register_failure(exc, None, goal)
                state = replace(state, last_error=hint)
                LOGGER.warning("ooda provider turn failed: %s", hint)
                continue
            if decision.thought:
                LOGGER.info("ooda thought: %s", decision.thought)
            if decision.sub_goal:
                LOGGER.info("ooda sub_goal: %s", decision.sub_goal)
            # OpenAI computer-use lesson (action sequences): one model turn may
            # carry an ordered batch of actions. Execute each in sequence
            # within this same turn — every action is still individually
            # validated, coordinate-gated, verified, and recorded — but no
            # LLM round-trip happens between them, and the LLM turn is the
            # dominant per-step cost (seconds, vs ~150ms for a capture). A
            # failure or a finish ends the batch early; the loop then
            # re-observes and asks the model for the next decision.
            batch = decision.actions or [decision.action]
            finished = False
            for batch_index, batch_action in enumerate(batch):
                if batch_index > 0:
                    # Mid-batch: the previous action changed the screen, so
                    # the model's pre-batch screenshot is stale. Refresh
                    # perception (cheap — no LLM turn) so the next action
                    # decides against the current frame, never the old one.
                    state = self._observe(state)
                    # The batch's later actions were chosen from the pre-batch
                    # frame; accept them against the refreshed one rather than
                    # tripping the staleness gate on our own re-observation.
                    self._decision_window = self._decision_window_of(self._observation)
                single = decision.model_copy(
                    update={"action": batch_action, "actions": None}
                )
                state, finished, stop_batch = self._execute_one(state, single, goal)
                self._last_state = state
                if finished or stop_batch:
                    break
            if finished:
                return state

        LOGGER.warning("ooda hit max_steps=%s on goal=%r", self.max_steps, goal)
        # A truncated run is a failure the caller must see, not a silent stop:
        # no DISTILL, no episode — and no ambiguity about why the run ended.
        raise MaxStepsError(steps=self.max_steps, goal=goal)

    @staticmethod
    def _decision_window_of(observation: Observation) -> tuple[str, str] | None:
        """The window identity a decision was made against (pure)."""
        window = observation.window
        if window is None:
            return None
        return (window.app_name, window.window_title)

    def _warn_if_blind(self, state: WorkingState) -> WorkingState:
        """Tell the model when vision was requested but is not available.

        Flying blind is a legitimate (degraded) mode, but the model must know:
        without this it keeps emitting coordinates as if it could see, and
        every one of them is a guess.
        """
        if self.sensor is None or not self.vision_enabled:
            return state
        if state.screenshot_b64 or state.step_index == 0 or state.last_error is not None:
            return state
        return replace(
            state,
            last_error=(
                "perception unavailable: live screenshot is unavailable. "
                "Grant Screen Recording consent (System Settings > Privacy & Security > "
                "Screen & System Audio Recording) or emit finish if the goal cannot be "
                "accomplished without visual grounding."
            ),
        )

    def _execute_one(
        self, state: WorkingState, decision: AgentTurn, goal: str
    ) -> tuple[WorkingState, bool, bool]:
        """Run one action through VALIDATE -> ACT -> VERIFY -> RECOVER (shell).

        Returns ``(state, finished, stop_batch)``:

        * ``finished`` — a terminal ``finish`` was accepted and the run is
          complete (the caller returns the state).
        * ``stop_batch`` — the batch must stop before this action's
          successors: the action failed (the diagnosis is folded into
          ``last_error`` for the next provider turn) or a ``finish`` advanced
          a hierarchical plan. Either way the outer cycle re-observes.
        """
        # Coordinate gate: the provider reports coordinates in the screenshot
        # map's image space; convert them to real screen points before
        # anything validates or actuates them. ``ScreenMap`` owns the
        # direction, so the conversion cannot be applied backwards.
        screen_map = self._observation.screen_map
        if screen_map is not None:
            decision = decision.model_copy(
                update={"action": map_action_to_screen(decision.action, screen_map)}
            )
        # Mark selection resolves *after* the coordinate gate, never before: a
        # mark already carries the element's rect in screen points, and scaling
        # it a second time would send the click a third of the way up the
        # display. ``map_action_to_screen`` leaves a ``click_mark``
        # untouched precisely so this ordering is safe.
        decision = decision.model_copy(
            update={"action": resolve_mark(decision.action, self._observation.marks)}
        )
        # Law 5.1 VALIDATE: the permission guard sees every proposed action
        # *before* it becomes physical, and can hard-stop a dangerous move.
        # Deliberately outside the recovery handler: a policy denial is the
        # user's decision, not a failure the agent may route around.
        self._validate(decision)
        outcome = decide_step(state, decision)
        # Pure projection only advances step_index; completed_steps is still
        # the pre-action list, so a failure below cannot pollute it (F2).
        state = outcome.state

        verdict: Evidence | None = None
        try:
            if outcome.route == "physical":
                # The repetition guard runs inside the recovery path, not
                # before it: an agent stuck on one target deserves the same
                # escalating "change your approach" ladder as any other
                # failure. Aborting the whole run at the first trip threw away
                # work the model could still have salvaged — while the ladder
                # still guarantees termination, because a trip that keeps
                # repeating climbs to ABORT within a handful of turns.
                self._guard_stuck_loop(outcome.action, goal)
                verdict = self._act_and_verify(outcome.action)
            elif outcome.route == "internal_wait":
                self._sleep_for(outcome.action)
            elif outcome.route == "internal_skill":
                # Explicit Stage 2: the provider asked for this skill by id;
                # mount it (replacing any auto-retrieved one).
                self._skill = self._load_skill_for(outcome.action)
        except KillSwitchTripped as exc:
            # Physical drivers may also raise a trip (e.g. during a long
            # type/drag); propagate it out cleanly rather than folding it
            # into a generic failure.
            self._trace_step(decision, outcome, verdict=None, error=str(exc))
            raise
        except Exception as exc:  # noqa: BLE001 - shell must survive provider/OS faults
            # RECOVER: classify, count, and hand the model an escalating hint.
            # The failed step is deliberately NOT added to completed_steps (F2)
            # so the next turn sees an accurate picture of what actually ran.
            # Traced *before* the ladder gets its turn: ``_register_failure``
            # may abort the run outright, and the step that ended it is the
            # one a person will want to read.
            self._trace_step(
                decision, outcome, verdict=None, error=f"{type(exc).__name__}: {exc}"
            )
            hint = self._register_failure(exc, outcome.action, goal)
            state = replace(state, last_error=hint)
            LOGGER.warning("ooda step %s failed: %s", outcome.action.type, hint)
            return state, False, True

        # A terminal decision is judged before it is recorded: a completion
        # claim the auditor rejects must leave no trace in the history (F2),
        # exactly like a failed action.
        if outcome.route == "finish":
            state, finished, stop_batch = self._finish(
                state, outcome.action, outcome.step_label, goal
            )
            # A rejected completion claim is the interesting case: the trace
            # carries the auditor's reason, not just "the model said done".
            self._trace_step(
                decision,
                outcome,
                verdict=None,
                error=None if finished else state.last_error,
            )
            return state, finished, stop_batch

        # The action succeeded: only now does the step enter the completed
        # history, keeping the trace honest for the next cycle (F2). The
        # typed action joins the distilled trajectory too — except the
        # finish itself, which is orchestrator-internal and would pollute
        # the flow signature (every workflow would end with "finish").
        self._executed.append(outcome.action)
        self._sub_goals.append(decision.sub_goal or outcome.step_label)
        if outcome.route == "physical":
            # Live step visibility: a real run takes seconds per LLM decision,
            # and a silent terminal reads as "nothing is happening". Log every
            # executed physical action with its payload so an interactive user
            # sees the agent working.
            LOGGER.info(
                "ooda %s: %s", outcome.step_label, outcome.action.model_dump(exclude_none=True)
            )
        if outcome.route == "physical":
            self._record_for_progress(outcome.action)
        self._trace_step(decision, outcome, verdict=verdict, error=None)
        # A successful action clears obsolete recovery diagnostics: the
        # provider must not keep steering around a failure that already
        # recovered (M1). A stuck-loop hint is re-injected by the next
        # cycle's progress check, which is when the model can act on it.
        self._last_error = None
        self._failure_streaks.clear()
        self._consecutive_failures = 0
        state = replace(
            state,
            completed_steps=state.completed_steps + (outcome.step_label,),
            last_error=None,
            # Authoritative runner skill state: reflects an explicit
            # load_skill mounted earlier in this same iteration.
            skill=self._skill,
        )
        return state, False, False

    def _trace_step(
        self,
        decision: AgentTurn,
        outcome: StepOutcome,
        *,
        verdict: Evidence | None,
        error: str | None,
    ) -> None:
        """Hand one step to the observability sink (best effort, never fatal).

        Emitted for every step whatever its ending, because the step worth
        reading is almost always the one that failed. A sink that raises is
        swallowed with a warning: diagnostics must not be able to end a run
        they exist to explain.
        """
        if self.trace is None:
            return
        try:
            self.trace(
                StepTrace(
                    run_id=self.run_id,
                    step=outcome.step_index,
                    app=self.app,
                    window=(
                        window_summary(self._observation.window)
                        if self._observation.window is not None
                        else None
                    ),
                    thought=decision.thought,
                    sub_goal=decision.sub_goal,
                    action=outcome.action.model_dump(exclude_none=True),
                    route=outcome.route,
                    verdict=verdict.value if verdict is not None else None,
                    error=error,
                    screenshot_b64=self._observation.screenshot_b64,
                )
            )
        except Exception as exc:  # noqa: BLE001 - a trace sink must never kill a run
            LOGGER.warning("run trace sink failed: %s", exc)

    # ------------------------------------------------------------------
    # ACT + VERIFY
    # ------------------------------------------------------------------

    def _act_and_verify(self, action: Action) -> Evidence:
        """Gate, actuate, and corroborate one physical action.

        Every gate runs before the host is touched, in cheapest-first order,
        and each raises a typed error the recovery ladder understands. The
        returned verdict is what the witnesses concluded, for the run trace —
        a contradiction has already raised by then.
        """
        expectation = expectation_for(action)
        # The quiet path is tried BEFORE the focus gate, not after. That gate
        # exists because a synthetic click goes to whatever is frontmost, so it
        # brings the target app forward first — which is precisely what the
        # quiet path is for avoiding. Guarding first would front the app on
        # every action and give back the whole benefit; observed in a live run,
        # which finished correctly but with the app pulled to the front.
        if self.quiet_press is not None and isinstance(action, ActivateApp):
            # Background mode's one promise is that the user's foreground is
            # not disturbed, and honouring it cannot rest on the model reading
            # prose: a run told not to front the app did it anyway. Actuation
            # here reaches the app wherever it is, so bringing it forward buys
            # nothing and costs exactly the thing the mode exists to protect.
            LOGGER.info("ooda background mode: not fronting %r", action.app)
            # Nothing was actuated, so no witness has anything to say — which
            # is silence, not a miss, and the recovery ladder must not treat it
            # as one.
            return Evidence.INCONCLUSIVE
        quiet = self._pressed_quietly(action)
        if not quiet:
            self._guard_positional(action)
        before = self._pre_action_frame(expectation)
        if before is not None:
            # Fail-closed coordinate gate: a point outside the observed
            # display is rejected BEFORE any physical effect (never clamped).
            self._validate_bounds(action, before)
        before_ui = self._observation.raw_ui_elements
        before_content = self._observation.content
        before_window = self._observation.window

        if not quiet:
            self._execute_physical(action)
        else:
            # The quiet press already touched the host; the cached OBSERVE
            # frame is stale for exactly the same reason.
            self._physical_since_capture = True
        # Any physical action may have changed the screen: invalidate the
        # encode cache so the next OBSERVE captures and encodes a fresh frame.
        self._last_capture_hash = None
        self._last_screenshot_b64 = None

        return self._verify(
            action, expectation, before, before_ui, before_content, before_window
        )

    def _pre_action_frame(self, expectation: ActionExpectation) -> ScreenCapture | None:
        """The frame the bounds check and (optionally) the pixel witness read.

        A cached OBSERVE frame is enough for the bounds check — display
        geometry does not change between two actions, and re-capturing a Retina
        frame is the single most expensive thing the loop can do. The pixel
        witness is the one consumer that needs a frame taken *after* the
        previous action: diffing against a screen two actions old would confirm
        a change this action did not make.
        """
        if self.sensor is None:
            return None
        cached = self._observation.frame
        needs_fresh = self.verify_enabled and expectation.pixel != "none"
        if cached is not None and not (needs_fresh and self._physical_since_capture):
            return cached
        return self.sensor()

    def _verify(
        self,
        action: Action,
        expectation: ActionExpectation,
        before: ScreenCapture | None,
        before_ui: tuple[str, ...],
        before_content: tuple[str, ...],
        before_window: FocusedWindow | None,
    ) -> Evidence:
        """Collect independent witnesses and judge whether the action landed.

        The decisive design choice: a witness that cannot speak returns
        ``INCONCLUSIVE`` and never fails the action, and a single confirming
        witness outweighs silent ones. A failure needs either a *direct*
        denial (the text is not in the field, the wrong app is frontmost) or
        two corroborating *circumstantial* ones — which is what makes the
        check safe to run on every action rather than the narrow subset a
        pixel-only diff could judge without inventing failures.
        """
        if not expectation.is_verifiable:
            return Evidence.INCONCLUSIVE
        if expectation.needs_settle:
            self._wait_for_settle(before_window)

        reports: list[tuple[str, Evidence]] = []
        element_at_target: str | None = None
        direct: list[Evidence] = []
        circumstantial: list[Evidence] = []

        if expectation.expected_app is not None:
            observed_app, observed_bundle = self._probe_app_identity()
            verdict = app_evidence(expectation.expected_app, observed_app, observed_bundle)
            reports.append(("frontmost_app", verdict))
            direct.append(verdict)
        if expectation.expected_text is not None:
            verdict = text_evidence(expectation.expected_text, self._probe_text_value())
            reports.append(("focused_field", verdict))
            direct.append(verdict)
        if (expectation.expects_ui_change or expectation.focus_target is not None) and (
            self.ax_probe is not None
        ):
            # One AX probe serves both witnesses: whether the surface moved at
            # all (circumstantial) and whether the element under the click now
            # holds focus (direct, and the only witness that can vouch for an
            # action which correctly changed nothing).
            after = self._probe_ax()
            after_ui = after.summaries
            # What sits under the click, if anything — the difference between
            # "you missed" and "you hit a control that was already in the state
            # you asked for". Only these two produce identical witness reports.
            if expectation.focus_target is not None:
                element_at_target = summary_covering(
                    after_ui, expectation.focus_target.x, expectation.focus_target.y
                )
            if expectation.focus_target is not None:
                verdict = target_focus_evidence(expectation.focus_target, after_ui)
                reports.append(("target_focus", verdict))
                direct.append(verdict)
            if expectation.expects_ui_change:
                # One witness, two signals: the element list catches structure
                # and focus, the content digest catches text that changes with
                # no structural change at all. Both come from this one probe,
                # so they must not vote twice — see ax_surface_evidence.
                verdict = ax_surface_evidence(
                    before_ui, after_ui, before_content, after.content
                )
                reports.append(("ax_state", verdict))
                circumstantial.append(verdict)
        if expectation.pixel != "none" and self.verify_enabled and before is not None:
            verdict = self._pixel_evidence(before, expectation)
            reports.append(("pixels", verdict))
            circumstantial.append(verdict)

        outcome = combine(direct=tuple(direct), circumstantial=tuple(circumstantial))
        if outcome is Evidence.CONTRADICTED:
            raise VerificationFailedError(
                verification_diagnostic(
                    action.type, expectation, tuple(reports), element_at_target
                )
            )
        detail = ", ".join(f"{name}={value.value}" for name, value in reports)
        if outcome is Evidence.CONFIRMED:
            LOGGER.info("ooda verified %s: %s", action.type, detail)
        else:
            # Not verified is not failed: say so once, at debug volume, and
            # let the model's own next observation be the arbiter.
            LOGGER.debug("ooda %s unverified (no conclusive witness): %s", action.type, detail)
        return outcome

    def _pixel_evidence(
        self, before: ScreenCapture, expectation: ActionExpectation
    ) -> Evidence:
        """Did the pixels corroborate the action (never the sole judge)?"""
        sensor = self.sensor
        if sensor is None:
            return Evidence.INCONCLUSIVE
        try:
            after = sensor()
        except Exception as exc:  # noqa: BLE001 - a dead sensor is silence, not failure
            LOGGER.debug("post-action capture failed; pixels abstain: %s", exc)
            return Evidence.INCONCLUSIVE
        if before.display_id != after.display_id:
            # The captures describe different displays; they cannot be compared.
            return Evidence.INCONCLUSIVE
        if expectation.pixel == "frame":
            # A whole-frame comparison uses the coarse layout signature, not a
            # byte hash: a byte hash "confirms" a navigation because a clock
            # ticked, which is how a failed Return used to pass verification.
            changed = coarse_fingerprint(before) != coarse_fingerprint(after)
            return Evidence.CONFIRMED if changed else Evidence.CONTRADICTED
        target = expectation.region_point
        if target is None:
            return Evidence.INCONCLUSIVE
        if (before.width, before.height, before.scale) != (
            after.width,
            after.height,
            after.scale,
        ):
            # Geometry changed under us (display reconfigured); that is itself
            # a change, but not one this region diff can quantify.
            return Evidence.CONFIRMED
        verification = verify_capture_region(before, after, verification_region(target))
        if verification.changed:
            return Evidence.CONFIRMED
        # A silent region is not a denial. The box is 48 points around the
        # cursor, but an action's visible effect very often lands somewhere
        # else entirely: pressing a calculator key updates the display at the
        # top of the window, following a link repaints the page below the
        # toolbar. Measured on Calculator, this region reported "unchanged" for
        # every one of three correct button presses — and as a CONTRADICTED
        # vote it was one half of the pair needed to call an action failed.
        # Before claiming nothing happened, look at the whole frame; only when
        # the screen is still everywhere is silence real evidence of a miss.
        if coarse_fingerprint(before) != coarse_fingerprint(after):
            return Evidence.INCONCLUSIVE
        return Evidence.CONTRADICTED

    def _probe_app_identity(self) -> tuple[str | None, str]:
        """The frontmost app's localized name and its bundle id (best effort).

        Both identities are returned together because either one alone can be
        wrong about whether the right app is in front: the name is translated
        per locale, and the bundle id is empty for hosts without a bundle.
        """
        if self.window_probe is None:
            return None, ""
        try:
            focused = self.window_probe()
        except Exception as exc:  # noqa: BLE001 - probe is best-effort perception
            LOGGER.debug("window probe failed during verification: %s", exc)
            return None, ""
        return focused.app_name, focused.bundle_id

    def _probe_text_value(self) -> str | None:
        if self.focused_text_value is None:
            return None
        try:
            return self.focused_text_value()
        except Exception as exc:  # noqa: BLE001 - probe is best-effort perception
            LOGGER.debug("text-value probe failed during verification: %s", exc)
            return None

    def _probe_ax(self) -> AxProbeResult:
        """One AX probe serving every accessibility-based witness.

        Called once per verification so the element list, the focus check and
        the content digest all describe the *same* moment — three separate
        probes would let the app move between them and make the witnesses
        disagree about a screen that never existed.
        """
        if self.ax_probe is None:
            return AxProbeResult()
        try:
            return self.ax_probe()
        except Exception as exc:  # noqa: BLE001 - probe is best-effort perception
            LOGGER.debug("ax probe failed during verification: %s", exc)
            return AxProbeResult()

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------

    def _guard_positional(self, action: Action) -> None:
        """One fresh window read gates both focus drift and decision staleness.

        Both questions are about the same thing — "does the window my
        coordinates describe still own the screen?" — and both need a reading
        taken *now*, not the one from before the model's turn. Sharing a single
        ``focused_window`` probe answers them for the price of one small RPC.

        Deliberately not a screenshot: re-capturing a Retina frame before every
        click would roughly double the per-step capture cost, which is the
        dominant latency in the loop. The window identity catches the races
        that actually misplace a click — a navigation completing, an app
        switching, a dialog taking over — while in-page content shifts remain
        the job of post-action verification and the recovery ladder.
        """
        if not isinstance(action, (MouseClick, MouseDrag, MouseScroll)):
            return
        if self.window_probe is None:
            return
        try:
            current = self.window_probe()
        except Exception as exc:  # noqa: BLE001 - a perception gap must not block acting
            LOGGER.debug("window probe failed before actuation: %s", exc)
            return
        self._guard_focus(current, action)
        self._guard_staleness(current)

    def _guard_focus(self, current: FocusedWindow, action: Action) -> None:
        """Refuse positional actions while another app owns the screen.

        Coordinates are meaningless once focus drifts: a dialog, a
        notification, or the user clicking elsewhere silently re-points every
        click at the wrong window. The run's target app is re-asserted once
        (the driver's activation is idempotent) and only a second mismatch is
        reported as a failure.
        """
        if not self.app_is_pinned or not current.app_name:
            return
        if (
            app_evidence(self.app, current.app_name, current.bundle_id)
            is not Evidence.CONTRADICTED
        ):
            return
        LOGGER.warning(
            "focus drifted to %r; re-activating %r before %s",
            current.app_name,
            self.app,
            action.type,
        )
        self.execute_physical(ActivateApp(type="activate_app", app=self.app))
        try:
            after = self.window_probe() if self.window_probe is not None else None
        except Exception as exc:  # noqa: BLE001 - probe is best-effort perception
            LOGGER.debug("window probe failed after re-activation: %s", exc)
            return
        if (
            after is not None
            and app_evidence(self.app, after.app_name, after.bundle_id)
            is Evidence.CONTRADICTED
        ):
            raise FocusLostError(
                f"the target application {self.app!r} is not frontmost "
                f"({after.app_name!r} is), and re-activating it did not help; "
                "coordinates read from the screenshot refer to a window that is "
                "no longer on top"
            )

    def _guard_staleness(self, current: FocusedWindow) -> None:
        """Refuse coordinates derived from a window the host has moved past.

        The model's turn takes seconds, during which a page can finish loading
        or a new window can take over. Clicking a coordinate read off the old
        layout then lands on whatever moved into that spot.

        A second consecutive rejection falls through: on a host whose title
        changes continuously (a video, a progress counter) blocking forever
        would be worse than acting on a slightly old reading, and the
        post-action witnesses still get the final say.
        """
        decided_at = self._decision_window
        if decided_at is None:
            return
        if (current.app_name, current.window_title) == decided_at:
            self._stale_rejections = 0
            return
        if self._stale_rejections >= 1:
            LOGGER.warning("host still changing; acting on a slightly stale reading")
            self._stale_rejections = 0
            return
        self._stale_rejections += 1
        raise StaleObservationError(
            f"the active window changed after this decision was made "
            f"({decided_at[0]!r} — {decided_at[1]!r} became {current.app_name!r} — "
            f"{current.window_title!r}); the coordinates describe a layout that is "
            "no longer on screen — re-read the target from the new screenshot"
        )

    def _guard_stuck_loop(self, action: Action, goal: str) -> None:
        """Refuse the repeat that would exceed the no-progress budget.

        The corrective hint already went into ``last_error`` after the second
        identical action; a model that still repeats is not going to recover by
        repeating again.
        """
        would_stuck = self._stuck_streak + 1 if self._same_physical(action) else 0
        if would_stuck >= REPEAT_ABORT_AFTER:
            raise StuckLoopError(action=action, repeats=would_stuck, goal=goal)

    def _validate(self, decision: AgentTurn) -> None:
        """Law 5.1 VALIDATE: enforce the autonomy guard before actuating.

        A ``BLOCK`` means the policy forbids it outright (e.g. destructive at
        Level 0), and a ``CONFIRM`` means a guarded/supervised human must sign
        off first. Both raise before the physical layer is ever reached.

        The current observation goes to the guard with the decision so the
        policy can classify the control the action actually targets. By this
        point the coordinate gate has already converted the model's image-space
        coordinates into screen points, which is the space
        :func:`target_element_label` reads its summaries in.
        """
        if self.guard is None:
            return
        verdict = self.guard(decision, self._observation)
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

    def _validate_bounds(self, action: Action, capture: ScreenCapture) -> None:
        """Fail-closed: reject coordinates outside the display being observed.

        A model can hallucinate coordinates; the schema only enforces
        non-negative values. The captured frame carries its display's global
        rectangle — origin included, so the gate is about *the display the
        agent is looking at* rather than about the primary one — and a point
        beyond it is rejected before any physical effect instead of silently
        clicking somewhere the agent did not intend.
        """
        frame = capture.display_frame
        targets: list[tuple[str, int, int]] = []
        if isinstance(action, MouseClick):
            targets.append(("click", action.x, action.y))
        elif isinstance(action, MouseDrag):
            targets.append(("drag start", action.start_x, action.start_y))
            targets.append(("drag end", action.end_x, action.end_y))
        for label, x, y in targets:
            if not point_in_frame(Point(float(x), float(y)), frame):
                raise CoordinateOutOfBoundsError(
                    f"{action.type} {label} coordinate ({x},{y}) is outside the "
                    f"observed display {frame.size.width:.0f}x"
                    f"{frame.size.height:.0f} logical points at "
                    f"({frame.origin.x:.0f},{frame.origin.y:.0f}); rejecting before "
                    "actuation (fail-closed). Re-derive the coordinate from the "
                    "current screenshot."
                )

    # ------------------------------------------------------------------
    # RECOVER
    # ------------------------------------------------------------------

    def _register_failure(self, exc: Exception, action: Action | None, goal: str) -> str:
        """Classify a failure, advance its ladder rung, and build the hint.

        ``action`` is ``None`` when the failure happened before there was one —
        a model turn that produced nothing usable. The failure is otherwise
        handled identically, so a bad turn climbs the same finite ladder as a
        bad click instead of escaping the loop.

        Raises :class:`UnrecoverableFailureError` when the ladder is exhausted
        — the guarantee that one obstacle can never consume a whole run.
        """
        failure = classify_failure(exc, action)
        streak = self._failure_streaks.get(failure.signature, 0) + 1
        self._failure_streaks[failure.signature] = streak
        self._consecutive_failures += 1
        if recovery_for(streak) is RecoveryAction.ABORT:
            raise UnrecoverableFailureError(failure=failure, streak=streak, goal=goal)
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            raise UnrecoverableFailureError(
                failure=failure, streak=self._consecutive_failures, goal=goal
            )
        if recovery_for(streak) is RecoveryAction.REPLAN and self._skill is not None:
            # A mounted workflow that keeps failing is actively misleading the
            # model — unmount it so the replan starts from the real screen.
            LOGGER.info("unmounting skill %s after repeated failures", self._skill.skill_id)
            self._skill = None
        hint = recovery_hint(failure, streak)
        self._last_error = hint
        return hint

    def _record_for_progress(self, action: Action) -> None:
        """Remember an executed action so the NEXT observation can judge it.

        Progress is deliberately evaluated one cycle late. Asking "did the
        screen move?" immediately after actuating means either an extra probe
        per step or a read of a half-rendered frame; the next cycle's OBSERVE
        already captures a settled screen for free, and its signature answers
        the same question with no additional I/O.
        """
        if not repetition_sensitive(action):
            self._pending_action = None
            self._last_physical = None
            self._stuck_streak = 0
            return
        self._pending_action = action
        self._pre_action_signature = observation_signature(self._observation)

    def _settle_progress(self, state: WorkingState) -> WorkingState:
        """Score the previous action against the fresh observation (stuck guard).

        The streak grows only when an action repeated the one before it AND
        nothing observable moved. Any different action, or any real change,
        resets it. At ``REPEAT_WARN_AFTER`` the corrective hint is folded into
        the state the provider is about to read — before it repeats again.
        """
        pending = self._pending_action
        if pending is None:
            return state
        self._pending_action = None
        moved = observation_signature(self._observation) != self._pre_action_signature
        if self._same_physical(pending) and not moved:
            self._stuck_streak += 1
        else:
            self._stuck_streak = 0
        self._last_physical = pending
        if self._stuck_streak < REPEAT_WARN_AFTER:
            return state
        hint = repetition_diagnostic(pending, self._stuck_streak)
        self._last_error = hint
        return replace(state, last_error=hint)

    def _same_physical(self, action: Action) -> bool:
        """Whether an action continues the current repeat streak."""
        return self._last_physical is not None and equivalent_action(
            self._last_physical, action
        )

    # ------------------------------------------------------------------
    # OBSERVE
    # ------------------------------------------------------------------

    def _observe(self, state: WorkingState) -> WorkingState:
        """Refresh the whole perception snapshot before a decision.

        Best-effort by design: a probe failure (driver hiccup, consent revoked)
        degrades to the previous context with a logged warning — a perception
        gap must not abort the workflow, but it is never swallowed silently.
        All probes fold into one immutable :class:`Observation`, so the state
        never flickers through a half-refreshed intermediate.
        """
        previous = self._observation
        window = previous.window
        if self.window_probe is not None:
            try:
                window = self.window_probe()
            except Exception as exc:  # noqa: BLE001 - probe is best-effort perception
                if self._window_probe_warned:
                    LOGGER.debug("focused-window probe still failing: %s", exc)
                else:
                    self._window_probe_warned = True
                    LOGGER.warning("focused-window probe failed: %s", exc)
        raw_ui_elements = previous.raw_ui_elements
        content = previous.content
        open_tabs = previous.open_tabs
        if self.ax_probe is not None:
            try:
                ax_result = self.ax_probe()
                raw_ui_elements = ax_result.summaries
                content = ax_result.content
                open_tabs = ax_result.open_tabs
            except Exception as exc:  # noqa: BLE001 - probe is best-effort perception
                if self._ax_probe_warned:
                    LOGGER.debug("ui-element probe still failing: %s", exc)
                else:
                    self._ax_probe_warned = True
                    LOGGER.warning("ui-element probe failed: %s", exc)

        frame = previous.frame
        screenshot_b64 = previous.screenshot_b64
        screen_map = previous.screen_map
        signature = previous.signature
        if self.sensor is not None:
            captured = self._capture_frame(raw_ui_elements)
            if captured is not None:
                frame, screenshot_b64, screen_map = captured
                signature = coarse_fingerprint(frame)
                self._physical_since_capture = False
        # An app's AX tree describes every display it has a window on, while
        # the frame describes exactly one. Elements the model cannot see are
        # dropped *before* anything downstream derives from them, so the image
        # rewrite, the mark numbering and the prompt all describe the same set.
        # On a single-display host every element is inside the frame and this
        # is a no-op.
        if screen_map is not None and raw_ui_elements:
            raw_ui_elements = summaries_within(raw_ui_elements, screen_map.frame)
        # One coordinate space for both perception sources: AX rects arrive in
        # logical points and are rewritten into the image space the model reads
        # coordinates from, so a coordinate picked from either source converts
        # back with the same map.
        ui_elements = self._image_space(raw_ui_elements, screen_map)

        self._observation = Observation(
            frame=frame,
            screenshot_b64=screenshot_b64,
            screen_map=screen_map,
            window=window,
            ui_elements=ui_elements,
            raw_ui_elements=raw_ui_elements,
            content=content,
            open_tabs=open_tabs,
            signature=signature,
            # Marks come from the *logical* summaries: a resolved mark is a
            # click in screen points, and the model's [N] indices line up
            # because the image-space rewrite preserves order and count.
            marks=parse_ax_elements_to_marks(raw_ui_elements),
        )
        active_window = window_summary(window) if window is not None else state.active_window
        return replace(
            state,
            active_window=active_window,
            ui_elements=ui_elements,
            open_tabs=open_tabs,
            screenshot_b64=screenshot_b64 if self.vision_enabled else None,
            observed_trail=_extend_trail(
                state.observed_trail, window, content, TRAIL_MAX_ENTRIES
            ),
        )

    @staticmethod
    def _image_space(
        summaries: tuple[str, ...], screen_map: ScreenMap | None
    ) -> tuple[str, ...]:
        """AX summaries rewritten into the model's image space (pure)."""
        if screen_map is None or screen_map.is_identity or not summaries:
            return summaries
        return summaries_to_image_space(summaries, screen_map)

    def _capture_frame(
        self,
        raw_ui_elements: tuple[str, ...],
    ) -> tuple[ScreenCapture, str | None, ScreenMap] | None:
        """Capture one frame and derive the model's map from it.

        The screenshot the VLM perceives and the map that converts its
        coordinates are produced together, from the same capture, or not at
        all. A capture that cannot yield both is discarded: a screenshot whose
        coordinate space is unknown is worse than none, because every click
        derived from it is confidently wrong.
        """
        sensor = self.sensor
        if sensor is None:
            return None
        try:
            capture = sensor()
        except Exception as exc:  # noqa: BLE001 - perception degradation is recoverable
            if self._screenshot_warned:
                LOGGER.debug("screen capture still failing: %s", exc)
            else:
                self._screenshot_warned = True
                LOGGER.warning("screen capture failed during observe: %s", exc)
            return None
        logical = to_logical_resolution(capture)
        mapped = downscale_to_max_side(logical, SCREENSHOT_MAP_MAX_SIDE)
        screen_map = screen_map_of(logical, mapped)
        if not self.vision_enabled:
            return capture, None, screen_map
        # Set-of-Marks: draw the grounded elements onto the map the model sees,
        # so a numbered line in the AX list and a highlighted region on screen
        # are visibly the same thing. The marks are part of the cache key —
        # identical pixels with a changed element list must be re-encoded, or
        # the model would be handed boxes describing the previous screen.
        marks = (
            parse_ax_elements_to_marks(self._image_space(raw_ui_elements, screen_map))
            if self.set_of_marks_enabled
            else ()
        )
        fingerprint = f"{frame_fingerprint(capture)}|{hash(marks)}"
        if fingerprint == self._last_capture_hash and self._last_screenshot_b64 is not None:
            return capture, self._last_screenshot_b64, screen_map
        screenshot_b64 = capture_to_base64_png(
            annotate_set_of_marks(mapped, marks) if marks else mapped
        )
        self._last_capture_hash = fingerprint
        self._last_screenshot_b64 = screenshot_b64
        return capture, screenshot_b64, screen_map

    # ------------------------------------------------------------------
    # Terminal handling
    # ------------------------------------------------------------------

    def _finish(
        self, state: WorkingState, action: Action, step_label: str, goal: str
    ) -> tuple[WorkingState, bool, bool]:
        """Handle a terminal decision: audit it, advance the plan, or accept it.

        The audit runs *before* any bookkeeping. A completion claim the checker
        rejects must leave the run exactly as it found it — no recorded step, no
        distilled trajectory — so the model's next turn sees an honest history
        rather than one that already says it finished.
        """
        if not isinstance(action, Finish):
            raise RuntimeError(  # noqa: TRY004 - routing invariant, not a caller type error
                f"OODA coding error: finish branch received {type(action).__name__}"
            )
        if action.status == "success":
            rejection = self._audit_completion(state, action)
            if rejection is not None:
                self._last_error = rejection
                LOGGER.warning("ooda finish rejected: %s", rejection)
                return replace(state, last_error=rejection), False, True
        # Terminal decisions are a fresh control boundary: repetition
        # diagnostics are useful while selecting an action, but must not
        # survive a valid completion decision.
        state = replace(
            state,
            completed_steps=state.completed_steps + (step_label,),
            last_error=self._last_error,
            skill=self._skill,
        )
        # Phase 3: when executing a hierarchical plan, a ``finish`` means the
        # CURRENT sub-goal is done — advance to the next one instead of
        # terminating. The run ends only when the plan has no sub-goal left.
        if state.plan is not None:
            advanced = advance_plan(
                state.plan,
                success=action.status == "success",
                error=action.summary if action.status != "success" else None,
            )
            if advanced.current_sub_goal is not None:
                state = replace(state, plan=advanced, skill=self._skill)
                if self.on_sub_goal_complete is not None:
                    self.on_sub_goal_complete(advanced)
                next_sub_goal = advanced.current_sub_goal
                LOGGER.info(
                    "ooda sub-goal complete; next: %r (%s)",
                    next_sub_goal.description,
                    next_sub_goal.success_criteria,
                )
                return state, False, True
            state = replace(state, plan=advanced)

        LOGGER.info("ooda finished goal=%r at step %s", goal, state.step_index)
        # DISTILL: hand the executed trajectory to the caller so a successful
        # run can become a reusable skill and every terminal run is remembered.
        self._finalize(
            state,
            outcome="success" if action.status == "success" else "failure",
            retrospective=action.summary,
        )
        return state, True, False

    def _audit_completion(self, state: WorkingState, finish: Finish) -> str | None:
        """Challenge a success claim; return the rejection reason, or None.

        A model that claims success is the least reliable witness to its own
        success. Two gates apply, cheapest first: the run must have observable
        perception at all, and — when an auditor is configured — a fresh,
        narrowly-scoped re-read of the current screen must agree that the goal
        is satisfied. A rejected claim is folded back as a normal recoverable
        error, so the loop keeps working instead of ending on a fiction.
        """
        if self.sensor is None and self.window_probe is None and self.ax_probe is None:
            # No perception at all: there is nothing to audit against, and
            # inventing a rejection would trap the run. Production wiring
            # always supplies probes (``Agent.run`` fails fast at startup when
            # the sensor is unavailable), so this is the headless/test shape.
            return None
        if self.vision_enabled and not state.screenshot_b64:
            raise RuntimeError(
                "finish verification unavailable: the latest screenshot is missing"
            )
        if self.completion_check is None:
            return None
        if self._rejected_finishes >= MAX_FINISH_REJECTIONS:
            # The auditor and the actor disagree persistently. Accept the
            # model's own verdict rather than looping: the run's honest
            # outcome is recorded in ``last_error`` either way.
            LOGGER.warning(
                "accepting finish after %d rejected completion claims",
                self._rejected_finishes,
            )
            return None
        try:
            verdict = self.completion_check(state, finish.summary)
        except Exception as exc:  # noqa: BLE001 - the auditor must never kill a run
            LOGGER.warning("completion audit unavailable: %s", exc)
            return None
        if verdict.satisfied:
            LOGGER.info("ooda completion audited: %s", verdict.evidence)
            return None
        self._rejected_finishes += 1
        return (
            "completion check rejected this finish: "
            f"{verdict.evidence} "
            "The goal is not yet observably satisfied on screen. Either continue "
            "working toward it, or emit finish with status \"failed\" and explain "
            "what blocked it."
        )

    def _finalize(
        self,
        state: WorkingState,
        *,
        outcome: EpisodeOutcome,
        retrospective: str | None,
    ) -> None:
        """Trigger the DISTILL/remember hooks on a terminal run.

        Every way a run can end reaches here, not just ``finish``. Aborted runs
        used to leave nothing behind at all — a run that worked for twenty
        steps and then exhausted its recovery ladder was as invisible to memory
        as one that never started, so the same wall was walked into again on
        the next attempt. Law 4.1 asks for failure retrospectives precisely
        because that is the trace worth keeping.

        Still gated on at least one executed action: a run that never touched
        the host has no trajectory to remember, and the reason it failed is
        already the caller's exception. What a *failure* trace must never do is
        become a skill; that is the caller's call, and the retrospective is
        passed so it can make it.
        """
        if self.on_complete is None or not self._executed:
            return
        self.on_complete(
            Trajectory(
                app=self.app,
                description=state.goal,
                steps=tuple(self._executed),
                step_descriptions=tuple(self._sub_goals),
            ),
            outcome,
            retrospective,
        )

    @property
    def executed_trajectory(self) -> tuple[Action, ...]:
        """The actions that physically succeeded in the most recent run."""
        return tuple(self._executed)

    # ------------------------------------------------------------------
    # Primitives
    # ------------------------------------------------------------------

    def _pressed_quietly(self, action: Action) -> bool:
        """Try to activate a click's target directly, without the cursor.

        A synthetic click goes into the system event stream: it lands on
        whatever is frontmost and drags the user's real pointer with it, so the
        machine belongs to the agent for the duration of a run. Asking the
        accessibility element under the point to press *itself* has neither
        cost — verified on a real desktop, three keypad presses landed in a
        background Calculator while Chrome stayed frontmost and the cursor
        never moved.

        Opt-in, and a strict fast path: anything other than a plain left click
        goes the ordinary way, and so does a press the element declines. It
        returning ``True`` is not proof the press did anything — a Chromium web
        view answers success and leaves the page untouched — so the action is
        still verified against the screen exactly as before, and a quiet press
        that changed nothing fails and recovers like any other miss.
        """
        if self.quiet_press is None:
            return False
        if not isinstance(action, MouseClick) or action.click_count != 1:
            return False
        if action.button != "left":
            return False
        try:
            return self.quiet_press(Point(action.x, action.y))
        except Exception as exc:  # noqa: BLE001 - the loud path is the fallback
            LOGGER.debug("quiet press unavailable, using a synthetic click: %s", exc)
            return False

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
        # Any physical action invalidates the reused OBSERVE frame: the next
        # pre-action capture must be fresh, never a pre-action snapshot.
        self._physical_since_capture = True
        if cancelled():
            raise KillSwitchTripped("human reclaimed control during physical action")

    def _wait_for_settle(self, before_window: FocusedWindow | None) -> None:
        """Give the host time to render before the after-observation.

        A fixed delay is either too short (a slow page renders after the
        capture, so verification sees a half-drawn frame and calls it
        unchanged) or wasteful (a fast app rendered in 50ms). This polls the
        cheapest available change signal — the focused window's title — and
        returns the moment it moves, falling back to the full budget when
        there is no probe or nothing changes.
        """
        import time

        max_polls = self.settle_max_polls
        interval_s = self.settle_interval_s
        if max_polls <= 0 or interval_s <= 0:
            return
        if self.window_probe is None or before_window is None:
            time.sleep(max_polls * interval_s)
            return
        for _ in range(max_polls):
            time.sleep(interval_s)
            try:
                current = self.window_probe()
            except Exception as exc:  # noqa: BLE001 - probe is best-effort
                LOGGER.debug("window probe failed during settle poll: %s", exc)
                continue
            if (current.app_name, current.window_title) != (
                before_window.app_name,
                before_window.window_title,
            ):
                return  # The host moved on — no need to wait out the budget.

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
        """Law 3 Stage 2: load the requested skill's full definition."""
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
        """Two-stage skill retrieval before a decision (Law 3).

        Stage 1 scans the summary index with the goal as the query; Stage 2
        loads the top *same-app* match (skills are app-scoped — another app's
        workflow must not mount) and carries its full instructions into the
        provider context. Runs at most once per run: after a mount, only an
        explicit ``load_skill`` swaps the mounted skill (or repeated failures
        unmount it). Best-effort: a scan or load failure degrades to the
        unmounted context with a warning, never aborts the workflow.
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
        return replace(state, skill=self._skill)


class UnknownMarkError(RuntimeError):
    """A ``click_mark`` named an index that is not in the current element list.

    Recoverable like any other bad decision: the list is re-derived every turn
    (elements appear, disappear and renumber as the screen changes), so the
    honest answer is to re-read it, not to click the nearest plausible thing.
    """

    def __init__(self, *, requested: int, available: int) -> None:
        self.requested = requested
        self.available = available
        listed = f"1-{available}" if available else "none are listed right now"
        super().__init__(
            f"click_mark referred to mark {requested}, which is not in the "
            f"current AX element list ({listed}). The list is re-derived every "
            "turn, so re-read it and use a number it actually shows — or click "
            "a coordinate from the screenshot if the target is not listed"
        )


def failure_retrospective(exc: BaseException) -> str:
    """A short, honest account of why a run ended without finishing (pure).

    Stored on the failed episode, so a later attempt at the same goal can be
    told what stopped the last one. Named by cause rather than by exception
    class: "recovery exhausted (consent_missing)" tells a human what to fix,
    while "UnrecoverableFailureError" only tells them what raised.
    """
    if isinstance(exc, MaxStepsError):
        return f"run truncated by the step budget: {exc}"
    if isinstance(exc, KillSwitchTripped):
        return f"human reclaimed control: {exc}"
    if isinstance(exc, BudgetExceededError):
        return f"run stopped by its budget: {exc}"
    if isinstance(exc, UnrecoverableFailureError):
        return f"recovery exhausted ({exc.failure.kind.value}): {exc}"
    return f"run ended abnormally: {exc}"


class KillSwitchTripped(RuntimeError):
    """Raised when the human reclaims control of the physical host (Law 5).

    Raised by the OODA loop and physical drivers when a kill-switch trips; a
    caller distinguishes a user takeover from a generic action failure.
    """


class StuckLoopError(RuntimeError):
    """The provider repeated one physical action with no observable progress.

    Raised *before* the repeat that would exceed ``REPEAT_ABORT_AFTER``, so the
    host never sees more than ``REPEAT_ABORT_AFTER - 1`` executions of the same
    intent. This is a recoverable failure, not a verdict on the run: it enters
    the recovery ladder, where the model is told — with escalating firmness —
    to change its approach. Only a model that keeps insisting reaches
    :class:`~computeruse.orchestrator.failures.UnrecoverableFailureError`, which
    is what guarantees the loop terminates against a degenerate provider.
    """

    def __init__(self, *, action: Action, repeats: int, goal: str) -> None:
        self.action = action
        self.repeats = repeats
        self.goal = goal
        super().__init__(
            f"stuck loop: the agent repeated the identical {action.type} action "
            f"{repeats} times for goal={goal!r} with nothing changing on screen; "
            "this repeat was refused before it reached the host"
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


class FocusLostError(RuntimeError):
    """The run's target application is no longer frontmost.

    Every coordinate the model reads off a screenshot describes the window
    that was on top when the frame was taken. Once focus drifts — a dialog, a
    notification, the user clicking away — those coordinates address a
    different window, and clicking them is worse than doing nothing. The loop
    re-asserts the target app once and raises this only when that fails.
    """


class StaleObservationError(RuntimeError):
    """The screen moved between the decision and its actuation.

    The model's turn takes seconds, during which a page can finish loading or
    a dialog can appear. Acting on the pre-change layout puts the click
    wherever the new content happens to sit. Folded into ``last_error`` so the
    next turn decides from the frame that actually exists.
    """


class SemanticVerificationFailedError(RuntimeError):
    """A text action was ACKed but the focused field shows no trace of it.

    The AXValue postcondition contradicted the intended outcome: the focused
    input's value is non-empty yet lacks the typed/pasted text. Retained as a
    distinct type so a caller can tell "the text went to the wrong field" from
    a generic verification miss.
    """


class VerificationFailedError(RuntimeError):
    """Every available witness contradicted an action's expected effect.

    Not a pixel verdict: the loop asks pixels, the AX surface, the focused
    field's value, and the frontmost app, and raises only when the witnesses
    that could speak all reported the expected change did *not* happen. A
    witness that abstains never contributes to this failure, which is what
    makes the check safe to run on every action instead of a narrow subset.
    """
