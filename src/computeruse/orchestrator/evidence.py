"""Evidence-based action verification (Law 2 VERIFY, done honestly).

The old ORIENT step asked one question — "did pixels in a 48pt square move?" —
and treated the answer as proof. That is wrong in both directions, and both
were observable in real runs:

* **False failures.** A click that focuses a text field moves a one-pixel
  caret; a click on an already-highlighted row may move nothing at all. The
  pixel diff reported "the action did not land", that lie went into
  ``last_error``, and the model abandoned a target it had actually hit.
* **False successes.** A whole-frame hash "verifies" a navigation because a
  clock ticked, a notification badge changed, or a video advanced one frame.

The fix is not a better threshold — it is more than one witness. This module
is the pure core that decides what an action was *supposed* to make true
(:func:`expectation_for`) and how to weigh the witnesses that report back
(:func:`combine`). Three rules make it robust:

1. **Absence of evidence is not evidence of absence.** A witness that cannot
   speak returns :attr:`Evidence.INCONCLUSIVE`, which never fails an action.
2. **One confirmation is enough.** Any witness that positively confirms the
   expected change outweighs silent ones.
3. **Contradiction needs a quorum.** Witnesses come in two strengths. A
   *direct* one contradicts a specific claim — the text you inserted is absent
   from a non-empty field, the app you activated is not frontmost — and one is
   conclusive on its own. A *circumstantial* one only reports that nothing
   changed, which is weak evidence: plenty of legitimate actions change nothing
   observable. Two independent circumstantial witnesses must agree before an
   action is called a miss, so neither a stubborn pixel diff nor a static AX
   tree can veto an action by itself.

Pure throughout: the runner gathers the witnesses, this module judges them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final, Literal

from computeruse.orchestrator.schemas import (
    Action,
    ActivateApp,
    ClipboardPaste,
    MouseClick,
    MouseDrag,
    MouseScroll,
    PressHotkey,
    TypeText,
)
from computeruse.vision.ax import summary_covering
from computeruse.vision.coordinates import Point

#: Hotkeys whose effect is a page-wide transition rather than a local change.
#: Return submits; Escape dismisses. Everything else (Cmd+L, Cmd+C, Cmd+A) is a
#: setup step whose effect is verified by the action that follows it.
_PAGE_TRANSITION_KEYS: frozenset[str] = frozenset({"return", "enter", "escape"})

PixelScope = Literal["region", "frame", "none"]


class Evidence(str, Enum):
    """One witness's report about whether an action landed."""

    #: The witness observed the expected change.
    CONFIRMED = "confirmed"
    #: The witness observed that the expected change did NOT happen.
    CONTRADICTED = "contradicted"
    #: The witness could not tell (no probe, no signal, redacted value).
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class ActionExpectation:
    """What should become observably true after one action (pure).

    The runner reads this to know which witnesses are worth calling: there is
    no point probing a text field after a scroll, and no point diffing a 48pt
    square after a page navigation.
    """

    #: Which pixel comparison, if any, is meaningful for this action.
    pixel: PixelScope
    #: Centre of the local pixel region ("region" scope only).
    region_point: Point | None
    #: Whether the host's UI state (AX focus/values/titles) should differ.
    expects_ui_change: bool
    #: Screen point whose element should hold keyboard focus afterwards. This
    #: is the witness that makes an *idempotent* action verifiable: clicking an
    #: already-selected tab, an already-checked box, or a button that is
    #: already focused changes nothing, and change-detection alone cannot tell
    #: that apart from a miss.
    focus_target: Point | None
    #: Text that must appear in the focused field afterwards, if any.
    expected_text: str | None
    #: Application that must own the frontmost window afterwards, if any.
    expected_app: str | None
    #: Whether the host needs a settle delay before the after-observation.
    needs_settle: bool

    @property
    def is_verifiable(self) -> bool:
        """Whether any witness at all can speak about this action."""
        return (
            self.pixel != "none"
            or self.expects_ui_change
            or self.focus_target is not None
            or self.expected_text is not None
            or self.expected_app is not None
        )


def expectation_for(action: Action) -> ActionExpectation:
    """The observable postcondition of an action (pure).

    Deliberately conservative: an action whose effect is genuinely invisible
    (a bare modifier hotkey, a clipboard copy) declares *no* expectation, so
    the loop does not invent a verification it cannot perform and then fail it.
    """
    if isinstance(action, MouseClick):
        # A click either changes pixels near the target or moves keyboard
        # focus/AX state — often exactly one of the two, never reliably both.
        return ActionExpectation(
            pixel="region",
            region_point=Point(action.x, action.y),
            expects_ui_change=True,
            focus_target=Point(action.x, action.y),
            expected_text=None,
            expected_app=None,
            needs_settle=True,
        )
    if isinstance(action, MouseDrag):
        return ActionExpectation(
            pixel="region",
            region_point=Point(action.end_x, action.end_y),
            expects_ui_change=True,
            focus_target=None,
            expected_text=None,
            expected_app=None,
            needs_settle=True,
        )
    if isinstance(action, MouseScroll):
        # Scrolling has no point target; the viewport as a whole is the
        # observable, and a scroll that reveals nothing new is real evidence
        # that the cursor is not over a scrollable area.
        return ActionExpectation(
            pixel="frame",
            region_point=None,
            expects_ui_change=True,
            focus_target=None,
            expected_text=None,
            expected_app=None,
            needs_settle=True,
        )
    if isinstance(action, PressHotkey):
        if action.key.lower().strip() in _PAGE_TRANSITION_KEYS:
            return ActionExpectation(
                pixel="frame",
                region_point=None,
                expects_ui_change=True,
                focus_target=None,
                expected_text=None,
                expected_app=None,
                needs_settle=True,
            )
        return _NO_EXPECTATION
    if isinstance(action, (TypeText, ClipboardPaste)):
        # The authoritative witness is the focused field's AXValue; pixels are
        # a poor judge of text (a caret alone can move them, and a rendered
        # glyph can be indistinguishable at map resolution).
        return ActionExpectation(
            pixel="none",
            region_point=None,
            expects_ui_change=False,
            focus_target=None,
            expected_text=action.text or None,
            expected_app=None,
            needs_settle=True,
        )
    if isinstance(action, ActivateApp):
        return ActionExpectation(
            pixel="none",
            region_point=None,
            expects_ui_change=False,
            focus_target=None,
            expected_text=None,
            expected_app=action.app,
            needs_settle=True,
        )
    return _NO_EXPECTATION


_NO_EXPECTATION = ActionExpectation(
    pixel="none",
    region_point=None,
    expects_ui_change=False,
    focus_target=None,
    expected_text=None,
    expected_app=None,
    needs_settle=False,
)


#: Circumstantial witnesses must corroborate each other before their silence
#: counts as a miss. One is not enough: a click that only moved keyboard focus
#: leaves pixels unchanged, and a click that only repainted a hover state
#: leaves the AX surface unchanged — either alone would fail a valid action.
CIRCUMSTANTIAL_QUORUM: Final[int] = 2


def combine(
    *,
    direct: tuple[Evidence, ...],
    circumstantial: tuple[Evidence, ...],
) -> Evidence:
    """Fold witness reports into one verdict, weighted by strength (pure).

    * Any :attr:`Evidence.CONFIRMED` wins — one positive observation is proof
      the action landed, whatever the silent witnesses think.
    * A *direct* contradiction ("the text is not in the field", "the app is not
      frontmost") is conclusive alone: it denies a specific claim rather than
      merely reporting stillness.
    * A *circumstantial* contradiction ("nothing changed") needs
      :data:`CIRCUMSTANTIAL_QUORUM` independent witnesses to agree. This is the
      rule that stops the loop from inventing failures: with a single available
      witness the honest answer is "I could not tell".
    * Otherwise the verdict is :attr:`Evidence.INCONCLUSIVE`, which callers
      must treat as "not verified", never as "failed".
    """
    if Evidence.CONFIRMED in direct or Evidence.CONFIRMED in circumstantial:
        return Evidence.CONFIRMED
    if Evidence.CONTRADICTED in direct:
        return Evidence.CONTRADICTED
    denials = sum(1 for report in circumstantial if report is Evidence.CONTRADICTED)
    if denials >= CIRCUMSTANTIAL_QUORUM:
        return Evidence.CONTRADICTED
    return Evidence.INCONCLUSIVE


def ui_state_evidence(before: tuple[str, ...], after: tuple[str, ...]) -> Evidence:
    """Did the host's AX surface change between two observations (pure)?

    The AX summaries encode roles, titles, values and focus markers, so a
    click that only moved keyboard focus — invisible to a pixel diff at map
    resolution — still shows up here. Two empty snapshots mean the probe is
    unavailable, which is silence, not a contradiction.

    Circumstantial: an unchanged surface is weak evidence of a miss, because
    many legitimate actions leave the accessibility tree untouched.
    """
    if not before and not after:
        return Evidence.INCONCLUSIVE
    return Evidence.CONFIRMED if before != after else Evidence.CONTRADICTED


def ax_surface_evidence(
    before_elements: tuple[str, ...],
    after_elements: tuple[str, ...],
    before_content: tuple[str, ...],
    after_content: tuple[str, ...],
) -> Evidence:
    """Did the accessibility surface change at all — structure or text (pure)?

    Deliberately **one** witness over two signals, not two witnesses. Both are
    read from the same AX snapshot, so treating them as independent would let a
    single silent source cast two votes — and a failure needs only two
    corroborating circumstantial witnesses to agree. That would make every
    action the AX probe cannot see look decisively failed.

    Either signal moving confirms: an element list changing catches focus
    moves and appearing controls, and the content digest catches text that
    changes without any structural change at all — a calculator display, a
    status line, a result count. Only both being silent, with something to
    compare, contradicts.
    """
    element_verdict = ui_state_evidence(before_elements, after_elements)
    content_verdict = content_evidence(before_content, after_content)
    if Evidence.CONFIRMED in (element_verdict, content_verdict):
        return Evidence.CONFIRMED
    if Evidence.CONTRADICTED in (element_verdict, content_verdict):
        return Evidence.CONTRADICTED
    return Evidence.INCONCLUSIVE


def content_evidence(before: tuple[str, ...], after: tuple[str, ...]) -> Evidence:
    """Did the app's visible text change between two observations (pure)?

    The witness for the most common effect an action has and the one every
    other witness misses. A calculator display updating, a status line, a
    result count, a page title, a field's contents — none of it moves the
    interactive element list, and a few glyphs redrawing is far below the
    fraction-of-pixels threshold a pixel diff needs to call a region changed.

    Circumstantial: text can change without the agent touching anything (a
    clock, a progress counter), so on its own an unchanged digest is weak
    evidence of a miss — but a *changed* one is a strong sign the action landed.
    """
    if not before and not after:
        return Evidence.INCONCLUSIVE
    return Evidence.CONFIRMED if before != after else Evidence.CONTRADICTED


def target_focus_evidence(target: Point | None, summaries: tuple[str, ...]) -> Evidence:
    """Does the element you aimed at now hold keyboard focus (pure)?

    The witness that makes *idempotent* actions verifiable. Clicking a control
    that is already in its target state — an already-focused button, a selected
    tab, a checked box — changes nothing observable, and every change-detecting
    witness therefore reports a miss. Observed in a real run: the model clicked
    the right button, the click landed, and it was told twice that it had
    failed; it then abandoned a correct approach for a worse one.

    Positive-only by construction. Focus landing on the element under the
    click is proof the click reached it; focus *not* landing there proves
    nothing, because plenty of controls (links, many buttons) never take focus
    at all. So this returns CONFIRMED or INCONCLUSIVE, never CONTRADICTED —
    it can rescue an action from a false failure but can never cause one.
    """
    if target is None or not summaries:
        return Evidence.INCONCLUSIVE
    line = summary_covering(summaries, target.x, target.y)
    if line is None:
        return Evidence.INCONCLUSIVE
    return Evidence.CONFIRMED if line.endswith("(focused)") else Evidence.INCONCLUSIVE


def text_evidence(expected: str, observed: str | None) -> Evidence:
    """Does the focused field's value corroborate an insertion (pure)?

    Direct: a non-empty field that does not contain the inserted text denies a
    specific claim, so one such report is conclusive.

    ``observed is None`` (no focused text element) and an empty value are both
    silence: many apps do not publish AXValue at all, and failing a valid
    paste because the app is quiet is exactly the false failure this module
    exists to prevent. A non-empty value that lacks the text is conclusive.
    """
    if observed is None or not observed:
        return Evidence.INCONCLUSIVE
    return Evidence.CONFIRMED if expected in observed else Evidence.CONTRADICTED


#: Shortest single token specific enough to name an application on its own.
#: "chrome" (6) identifies one; "mail" (4) is a word that appears inside a
#: dozen application names, and treating it as a match is how "Mail" confirmed
#: "Gmail".
APP_TOKEN_MIN_CHARS: Final[int] = 6


def _app_tokens(text: str) -> frozenset[str]:
    """Word tokens of an application name, punctuation folded (pure)."""
    return frozenset(
        token for token in re.sub(r"[^\w]+", " ", text.casefold()).split() if token
    )


def _names_one_app(subset: frozenset[str]) -> bool:
    """Is this token subset specific enough to identify an application (pure)?

    Two tokens are, because two words in common is not a coincidence between
    unrelated app names. One token is only when it is long: LaunchServices and
    the accessibility API routinely disagree about the same app ("Chrome" vs
    "Google Chrome"), so a subset match has to be allowed — but "Notes" inside
    "Google Chrome — Meeting Notes" is a different application entirely.
    """
    if len(subset) >= 2:
        return True
    return len(subset) == 1 and len(next(iter(subset))) >= APP_TOKEN_MIN_CHARS


def app_evidence(
    expected: str, observed: str | None, bundle_id: str | None = None
) -> Evidence:
    """Does the frontmost application match the one an activation requested?

    Direct: naming the wrong frontmost application denies the activation
    outright, so one such report is conclusive.

    Matching is case-insensitive and accepts either name containing the other,
    because LaunchServices names ("Google Chrome") and AX titles ("Chrome")
    routinely disagree about the same application.

    ``bundle_id`` is checked as an independent identity, and it is the one that
    survives translation. macOS shows apps under localized names: on a Turkish
    desktop Calculator is "Hesap Makinesi", which shares no substring with
    "Calculator". Comparing names alone, an agent asked to work in Calculator
    concluded a different app was in front of it and refused to act — while
    activating the name it *could* see failed too, because no bundle on disk
    carries the translated name. Accepting a bundle-id match ends that
    deadlock: "com.apple.calculator" is the same string in every language, and
    the caller may pass either identity as ``expected``.
    """
    if bundle_id:
        wanted = expected.casefold()
        bundle = bundle_id.casefold()
        if bundle == wanted or bundle.rsplit(".", 1)[-1] == wanted.replace(" ", ""):
            return Evidence.CONFIRMED
    if observed is None or not observed:
        return Evidence.INCONCLUSIVE
    # Token-based matching, not bidirectional substring: "notes" must not
    # confirm "Meeting Notes" in another app, and "Mail" must not confirm
    # "Gmail" — but "Chrome" must still confirm "Google Chrome" (LaunchServices
    # and AX routinely disagree about the same app). Rule: a subset in either
    # direction confirms only when it is specific — two or more tokens, one
    # long token (>=6 chars, e.g. "chrome"), or exact string equality.
    # Anything weaker is INCONCLUSIVE, which the quorum logic carries safely.
    expected_tokens = _app_tokens(expected)
    observed_tokens = _app_tokens(observed)
    if not expected_tokens or not observed_tokens:
        return Evidence.INCONCLUSIVE
    if expected_tokens == observed_tokens:
        return Evidence.CONFIRMED
    if expected_tokens <= observed_tokens:
        if _names_one_app(expected_tokens):
            return Evidence.CONFIRMED
        return Evidence.INCONCLUSIVE
    if observed_tokens <= expected_tokens:
        if _names_one_app(observed_tokens):
            return Evidence.CONFIRMED
        return Evidence.INCONCLUSIVE
    return Evidence.CONTRADICTED


def verification_diagnostic(
    action_type: str,
    expectation: ActionExpectation,
    reports: tuple[tuple[str, Evidence], ...],
    element_at_target: str | None = None,
) -> str:
    """The LLM-facing message when every witness contradicted an action (pure).

    Names each witness and what it saw, so the model can tell "the pixels did
    not move" from "the app never came to the front" — different problems with
    different fixes.

    ``element_at_target`` is the accessibility summary of whatever sits under
    the click, and it changes the diagnosis completely. "Nothing changed" has
    two causes that need opposite responses: the click missed, or the click
    landed on a control that was already in the state being asked for. Telling
    a model that hit the right button to "re-derive the target" sends it
    hunting coordinates that were correct — observed on Calculator, where
    pressing Clear on an already-clear display and pressing Equals on an
    already-computed result were both reported as misses, and the agent spent
    four steps chasing a coordinate problem it did not have.
    """
    where = ""
    if expectation.region_point is not None:
        point = expectation.region_point
        where = f" at ({point.x:.0f},{point.y:.0f})"
    detail = ", ".join(f"{name}={verdict.value}" for name, verdict in reports)
    if element_at_target is not None:
        return (
            f"action verification failed: {action_type}{where} landed on "
            f"{element_at_target} but produced no observable change ({detail}); "
            "the coordinate was right, so do NOT re-aim. Either that control "
            "is already in the state you want — check whether the goal is "
            "satisfied and move on — or it needs a different interaction"
        )
    return (
        f"action verification failed: {action_type}{where} produced no "
        f"observable change ({detail}); the action did not land where you "
        "aimed — re-derive the target from the current screen before retrying"
    )


@dataclass(frozen=True)
class CompletionVerdict:
    """The answer to "is the goal observably satisfied right now?" (pure data).

    Produced by an auditor that re-reads the *current* screen with the goal in
    hand, independently of the acting context that produced the claim. Keeping
    the evidence string mandatory is deliberate: a bare boolean lets a model
    assert completion, while a sentence describing what is visible on screen
    can be folded straight back into ``last_error`` when the claim is rejected.
    """

    satisfied: bool
    evidence: str
