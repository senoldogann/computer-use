"""Failure taxonomy and the bounded recovery ladder (Law 2 RECOVER).

The OODA loop's old recovery was a single string folded into ``last_error``:
every failure looked the same to the model, so it could only guess whether to
retry the same thing, try something else, or stop. Two failure modes followed
directly from that:

* **The same error forever.** Nothing counted *failed* attempts, so an action
  that raised on every turn (a phantom coordinate, a dead probe) burned the
  whole step budget repeating itself. The stuck-loop guard only ever saw
  *successful* actions, so it never fired.
* **Aborting too early.** The one guard that did fire killed the run outright
  after three repeats, with no intermediate "try a different approach" stage.

This module is the pure core of the fix. :func:`classify_failure` turns an
exception into a typed :class:`Failure` with a stable ``signature``; the
runner counts consecutive failures per signature and asks
:func:`recovery_for` what to do next. The ladder is deliberately short and
finite — retry, then force an alternate approach, then force a replan, then
abort — so the loop always makes progress *or* terminates, and never sits in
between.

Everything here is pure: no I/O, no clock, no OS. The runner owns the counters.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

from computeruse.orchestrator.schemas import (
    Action,
    ActivateApp,
    ClipboardPaste,
    MouseClick,
    MouseDrag,
    PressHotkey,
    TypeText,
)


class FailureKind(str, Enum):
    """What *category* of thing went wrong, independent of the message.

    The kind drives recovery: a coordinate the model invented needs a fresh
    look at the screen, while a driver that timed out needs a wait, and a
    contract violation needs a different action shape entirely.
    """

    #: The model named a point outside the observed display.
    COORDINATE = "coordinate"
    #: The action was ACKed but no observable change corroborated it.
    VERIFICATION = "verification"
    #: Typed/pasted text is provably not where the agent believes it went.
    TEXT_PLACEMENT = "text_placement"
    #: The target application is no longer frontmost.
    FOCUS = "focus"
    #: The screen changed materially between OBSERVE and actuation.
    STALE = "stale"
    #: The same action was repeated with nothing observable changing.
    REPETITION = "repetition"
    #: The driver refused the request (bad params, unsupported key, ...).
    DRIVER_REJECTED = "driver_rejected"
    #: The driver could not be reached, or the call timed out.
    DRIVER_UNAVAILABLE = "driver_unavailable"
    #: The model never produced a decision matching the action contract.
    MODEL_CONTRACT = "model_contract"
    #: Anything not recognised — treated conservatively.
    UNKNOWN = "unknown"


class RecoveryAction(str, Enum):
    """What the loop should do about a failure that keeps repeating."""

    #: Fold the diagnostic in and let the model try again as it sees fit.
    RETRY = "retry"
    #: Demand a materially different approach (different target or method).
    ALTERNATE = "alternate"
    #: Discard the current tactic wholesale: unmount skills, restate the
    #: strategy from the current screen rather than the original plan.
    REPLAN = "replan"
    #: Stop the run; further attempts cannot succeed.
    ABORT = "abort"


# The ladder. Index by the number of *consecutive* failures carrying the same
# signature: the first failure gets a plain retry, the second demands a
# different approach, the third forces a full replan, and the fourth ends the
# run. Short by design — a model that cannot get past one obstacle in four
# tries is not going to on the fifth, and a physical host is not a free
# retry budget.
RETRY_BEFORE_ALTERNATE: Final[int] = 1
RETRY_BEFORE_REPLAN: Final[int] = 2
RETRY_BEFORE_ABORT: Final[int] = 4

# Consecutive failures of *any* kind that end a run. A model that fails four
# different ways in a row is lost in a way no single-signature ladder catches.
MAX_CONSECUTIVE_FAILURES: Final[int] = 6

# Pointer coordinates within this many logical points belong to the same
# failure signature: a model retrying "the same wrong click" jitters its
# coordinates a few pixels, and a signature that changed on every pixel would
# reset the ladder forever (the exact hole the old guard had).
SIGNATURE_TOLERANCE_PX: Final[int] = 32


@dataclass(frozen=True)
class Failure:
    """One classified failure, with the identity used to count repeats."""

    kind: FailureKind
    #: The underlying exception's message — carried verbatim into the hint so
    #: the model sees the real reason, never a sanitised summary.
    message: str
    #: The action type that failed ("mouse_click", ...); "none" when the
    #: failure did not originate from an action (e.g. a model contract miss).
    action_type: str
    #: Coarse target identity, so a retry of the same intent counts as a
    #: repeat while a genuinely different target starts a fresh ladder.
    target: str

    @property
    def signature(self) -> str:
        """Stable identity of "this same failure again" (pure)."""
        return f"{self.kind.value}:{self.action_type}:{self.target}"


def _target_of(action: Action | None) -> str:
    """Coarse, jitter-tolerant identity of an action's target (pure)."""
    if action is None:
        return "-"
    if isinstance(action, MouseClick):
        return _bucketed_point(action.x, action.y)
    if isinstance(action, MouseDrag):
        return f"{_bucketed_point(action.start_x, action.start_y)}>{_bucketed_point(action.end_x, action.end_y)}"
    if isinstance(action, (TypeText, ClipboardPaste)):
        # The text itself is the target: retyping the same string into the
        # same failing field is a repeat; a different string is new work.
        return action.text[:48]
    if isinstance(action, PressHotkey):
        return "+".join([*sorted(action.modifiers), action.key.lower()])
    if isinstance(action, ActivateApp):
        return action.app.casefold()
    return action.type


def _bucketed_point(x: int, y: int) -> str:
    """Quantize a coordinate to the signature tolerance grid (pure)."""
    return f"({x // SIGNATURE_TOLERANCE_PX},{y // SIGNATURE_TOLERANCE_PX})"


def classify_failure(exc: BaseException, action: Action | None) -> Failure:
    """Map an exception raised during a step onto a typed failure (pure).

    Classification is by exception *type name* rather than by isinstance
    chains against every module, so this stays a leaf with no import cycle
    back into the loop, the client, or the vision layer. Unrecognised types
    fall to :attr:`FailureKind.UNKNOWN`, which the ladder treats exactly like
    any other recoverable failure — conservative, never silently ignored.
    """
    name = type(exc).__name__
    kinds: dict[str, FailureKind] = {
        "CoordinateOutOfBoundsError": FailureKind.COORDINATE,
        "VerificationFailedError": FailureKind.VERIFICATION,
        "SemanticVerificationFailedError": FailureKind.TEXT_PLACEMENT,
        "FocusLostError": FailureKind.FOCUS,
        "StaleObservationError": FailureKind.STALE,
        "StuckLoopError": FailureKind.REPETITION,
        "DriverRpcError": FailureKind.DRIVER_REJECTED,
        "DriverConnectionError": FailureKind.DRIVER_UNAVAILABLE,
        "DriverTimeoutError": FailureKind.DRIVER_UNAVAILABLE,
        "InvalidDecisionError": FailureKind.MODEL_CONTRACT,
    }
    kind = kinds.get(name, FailureKind.UNKNOWN)
    return Failure(
        kind=kind,
        message=f"{name}: {exc}",
        action_type=action.type if action is not None else "none",
        target=_target_of(action),
    )


def recovery_for(streak: int) -> RecoveryAction:
    """Which rung of the ladder a repeat count lands on (pure).

    ``streak`` is the number of consecutive failures carrying one signature,
    counting the one that just happened (so the first failure is ``1``).
    """
    if streak <= 0:
        raise ValueError(f"streak must be positive, got {streak}")
    if streak <= RETRY_BEFORE_ALTERNATE:
        return RecoveryAction.RETRY
    if streak <= RETRY_BEFORE_REPLAN:
        return RecoveryAction.ALTERNATE
    if streak < RETRY_BEFORE_ABORT:
        return RecoveryAction.REPLAN
    return RecoveryAction.ABORT


# Per-kind tactical guidance. Generic "try something else" advice sends a
# model in circles; naming the *mechanism* that failed tells it which lever to
# reach for. Kept as data so the text is testable without a runner.
_KIND_GUIDANCE: Final[dict[FailureKind, str]] = {
    FailureKind.COORDINATE: (
        "The coordinate does not exist on this display. Re-read the target's "
        "position from the CURRENT screenshot and report it in image-map "
        "coordinates exactly as it appears there — never carry a coordinate "
        "over from an earlier turn, and never do scale math."
    ),
    FailureKind.VERIFICATION: (
        # Deliberately does not name a cause. "Nothing changed" has two, and
        # the diagnostic this is appended to already says which — it reads the
        # accessibility element under the point. Asserting "the action did not
        # land where you aimed" here contradicted that diagnostic whenever the
        # coordinate had in fact been right, so a live run was told in one
        # breath not to re-aim and that it had aimed wrong.
        "The action ran and nothing observable changed, which has two causes. "
        "The diagnosis above says which: if it named the control you meant, do "
        "not re-aim — that control is either already in the state you want, or "
        "it needs a different interaction. If it named nothing, the target is "
        "off-screen, covered by an overlay, or not where you read it: scroll it "
        "into view, dismiss the overlay, or reach the same destination another "
        "way (e.g. the URL bar instead of a link)."
    ),
    FailureKind.TEXT_PLACEMENT: (
        "The text went somewhere other than the field you intended. Click the "
        "field first, confirm it shows as focused in the AX elements, clear it "
        "(Cmd+A), and only then insert the text."
    ),
    FailureKind.FOCUS: (
        "The target application is not frontmost, so your coordinates refer to "
        "a window that is no longer on top. Bring it back with activate_app "
        "before doing anything positional."
    ),
    FailureKind.STALE: (
        "The screen changed after the screenshot you decided from, so your "
        "coordinates describe a layout that no longer exists. Decide again "
        "from the new screenshot."
    ),
    FailureKind.REPETITION: (
        "You have repeated this action with nothing changing on screen. The "
        "target is not where you think it is, or the element does not respond "
        "to this interaction. Do something structurally different: scroll to "
        "reveal the real target, reach the destination by another route, or "
        "finish if the goal is already met."
    ),
    FailureKind.DRIVER_REJECTED: (
        "The input layer refused this request as malformed or unsupported. Use "
        "a different action shape — e.g. a supported key name, or clipboard_paste "
        "instead of type_text."
    ),
    FailureKind.DRIVER_UNAVAILABLE: (
        "The input layer did not respond in time. Wait briefly for the host to "
        "settle, then retry a smaller action (shorter text, one step at a time)."
    ),
    FailureKind.MODEL_CONTRACT: (
        "Your reply did not match the action contract. Emit exactly one JSON "
        "object with the required fields and one supported action type."
    ),
    FailureKind.UNKNOWN: (
        "The step failed for a reason the orchestrator could not classify. "
        "Re-observe the screen and choose the simplest action that makes "
        "progress from what you can actually see."
    ),
}

_RUNG_DIRECTIVE: Final[dict[RecoveryAction, str]] = {
    RecoveryAction.RETRY: (
        "This is the first failure of its kind — a corrected retry is fine."
    ),
    RecoveryAction.ALTERNATE: (
        "This has now failed twice in a row. Do NOT repeat the same action "
        "with adjusted coordinates. Change the METHOD: a different UI path, a "
        "keyboard shortcut, a URL, or scrolling to reveal the real target."
    ),
    RecoveryAction.REPLAN: (
        "This has failed three times in a row. Abandon the current tactic "
        "entirely. Describe, from the CURRENT screenshot only, the shortest "
        "remaining route to the goal and take its first step. If the goal is "
        "already satisfied on screen, emit finish now; if it cannot be reached "
        "from here, emit finish with status \"failed\" and say why."
    ),
    RecoveryAction.ABORT: (
        "This failure is unrecoverable within the configured budget."
    ),
}


def recovery_hint(failure: Failure, streak: int) -> str:
    """The LLM-facing diagnostic for a failure at its current ladder rung (pure).

    Three parts, always in the same order: what actually happened (the raw
    message), why that mechanism fails and which lever to reach for instead,
    and how hard the loop is now insisting on a change of approach. A model
    that only reads the first line still gets the truth; one that reads all
    three gets a plan.
    """
    rung = recovery_for(streak)
    repeat = (
        f" (failure {streak} of this kind in a row)" if streak > 1 else ""
    )
    return (
        f"{failure.message}{repeat}. "
        f"{_KIND_GUIDANCE[failure.kind]} "
        f"{_RUNG_DIRECTIVE[rung]}"
    )


class UnrecoverableFailureError(RuntimeError):
    """The recovery ladder reached :attr:`RecoveryAction.ABORT`.

    Raised instead of letting the loop keep spending a physical host on an
    obstacle it has already failed to pass four times (or six times in a row
    across different obstacles). Carries the classified failure so a caller
    reports *what* could not be recovered, not just "the run ended".
    """

    def __init__(self, *, failure: Failure, streak: int, goal: str) -> None:
        self.failure = failure
        self.streak = streak
        self.goal = goal
        super().__init__(
            f"unrecoverable {failure.kind.value} failure after {streak} "
            f"consecutive attempts on goal={goal!r}: {failure.message}"
        )
