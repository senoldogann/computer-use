"""OODA OBSERVE/ORIENT visual verification (Law 2 self-correction).

The driver can ACK a click that landed on nothing (hallucinated coordinates).
These tests pin down the ORIENT step: with a ``sensor`` wired in, the loop
captures before/after a click, diffs the target region, and folds a failed
verification into ``last_error`` for the next provider turn — while keeping
the failed step out of ``completed_steps`` (F2).

Frames are hand-built (all-black BGRA, optionally with a white region), so the
whole path is deterministic and offline.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from computeruse.orchestrator.loop import (
    OodaRunner,
    VisualVerificationFailedError,
    WorkingState,
    target_point_of,
    verification_region,
    visual_failure_diagnostics,
)
from computeruse.orchestrator.schemas import (
    AgentTurn,
    ClipboardPaste,
    Finish,
    MouseClick,
    MouseDrag,
    MouseMove,
    PressHotkey,
    TypeText,
)
from computeruse.vision import ScreenCapture
from computeruse.vision.coordinates import Point, Rect, Size


def _turn(action: object) -> AgentTurn:
    return AgentTurn.model_validate({"thought": "", "sub_goal": "", "action": action})


def _frame(white: Rect | None = None) -> ScreenCapture:
    """64×36 BGRA frame, all black, with an optional white region painted."""
    width, height = 64, 36
    buf = bytearray(width * height * 4)
    if white is not None:
        x0 = max(0, int(white.origin.x))
        y0 = max(0, int(white.origin.y))
        x1 = min(width, int(white.origin.x + white.size.width))
        y1 = min(height, int(white.origin.y + white.size.height))
        white_row = b"\xff\xff\xff\xff" * (x1 - x0)
        for y in range(y0, y1):
            base = (y * width + x0) * 4
            buf[base : base + (x1 - x0) * 4] = white_row
    return ScreenCapture(display_id=0, width=width, height=height, scale=1.0, data=bytes(buf))


# --- Pure ORIENT helpers -----------------------------------------------------


def test_target_point_of_only_verifiable_actions() -> None:
    assert target_point_of(MouseClick(type="mouse_click", x=10, y=20)) == Point(10, 20)
    assert target_point_of(MouseDrag(type="mouse_drag", start_x=1, start_y=2, end_x=30, end_y=40)) == Point(30, 40)
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


def test_visual_failure_diagnostics_carry_signals() -> None:
    from computeruse.vision.diff import ChangeKind, ChangeVerdict

    message = visual_failure_diagnostics(
        "mouse_click",
        Point(10, 20),
        verification_region(Point(10, 20)),
        ChangeVerdict(ChangeKind.UNCHANGED, 0.0, 0.0),
    )
    assert "mouse_click" in message
    assert "(10,20)" in message
    assert "mean_abs=0.000" in message
    assert "changed_fraction=0.000" in message


# --- OODA integration --------------------------------------------------------


def _sensor_over(frames: list[ScreenCapture]) -> tuple[Callable[[], ScreenCapture], list[ScreenCapture]]:
    """A sensor that pops frames in order, recording what it returned."""
    consumed: list[ScreenCapture] = []

    def sensor() -> ScreenCapture:
        frame = frames.pop(0)
        consumed.append(frame)
        return frame

    return sensor, consumed


def test_landed_click_passes_verification() -> None:
    """A click that visibly changed its target region completes normally."""
    seen: list[WorkingState] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state)
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=10, y=10))
        return _turn(Finish(type="finish", status="success", summary="done"))

    # The click region is (-14,-14,48,48), clamped to the 64×36 frame; paint
    # the whole clamped area white in the after-frame.
    sensor, _ = _sensor_over([_frame(), _frame(white=Rect(Point(0, 0), Size(34, 34)))])
    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=sensor,
        verify_enabled=True,
        max_steps=5,
    )
    final = runner.run(goal="click the button")
    assert final.last_error is None
    assert final.completed_steps == ("step_0:mouse_click", "step_1:finish")


def test_missed_click_folds_failure_into_state() -> None:
    """An ACKed click that changed nothing is caught and surfaced to the LLM."""
    seen: list[WorkingState] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state)
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=10, y=10))
        return _turn(Finish(type="finish", status="failed", summary="gave up"))

    sensor, _ = _sensor_over([_frame(), _frame()])  # identical frames
    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=sensor,
        verify_enabled=True,
        max_steps=5,
    )
    final = runner.run(goal="click the button")
    assert final.last_error is not None
    assert "VisualVerificationFailedError" in final.last_error
    assert "produced no visible change" in final.last_error
    # F2: the failed click never entered completed_steps.
    assert "mouse_click" not in final.completed_steps
    assert final.completed_steps == ("step_1:finish",)
    # Law 2: the *next* provider turn saw the failure before deciding to finish.
    assert "VisualVerificationFailedError" in (seen[1].last_error or "")


def test_sensor_never_called_for_unverifiable_action() -> None:
    """type_text has no visual target — no capture pair is wasted on it."""
    frames = [_frame(), _frame()]
    sensor, consumed = _sensor_over(frames)

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(TypeText(type="type_text", text="hi", wpm=40))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=sensor,
        verify_enabled=True,
        max_steps=5,
    )
    final = runner.run(goal="type")
    assert final.last_error is None
    assert consumed == [], "sensor must not be consulted for type_text"


def test_verification_disabled_without_sensor() -> None:
    """Backwards compatibility: no sensor → ORIENT skipped entirely."""
    sensor_calls: list[ScreenCapture] = []

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
    assert sensor_calls == []


def test_out_of_bounds_click_rejected_before_actuation() -> None:
    """A hallucinated coordinate outside the observed display never actuates."""
    executed: list[object] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=5000, y=5000))
        return _turn(Finish(type="finish", status="failed", summary="gave up"))

    sensor, _ = _sensor_over([_frame()])
    runner = OodaRunner(
        provider=provider,
        execute_physical=executed.append,  # type: ignore[arg-type]
        sensor=sensor,
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

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(
                MouseDrag(type="mouse_drag", start_x=10, start_y=10, end_x=9000, end_y=10)
            )
        return _turn(Finish(type="finish", status="failed", summary="gave up"))

    sensor, _ = _sensor_over([_frame()])
    runner = OodaRunner(
        provider=provider,
        execute_physical=executed.append,  # type: ignore[arg-type]
        sensor=sensor,
        verify_enabled=True,
        max_steps=5,
    )
    final = runner.run(goal="drag")
    assert executed == []
    assert final.last_error is not None and "drag end" in final.last_error


# --- Semantic text-insertion verification (ADR-2 AXValue) -------------------


def test_paste_verified_against_focused_field_value() -> None:
    """Paste that lands in the focused field completes normally."""
    calls: list[str | None] = []

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

    def probe() -> str | None:
        return "different query"

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        focused_text_value=probe,
        max_steps=5,
    )
    final = runner.run(goal="search the web")
    assert final.last_error is not None
    assert "text insertion verification failed" in final.last_error
    # F2: the failed paste never entered completed_steps.
    assert "clipboard_paste" not in final.completed_steps
    assert "SemanticVerificationFailedError" in (seen[1].last_error or "")


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
    assert any("type_text" in s for s in final.completed_steps)


def test_verification_skipped_when_probe_unconfigured() -> None:
    """No focused_text_value seam -> paste is not claimed as verified."""
    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(ClipboardPaste(type="clipboard_paste", text="hello"))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(provider=provider, execute_physical=lambda _action: None, max_steps=5)
    final = runner.run(goal="paste")
    assert final.last_error is None
    assert any("clipboard_paste" in s for s in final.completed_steps)


def test_error_carries_structured_context() -> None:
    """Law 6.3: the raised error keeps target/region/verdict for logging."""
    from computeruse.vision.diff import ChangeKind, ChangeVerdict

    error = VisualVerificationFailedError(
        action_type="mouse_click",
        target=Point(10, 20),
        region=verification_region(Point(10, 20)),
        verdict=ChangeVerdict(ChangeKind.UNCHANGED, 0.001, 0.0),
    )
    assert error.action_type == "mouse_click"
    assert error.target == Point(10, 20)
    assert error.verdict.mean_abs_change == pytest.approx(0.001)


# --- press_hotkey full-screen verification (Return/Escape) ------------------


def test_press_hotkey_return_verified_on_page_change() -> None:
    """Return key submits a search → the full screen must change."""
    before = _frame()
    after = _frame(white=Rect(Point(0, 0), Size(64, 36)))  # entirely different
    sensor, consumed = _sensor_over([before, after])
    seen: list[WorkingState] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state)
        if state.step_index == 0:
            return _turn(PressHotkey(type="press_hotkey", modifiers=[], key="return"))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=sensor,
        verify_enabled=True,
        max_steps=5,
    )
    final = runner.run(goal="submit search")
    assert final.last_error is None
    assert any("press_hotkey" in s for s in final.completed_steps)
    assert len(consumed) == 2  # before + after


def test_press_hotkey_return_fails_on_unchanged_screen() -> None:
    """Return key that doesn't change the screen → verification failure."""
    before = _frame()
    after = _frame()  # identical — the Return did nothing
    sensor, _consumed = _sensor_over([before, after])
    seen: list[WorkingState] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state)
        if state.step_index == 0:
            return _turn(PressHotkey(type="press_hotkey", modifiers=[], key="return"))
        return _turn(Finish(type="finish", status="failed", summary="gave up"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=sensor,
        verify_enabled=True,
        max_steps=5,
    )
    final = runner.run(goal="submit")
    assert final.last_error is not None
    assert "VisualVerificationFailedError" in final.last_error
    assert "press_hotkey" not in final.completed_steps


def test_press_hotkey_cmd_l_not_pixel_verified() -> None:
    """Cmd+L is a setup step (focus address bar) — no pixel verification.

    The effect is verified indirectly by the *next* action (e.g. paste is
    verified by AXValue). Wasting a full-screen diff on Cmd+L would be
    noise and could false-fail on a slow render.
    """
    frames = [_frame(), _frame()]
    sensor, consumed = _sensor_over(frames)

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(PressHotkey(type="press_hotkey", modifiers=["command"], key="l"))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=sensor,
        verify_enabled=True,
        max_steps=5,
    )
    final = runner.run(goal="focus address bar")
    assert final.last_error is None
    assert consumed == [], "sensor must not be consulted for Cmd+L"
