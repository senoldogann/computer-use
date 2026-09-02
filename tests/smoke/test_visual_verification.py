"""Evidence-based action verification and the recovery ladder (Law 2).

The driver ACKs a click that landed on nothing, and a pixel diff alone answers
the wrong question — it fails a click that only moved keyboard focus, and it
passes a navigation that never happened because a clock ticked. These tests pin
the replacement contract:

* several independent witnesses (pixels, AX surface, focused-field value,
  frontmost app) are polled per action;
* one confirming witness outweighs silent ones, and silence never fails an
  action;
* only unanimous contradiction raises, folds into ``last_error`` for the next
  provider turn, and keeps the failed step out of ``completed_steps`` (F2);
* a failure that keeps repeating climbs a bounded ladder and ends the run
  rather than consuming the whole step budget.

Frames are hand-built (all-black BGRA, optionally with a white region), so the
whole path is deterministic and offline.
"""

from __future__ import annotations

import pytest

from computeruse.orchestrator.evidence import (
    Evidence,
    app_evidence,
    ax_surface_evidence,
    combine,
    expectation_for,
    target_focus_evidence,
    text_evidence,
    ui_state_evidence,
    verification_diagnostic,
)
from computeruse.orchestrator.failures import (
    FailureKind,
    RecoveryAction,
    UnrecoverableFailureError,
    classify_failure,
    recovery_for,
    recovery_hint,
)
from computeruse.orchestrator.loop import (
    AxProbeResult,
    OodaRunner,
    WorkingState,
    target_point_of,
    verification_region,
)
from computeruse.orchestrator.schemas import (
    ActivateApp,
    AgentTurn,
    ClipboardPaste,
    Finish,
    MouseClick,
    MouseDrag,
    MouseMove,
    MouseScroll,
    PressHotkey,
    TypeText,
)
from computeruse.vision import ScreenCapture
from computeruse.vision.coordinates import Point, Rect, Size
from computeruse.vision.focus import FocusedWindow

FRAME_WIDTH = 64
FRAME_HEIGHT = 36


def _turn(action: object) -> AgentTurn:
    return AgentTurn.model_validate({"thought": "", "sub_goal": "", "action": action})


def _frame(white: Rect | None = None) -> ScreenCapture:
    """64×36 BGRA frame, all black, with an optional white region painted."""
    buf = bytearray(FRAME_WIDTH * FRAME_HEIGHT * 4)
    if white is not None:
        x0 = max(0, int(white.origin.x))
        y0 = max(0, int(white.origin.y))
        x1 = min(FRAME_WIDTH, int(white.origin.x + white.size.width))
        y1 = min(FRAME_HEIGHT, int(white.origin.y + white.size.height))
        white_row = b"\xff\xff\xff\xff" * (x1 - x0)
        for y in range(y0, y1):
            base = (y * FRAME_WIDTH + x0) * 4
            buf[base : base + (x1 - x0) * 4] = white_row
    return ScreenCapture(
        display_id=0, width=FRAME_WIDTH, height=FRAME_HEIGHT, scale=1.0, data=bytes(buf)
    )


class FakeScreen:
    """A screen the test can repaint, plus the sensor that reads it.

    Unlike a fixed list of frames, this survives however many captures the loop
    decides to take — the number of captures is an implementation detail, and a
    test that pins it would break on every legitimate optimisation.
    """

    def __init__(self) -> None:
        self.capture_count = 0
        self._frame = _frame()

    def paint(self, white: Rect | None) -> None:
        self._frame = _frame(white)

    def sensor(self) -> ScreenCapture:
        self.capture_count += 1
        return self._frame


# --- Pure helpers ------------------------------------------------------------


def test_target_point_of_only_verifiable_actions() -> None:
    assert target_point_of(MouseClick(type="mouse_click", x=10, y=20)) == Point(10, 20)
    assert target_point_of(
        MouseDrag(type="mouse_drag", start_x=1, start_y=2, end_x=30, end_y=40)
    ) == Point(30, 40)
    # Moves are excluded: CGDisplayCreateImage omits the cursor, so a move to
    # empty space legitimately changes nothing on screen.
    assert target_point_of(MouseMove(type="mouse_move", x=10, y=20)) is None
    assert target_point_of(TypeText(type="type_text", text="hi", wpm=40)) is None


def test_verification_region_is_centred() -> None:
    region = verification_region(Point(10, 20))
    assert region.origin == Point(-14, -4)
    assert region.size == Size(48, 48)


def test_verification_region_rejects_non_positive_size() -> None:
    with pytest.raises(ValueError, match="positive"):
        verification_region(Point(0, 0), size=0)


def test_expectation_matches_each_action_kind() -> None:
    """Each action declares only the witnesses that can speak about it."""
    click = expectation_for(MouseClick(type="mouse_click", x=5, y=6))
    assert click.pixel == "region"
    assert click.region_point == Point(5, 6)
    assert click.expects_ui_change

    scroll = expectation_for(MouseScroll(type="mouse_scroll", dx=0, dy=300))
    assert scroll.pixel == "frame"

    paste = expectation_for(ClipboardPaste(type="clipboard_paste", text="hello"))
    # Pixels judge text badly at map resolution; AXValue is the real witness.
    assert paste.pixel == "none"
    assert paste.expected_text == "hello"

    activate = expectation_for(ActivateApp(type="activate_app", app="Safari"))
    assert activate.expected_app == "Safari"

    submit = expectation_for(PressHotkey(type="press_hotkey", modifiers=[], key="return"))
    assert submit.pixel == "frame"
    # A setup hotkey has no observable of its own; the next action verifies it.
    setup = expectation_for(PressHotkey(type="press_hotkey", modifiers=["command"], key="l"))
    assert not setup.is_verifiable


def test_combine_weighs_witnesses_by_strength() -> None:
    """Confirmation always wins; contradiction is weighted by witness strength."""
    # One positive observation outweighs any number of silent witnesses.
    assert (
        combine(direct=(Evidence.CONTRADICTED,), circumstantial=(Evidence.CONFIRMED,))
        is Evidence.CONFIRMED
    )
    # A direct denial ("the text is not in the field") is conclusive alone.
    assert (
        combine(direct=(Evidence.CONTRADICTED,), circumstantial=())
        is Evidence.CONTRADICTED
    )
    # A lone circumstantial "nothing changed" is NOT: plenty of valid actions
    # change nothing observable, and failing them was the loop's worst habit.
    assert (
        combine(direct=(), circumstantial=(Evidence.CONTRADICTED,))
        is Evidence.INCONCLUSIVE
    )
    # Two independent circumstantial witnesses agreeing is a real miss.
    assert (
        combine(
            direct=(), circumstantial=(Evidence.CONTRADICTED, Evidence.CONTRADICTED)
        )
        is Evidence.CONTRADICTED
    )
    assert combine(direct=(), circumstantial=()) is Evidence.INCONCLUSIVE


def test_witnesses_treat_absence_as_silence_not_failure() -> None:
    """Absence of evidence must never be reported as evidence of absence."""
    assert text_evidence("hello", None) is Evidence.INCONCLUSIVE
    assert text_evidence("hello", "") is Evidence.INCONCLUSIVE
    assert text_evidence("hello", "say hello now") is Evidence.CONFIRMED
    assert text_evidence("hello", "something else") is Evidence.CONTRADICTED

    assert ui_state_evidence((), ()) is Evidence.INCONCLUSIVE
    assert ui_state_evidence(("a",), ("b",)) is Evidence.CONFIRMED
    assert ui_state_evidence(("a",), ("a",)) is Evidence.CONTRADICTED

    assert app_evidence("Safari", None) is Evidence.INCONCLUSIVE
    # LaunchServices names and AX titles routinely disagree about the same app.
    assert app_evidence("Google Chrome", "Chrome") is Evidence.CONFIRMED
    assert app_evidence("Safari", "Finder") is Evidence.CONTRADICTED


# --- Verification integration ------------------------------------------------


def test_landed_click_passes_verification() -> None:
    """A click that visibly changed its target region completes normally."""
    screen = FakeScreen()

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            # The after-frame differs, so the pixel witness confirms.
            screen.paint(Rect(Point(0, 0), Size(34, 34)))
            return _turn(MouseClick(type="mouse_click", x=10, y=10))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=screen.sensor,
        verify_enabled=True,
        max_steps=5,
    )
    final = runner.run(goal="click the button")
    assert final.last_error is None
    assert final.completed_steps == ("step_0:mouse_click", "step_1:finish")


def test_missed_click_folds_failure_into_state() -> None:
    """A click that TWO witnesses deny is caught and surfaced to the LLM."""
    screen = FakeScreen()
    seen: list[WorkingState] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state)
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=10, y=10))
        return _turn(Finish(type="finish", status="failed", summary="gave up"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=screen.sensor,  # pixels never move
        verify_enabled=True,
        # A static AX surface is the corroborating second witness; without it
        # the pixel diff alone would (correctly) be inconclusive.
        ax_probe=lambda: AxProbeResult(summaries=('Button "Go" at (20,15) 20x10',)),
        max_steps=5,
    )
    final = runner.run(goal="click the button")
    assert final.last_error is not None
    assert "VerificationFailedError" in final.last_error
    assert "no observable change" in final.last_error
    # F2: the failed click never entered completed_steps.
    assert "mouse_click" not in final.completed_steps
    assert final.completed_steps == ("step_1:finish",)
    # The *next* provider turn saw the failure before deciding to finish.
    assert "VerificationFailedError" in (seen[1].last_error or "")


def test_a_lone_silent_witness_never_fails_an_action() -> None:
    """With only one witness available, "nothing changed" is not a verdict.

    This is the false-failure that made the loop unusable with a single
    perception channel: a click that legitimately changes nothing near its
    target (dismissing focus, re-selecting an active row) was reported to the
    model as a miss, and it abandoned a target it had actually hit.
    """
    screen = FakeScreen()

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=10, y=10))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=screen.sensor,  # the only witness, and it never sees a change
        verify_enabled=True,
        max_steps=5,
    )
    final = runner.run(goal="click")
    assert final.last_error is None
    assert "step_0:mouse_click" in final.completed_steps


def test_ax_confirmation_overrides_a_silent_pixel_diff() -> None:
    """A click that only moved keyboard focus must NOT be reported as a miss.

    This is the false-failure the pixel-only check produced constantly: a click
    into a text field changes a one-pixel caret, the region diff says
    "unchanged", and the model abandoned a target it had actually hit.
    """
    screen = FakeScreen()
    ax_states = [
        AxProbeResult(summaries=('TextField "url" at (30,15) 40x10',)),
        AxProbeResult(summaries=('TextField "url" at (30,15) 40x10 (focused)',)),
    ]
    index = {"value": 0}

    def ax_probe() -> AxProbeResult:
        result = ax_states[min(index["value"], len(ax_states) - 1)]
        index["value"] += 1
        return result

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=10, y=10))
        return _turn(Finish(type="finish", status="success", summary="focused"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=screen.sensor,  # pixels never change -> pixel witness contradicts
        verify_enabled=True,
        ax_probe=ax_probe,
        max_steps=5,
    )
    final = runner.run(goal="focus the address bar")
    assert final.last_error is None
    assert "step_0:mouse_click" in final.completed_steps


def test_out_of_bounds_click_rejected_before_actuation() -> None:
    """A hallucinated coordinate outside the observed display never actuates."""
    executed: list[object] = []
    screen = FakeScreen()

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=5000, y=5000))
        return _turn(Finish(type="finish", status="failed", summary="gave up"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=executed.append,  # type: ignore[arg-type]
        sensor=screen.sensor,
        verify_enabled=True,
        max_steps=5,
    )
    final = runner.run(goal="click the thing")
    assert executed == [], "out-of-bounds click must never reach the physical layer"
    assert final.last_error is not None
    assert "outside the observed" in final.last_error


def test_out_of_bounds_drag_end_rejected() -> None:
    """A drag whose endpoint leaves the display is rejected too."""
    executed: list[object] = []
    screen = FakeScreen()

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(
                MouseDrag(type="mouse_drag", start_x=10, start_y=10, end_x=9000, end_y=10)
            )
        return _turn(Finish(type="finish", status="failed", summary="gave up"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=executed.append,  # type: ignore[arg-type]
        sensor=screen.sensor,
        verify_enabled=True,
        max_steps=5,
    )
    final = runner.run(goal="drag")
    assert executed == []
    assert final.last_error is not None and "drag end" in final.last_error


def test_bounds_are_checked_even_without_pixel_verification() -> None:
    """A phantom coordinate is rejected whether or not --verify is on.

    Bounds checking is not a verification feature — it is the gate that stops
    an invented coordinate from reaching a physical mouse. Tying it to a flag
    meant ``--no-verify`` silently disabled it.
    """
    executed: list[object] = []
    screen = FakeScreen()

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=4000, y=10))
        return _turn(Finish(type="finish", status="failed", summary="gave up"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=executed.append,  # type: ignore[arg-type]
        sensor=screen.sensor,
        verify_enabled=False,
        max_steps=5,
    )
    final = runner.run(goal="click")
    assert executed == []
    assert final.last_error is not None and "outside the observed" in final.last_error


def test_verification_skipped_without_any_probe() -> None:
    """No sensor and no probes → nothing to verify against, and no failure."""

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=10, y=10))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        max_steps=5,
    )
    final = runner.run(goal="click")
    assert final.last_error is None
    assert final.completed_steps == ("step_0:mouse_click", "step_1:finish")


def test_setup_hotkey_consults_no_witness() -> None:
    """Cmd+L has no observable of its own; the next action verifies it."""
    screen = FakeScreen()

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(PressHotkey(type="press_hotkey", modifiers=["command"], key="l"))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=screen.sensor,
        verify_enabled=True,
        max_steps=5,
    )
    final = runner.run(goal="focus address bar")
    assert final.last_error is None
    assert any("press_hotkey" in step for step in final.completed_steps)


# --- Semantic text-insertion verification (AXValue) --------------------------


def test_paste_verified_against_focused_field_value() -> None:
    """Paste that lands in the focused field completes normally."""
    calls: list[str] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(ClipboardPaste(type="clipboard_paste", text="latest AI news"))
        return _turn(Finish(type="finish", status="success", summary="done"))

    def probe() -> str | None:
        calls.append("probed")
        return "latest AI news"

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        focused_text_value=probe,
        max_steps=5,
    )
    final = runner.run(goal="search the web")
    assert final.last_error is None
    assert calls, "semantic probe must be consulted after a paste"


def test_paste_miss_folds_semantic_failure_into_state() -> None:
    """A paste whose text is absent from the focused field is caught."""
    seen: list[WorkingState] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state)
        if state.step_index == 0:
            return _turn(ClipboardPaste(type="clipboard_paste", text="latest AI news"))
        return _turn(Finish(type="finish", status="failed", summary="gave up"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        focused_text_value=lambda: "different query",
        max_steps=5,
    )
    final = runner.run(goal="search the web")
    assert final.last_error is not None
    assert "focused_field=contradicted" in final.last_error
    # F2: the failed paste never entered completed_steps.
    assert "clipboard_paste" not in final.completed_steps
    assert "no observable change" in (seen[1].last_error or "")


def test_type_verification_skipped_without_evidence() -> None:
    """No focused-field value -> verification is skipped, not failed."""

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(TypeText(type="type_text", text="hi", wpm=40))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        focused_text_value=lambda: None,
        max_steps=5,
    )
    final = runner.run(goal="type")
    assert final.last_error is None
    assert any("type_text" in step for step in final.completed_steps)


def test_activation_contradiction_is_caught() -> None:
    """activate_app that leaves another app frontmost is a real failure."""

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(ActivateApp(type="activate_app", app="Safari"))
        return _turn(Finish(type="finish", status="failed", summary="gave up"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        window_probe=lambda: FocusedWindow(pid=1, app_name="Finder", window_title=""),
        max_steps=5,
    )
    final = runner.run(goal="open safari")
    assert final.last_error is not None
    assert "frontmost_app=contradicted" in final.last_error


# --- Recovery ladder ---------------------------------------------------------


def test_failure_classification_and_ladder_rungs() -> None:
    """Each rung demands a strictly stronger change of approach (pure)."""
    failure = classify_failure(
        RuntimeError("boom"), MouseClick(type="mouse_click", x=10, y=10)
    )
    assert failure.kind is FailureKind.UNKNOWN
    assert recovery_for(1) is RecoveryAction.RETRY
    assert recovery_for(2) is RecoveryAction.ALTERNATE
    assert recovery_for(3) is RecoveryAction.REPLAN
    assert recovery_for(4) is RecoveryAction.ABORT
    with pytest.raises(ValueError, match="positive"):
        recovery_for(0)
    # The hint escalates in wording, not just in count.
    assert "different" in recovery_hint(failure, 2).lower()
    assert "abandon" in recovery_hint(failure, 3).lower()


def test_pointer_jitter_does_not_reset_the_ladder() -> None:
    """A model nudging its coordinates a few pixels is still the same failure.

    Byte-identical comparison let a lost model dodge every guard by varying
    the click by 2px per attempt; the signature buckets coordinates so it
    cannot.
    """
    near = classify_failure(ValueError("x"), MouseClick(type="mouse_click", x=100, y=100))
    jittered = classify_failure(ValueError("x"), MouseClick(type="mouse_click", x=103, y=98))
    far = classify_failure(ValueError("x"), MouseClick(type="mouse_click", x=900, y=100))
    assert near.signature == jittered.signature
    assert near.signature != far.signature


def test_repeated_failure_ends_the_run_instead_of_looping() -> None:
    """A permanently failing action must not consume the whole step budget."""
    attempts: list[int] = []

    def provider(state: WorkingState) -> AgentTurn:
        attempts.append(state.step_index)
        return _turn(MouseClick(type="mouse_click", x=10, y=10))

    def always_fails(_action: object) -> None:
        raise RuntimeError("the driver refuses this click")

    runner = OodaRunner(
        provider=provider,
        execute_physical=always_fails,
        max_steps=100,
    )
    with pytest.raises(UnrecoverableFailureError) as excinfo:
        runner.run(goal="click forever")
    assert excinfo.value.failure.action_type == "mouse_click"
    # The ladder, not the step budget, ended the run.
    assert len(attempts) < 10


def test_recovery_hint_escalates_across_repeats() -> None:
    """The model is told, in words, that a retry is no longer acceptable."""
    seen: list[str | None] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state.last_error)
        return _turn(MouseClick(type="mouse_click", x=10, y=10))

    def always_fails(_action: object) -> None:
        raise RuntimeError("nope")

    runner = OodaRunner(
        provider=provider, execute_physical=always_fails, max_steps=100
    )
    with pytest.raises(UnrecoverableFailureError):
        runner.run(goal="click")
    hints = [hint for hint in seen if hint]
    assert len(hints) >= 2
    assert "different" in hints[1].lower()


def test_success_resets_the_failure_ladder() -> None:
    """A recovered failure must not count toward a later, unrelated one."""
    calls = {"count": 0}

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index >= 6:
            return _turn(Finish(type="finish", status="success", summary="done"))
        return _turn(MouseClick(type="mouse_click", x=10 + state.step_index * 100, y=10))

    def fails_every_other(_action: object) -> None:
        calls["count"] += 1
        if calls["count"] % 2 == 1:
            raise RuntimeError("intermittent")

    runner = OodaRunner(
        provider=provider, execute_physical=fails_every_other, max_steps=20
    )
    final = runner.run(goal="alternate")
    assert final.completed_steps


# --- Goal-completion audit ---------------------------------------------------


def test_rejected_completion_claim_keeps_the_run_going() -> None:
    """A model claiming success it cannot show is not allowed to end the run."""
    from computeruse.orchestrator.evidence import CompletionVerdict

    screen = FakeScreen()
    audits: list[str] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(Finish(type="finish", status="success", summary="all done"))
        return _turn(Finish(type="finish", status="failed", summary="could not do it"))

    def audit(_state: WorkingState, claim: str) -> CompletionVerdict:
        audits.append(claim)
        return CompletionVerdict(satisfied=False, evidence="the page still shows a spinner")

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=screen.sensor,
        completion_check=audit,
        max_steps=5,
    )
    final = runner.run(goal="do the thing")
    assert audits == ["all done"]
    # The rejection reached the model, and the run ended on the honest verdict.
    assert final.completed_steps == ("step_1:finish",)


def test_accepted_completion_claim_ends_the_run() -> None:
    """An audited success terminates immediately."""
    from computeruse.orchestrator.evidence import CompletionVerdict

    screen = FakeScreen()

    def provider(_state: WorkingState) -> AgentTurn:
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=screen.sensor,
        completion_check=lambda _s, _c: CompletionVerdict(
            satisfied=True, evidence="the profile shows the new name"
        ),
        max_steps=5,
    )
    final = runner.run(goal="rename")
    assert final.completed_steps == ("step_0:finish",)


def test_broken_auditor_never_traps_a_finished_run() -> None:
    """A checker that raises must not turn a completed task into a hung one."""
    screen = FakeScreen()

    def provider(_state: WorkingState) -> AgentTurn:
        return _turn(Finish(type="finish", status="success", summary="done"))

    def broken_audit(_state: WorkingState, _claim: str) -> object:
        raise RuntimeError("auditor transport down")

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=screen.sensor,
        completion_check=broken_audit,  # type: ignore[arg-type]
        max_steps=5,
    )
    final = runner.run(goal="rename")
    assert final.completed_steps == ("step_0:finish",)


# --- Staleness and focus gates ----------------------------------------------


def test_stale_window_blocks_the_click_then_yields() -> None:
    """A host that moved after the decision rejects the click exactly once.

    Blocking forever would be worse than acting slightly late: on a host whose
    title changes continuously (a video, a progress counter) nothing would ever
    execute. The gate rejects the first attempt — so the model re-reads — and
    lets the next one through.
    """
    executed: list[object] = []
    title = {"value": "page 0"}
    reads = {"count": 0}

    def window_probe() -> FocusedWindow:
        # Every read reports a new title: the host is continuously changing,
        # which is the worst case for a staleness gate.
        reads["count"] += 1
        title["value"] = f"page {reads['count']}"
        return FocusedWindow(pid=1, app_name="Safari", window_title=title["value"])

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index >= 3:
            return _turn(Finish(type="finish", status="failed", summary="stop"))
        return _turn(MouseClick(type="mouse_click", x=10, y=10))

    runner = OodaRunner(
        provider=provider,
        execute_physical=executed.append,  # type: ignore[arg-type]
        window_probe=window_probe,
        max_steps=6,
    )
    final = runner.run(goal="click a moving page")
    assert executed, "the gate must yield rather than block forever"
    assert final.step_index > 0


def test_stale_gate_is_silent_when_the_window_is_stable() -> None:
    """A settled host must never be reported as stale."""
    executed: list[object] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=10, y=10))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=executed.append,  # type: ignore[arg-type]
        window_probe=lambda: FocusedWindow(
            pid=1, app_name="Safari", window_title="GitHub"
        ),
        max_steps=5,
    )
    final = runner.run(goal="click")
    assert final.last_error is None
    assert len(executed) == 1


def test_focus_drift_reactivates_the_target_app() -> None:
    """A positional action while another app is frontmost re-asserts the target."""
    dispatched: list[str] = []
    frontmost = {"app": "Finder"}

    def execute(action: object) -> None:
        dispatched.append(getattr(action, "type", "?"))
        if isinstance(action, ActivateApp):
            frontmost["app"] = action.app

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=10, y=10))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=execute,
        window_probe=lambda: FocusedWindow(
            pid=1, app_name=frontmost["app"], window_title=""
        ),
        app="Safari",
        app_is_pinned=True,
        max_steps=5,
    )
    runner.run(goal="click in safari")
    assert dispatched[0] == "activate_app", "focus must be re-asserted before clicking"
    assert "mouse_click" in dispatched


def test_focus_guard_is_inert_for_an_unpinned_run() -> None:
    """A run that merely discovered the frontmost app has no drift to guard."""
    dispatched: list[str] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=10, y=10))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda action: dispatched.append(action.type),
        window_probe=lambda: FocusedWindow(pid=1, app_name="Finder", window_title=""),
        app="Safari",
        app_is_pinned=False,
        max_steps=5,
    )
    runner.run(goal="click wherever")
    assert dispatched == ["mouse_click"]


def test_idempotent_click_is_confirmed_by_target_focus() -> None:
    """Clicking a control already in its target state must not read as a miss.

    Observed in a real model run: the agent clicked the right button, the click
    landed, nothing changed because the button was already focused — and both
    change-detecting witnesses reported a failure. The agent was told twice it
    had missed and abandoned a correct approach. The element under the click
    holding focus is direct proof the click reached it.
    """
    screen = FakeScreen()
    summaries = ('Button "Reload" at (30,16) 20x12 (focused)',)

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=30, y=16))
        return _turn(Finish(type="finish", status="success", summary="reloaded"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=screen.sensor,  # pixels never move
        verify_enabled=True,
        ax_probe=lambda: AxProbeResult(summaries=summaries),  # surface never moves
        max_steps=5,
    )
    final = runner.run(goal="reload the page")
    assert final.last_error is None
    assert "step_0:mouse_click" in final.completed_steps


def test_target_focus_witness_can_never_cause_a_failure() -> None:
    """Focus not landing proves nothing — many controls never take focus."""
    unfocused = ('Button "Reload" at (254,80) 44x24',)
    assert target_focus_evidence(Point(254, 80), unfocused) is Evidence.INCONCLUSIVE
    # A point no summary covers is silence, not a denial: the summary list is
    # budget-capped and may simply not include the element.
    assert target_focus_evidence(Point(9, 9), unfocused) is Evidence.INCONCLUSIVE
    assert target_focus_evidence(None, unfocused) is Evidence.INCONCLUSIVE
    focused = ('Button "Reload" at (254,80) 44x24 (focused)',)
    assert target_focus_evidence(Point(254, 80), focused) is Evidence.CONFIRMED


def test_summary_lookup_prefers_the_most_specific_element() -> None:
    """A button inside a toolbar must win: only the button explains the click."""
    from computeruse.vision.ax import summary_covering

    summaries = (
        'Toolbar "" at (500,80) 800x40',
        'Button "Reload" at (254,80) 44x24 (focused)',
    )
    covering = summary_covering(summaries, 254, 80)
    assert covering is not None and "Reload" in covering
    assert summary_covering(summaries, 120, 65) is not None
    assert summary_covering(summaries, 5000, 5000) is None


# --- the AX surface witness --------------------------------------------------


def test_text_change_alone_confirms_an_action() -> None:
    """The most common effect an action has, and the one nothing else saw.

    Measured on Calculator, three real button presses in a row: the interactive
    element list was identical every time (a display is a StaticText, not an
    interactive role), and the pixel diff was unchanged every time — even over
    the whole window, because a few digits redrawing is far below the
    fraction-of-pixels threshold. The agent pressed the right buttons, watched
    the display update, and was told it had missed.
    """
    elements = ('Button "7" at (345,664) 48x48',)
    assert (
        ax_surface_evidence(
            elements, elements, ("StaticText=47",), ("StaticText=478",)
        )
        is Evidence.CONFIRMED
    )


def test_structure_change_alone_still_confirms() -> None:
    """A focus move with no text change must keep working as before."""
    before = ('Button "Reload" at (254,80) 44x24',)
    after = ('Button "Reload" at (254,80) 44x24 (focused)',)
    content = ("StaticText=unchanged",)
    assert ax_surface_evidence(before, after, content, content) is Evidence.CONFIRMED


def test_both_signals_silent_contradicts() -> None:
    same_elements = ('Button "7" at (345,664) 48x48',)
    same_content = ("StaticText=47",)
    assert (
        ax_surface_evidence(same_elements, same_elements, same_content, same_content)
        is Evidence.CONTRADICTED
    )


def test_an_unavailable_probe_never_fails_an_action() -> None:
    """No data is silence, not a denial — the loop's central rule."""
    assert ax_surface_evidence((), (), (), ()) is Evidence.INCONCLUSIVE


def test_the_two_ax_signals_never_vote_twice() -> None:
    """They read one snapshot; counting them separately would fail blind runs.

    A failure needs two corroborating circumstantial witnesses. If the element
    list and the content digest each cast a vote, a single silent AX probe
    would supply both, and every action it cannot observe would look decisively
    failed rather than unverified.
    """
    elements = ('Button "OK" at (20,10) 20x12',)
    content = ("StaticText=idle",)

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=30, y=16))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        # No pixel witness at all, so AX is the ONLY circumstantial voice: if
        # its two signals counted separately they would convict on their own.
        sensor=None,
        verify_enabled=True,
        ax_probe=lambda: AxProbeResult(summaries=elements, content=content),
        max_steps=5,
    )
    final = runner.run(goal="press OK")
    assert final.last_error is None
    assert "step_0:mouse_click" in final.completed_steps


def test_diagnosis_distinguishes_a_miss_from_an_idempotent_hit() -> None:
    """Same witnesses, opposite advice — because the causes are opposite.

    "Nothing changed" happens both when a click misses and when it lands on a
    control already in the state being asked for. Observed on Calculator:
    pressing Clear on an already-clear display and Equals on an already-computed
    result were both reported as misses, and the agent spent four steps chasing
    a coordinate problem it did not have.
    """
    expectation = expectation_for(MouseClick(type="mouse_click", x=397, y=611))
    reports = (("ax_state", Evidence.CONTRADICTED), ("pixels", Evidence.CONTRADICTED))

    missed = verification_diagnostic("mouse_click", expectation, reports, None)
    assert "did not land where you aimed" in missed
    assert "re-derive" in missed

    hit = verification_diagnostic(
        "mouse_click", expectation, reports, 'Button "Tümünü Sil" at (399,610) 48x48'
    )
    assert "Tümünü Sil" in hit
    assert "do NOT re-aim" in hit
    assert "already in the state you want" in hit
    # The advice must not contradict itself.
    assert "re-derive" not in hit
