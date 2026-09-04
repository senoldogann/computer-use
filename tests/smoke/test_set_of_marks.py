"""Smoke tests for Phase 4: Set-of-Marks (SoM) visual annotator."""

from __future__ import annotations

import pytest

from computeruse.orchestrator.loop import (
    MARK_DRIFT_TOLERANCE_PX,
    AxProbeResult,
    OodaRunner,
    UnknownMarkError,
    WorkingState,
    mark_still_current,
    resolve_mark,
)
from computeruse.orchestrator.prompts import state_context
from computeruse.orchestrator.schemas import (
    Action,
    AgentTurn,
    ClickMark,
    Finish,
    MouseClick,
    Wait,
)
from computeruse.vision.capture import ScreenCapture, capture_to_base64_png
from computeruse.vision.som import (
    annotate_set_of_marks,
    parse_ax_elements_to_marks,
)


def test_parse_ax_elements_to_marks() -> None:
    """Parses AX element coordinate summaries into MarkElements."""
    summaries = (
        'Button "Reload" at (232, 68) 44x24',
        'TextField "Search" at (320, 68) 400x24 (focused)',
        'InvalidElementWithoutCoords',
    )
    marks = parse_ax_elements_to_marks(summaries)
    assert len(marks) == 2
    assert marks[0].index == 1
    assert marks[0].role == "Button"
    # The summary reports the element's CENTRE, so the box is rebuilt around
    # it: (232,68) with a 44x24 element means a top-left of (210,56).
    assert marks[0].rect.origin.x == 210.0
    assert marks[0].rect.origin.y == 56.0
    assert marks[0].rect.size.width == 44.0
    assert marks[0].rect.size.height == 24.0

    assert marks[1].index == 2
    assert marks[1].role == "TextField"
    assert marks[1].rect.origin.x == 120.0


def test_annotate_set_of_marks_modifies_buffer() -> None:
    """Annotating a blank capture draws bounding box pixels into the buffer."""
    w, h = 100, 100
    # Blank black BGRA frame (100x100 = 10000 pixels * 4 = 40000 bytes)
    blank_data = bytes([0] * (w * h * 4))
    capture = ScreenCapture(
        display_id=0,
        width=w,
        height=h,
        scale=1.0,
        data=blank_data,
    )

    summaries = ('Button "Test" at (10, 10) 30x20',)
    marks = parse_ax_elements_to_marks(summaries)
    annotated = annotate_set_of_marks(capture, marks)

    assert annotated.data != blank_data
    # Check that border pixel (10, 10) was colored with emerald green (B=116, G=165, R=80)
    idx = (10 * w + 10) * 4
    assert annotated.data[idx] == 116      # B
    assert annotated.data[idx + 1] == 165  # G
    assert annotated.data[idx + 2] == 80   # R


# ── Set-of-Marks wired into the loop ───────────────────────────────────────


def _turn(action: object) -> AgentTurn:
    return AgentTurn.model_validate({"thought": "t", "sub_goal": "s", "action": action})


def test_a_mark_resolves_to_the_element_centre_in_screen_points() -> None:
    """The whole point of the mark channel: no image-space estimate at all."""
    marks = parse_ax_elements_to_marks(('Button "Reload" at (232,68) 44x24',))
    resolved = resolve_mark(ClickMark(type="click_mark", mark=1), marks)
    assert isinstance(resolved, MouseClick)
    assert (resolved.x, resolved.y) == (232, 68)
    assert resolved.button == "left"


def test_a_mark_keeps_its_button_and_click_count() -> None:
    marks = parse_ax_elements_to_marks(('Cell "Row" at (100,200) 40x20',))
    resolved = resolve_mark(
        ClickMark(type="click_mark", mark=1, button="right", click_count=2), marks
    )
    assert isinstance(resolved, MouseClick)
    assert (resolved.button, resolved.click_count) == ("right", 2)


def test_an_unknown_mark_raises_instead_of_clicking_something_plausible() -> None:
    marks = parse_ax_elements_to_marks(('Button "Reload" at (232,68) 44x24',))
    with pytest.raises(UnknownMarkError, match="mark 4"):
        resolve_mark(ClickMark(type="click_mark", mark=4), marks)


def test_non_mark_actions_pass_through_untouched() -> None:
    click = MouseClick(type="mouse_click", x=1, y=2)
    assert resolve_mark(click, ()) is click


def test_the_runner_clicks_the_element_a_mark_names() -> None:
    """End to end through the loop: [N] in, the element's own centre out."""
    executed: list[Action] = []

    def ax_probe() -> AxProbeResult:
        return AxProbeResult(
            summaries=(
                'Button "Back" at (40,60) 20x20',
                'Link "Download the report" at (300,480) 160x18',
            )
        )

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(ClickMark(type="click_mark", mark=2))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=executed.append,
        ax_probe=ax_probe,
        max_steps=4,
    )
    runner.run(goal="download it")
    assert len(executed) == 1
    click = executed[0]
    assert isinstance(click, MouseClick)
    assert (click.x, click.y) == (300, 480)


def test_a_mark_is_never_scaled_by_the_coordinate_gate() -> None:
    """A resolved mark is already in screen points; scaling it again misplaces it.

    With a screen map in play the model's own coordinates are multiplied on
    their way to the driver. A mark's coordinates come from the accessibility
    rect, which is already in that space — running them through the same
    multiplication would send the click a third of the way up the display.
    """
    executed: list[Action] = []
    logical = ScreenCapture(
        display_id=0, width=1536, height=1024, scale=1.0, data=bytes(1536 * 1024 * 4)
    )

    def ax_probe() -> AxProbeResult:
        return AxProbeResult(summaries=('Button "Send" at (600,300) 80x24',))

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(ClickMark(type="click_mark", mark=1))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=executed.append,
        sensor=lambda: logical,
        vision_enabled=False,
        ax_probe=ax_probe,
        max_steps=4,
    )
    runner.run(goal="send it")
    assert runner._observation.screen_map is not None
    assert not runner._observation.screen_map.is_identity, "the map must be in play"
    click = executed[0]
    assert isinstance(click, MouseClick)
    assert (click.x, click.y) == (600, 300), "screen points, not scaled again"


def test_the_screenshot_the_model_sees_carries_the_boxes() -> None:
    """Annotation is on by default and re-encodes when the element list moves."""
    frame = ScreenCapture(
        display_id=0, width=64, height=48, scale=1.0, data=bytes(64 * 48 * 4)
    )
    listings = [
        ('Button "One" at (20,20) 16x10',),
        ('Button "Two" at (44,30) 16x10',),
    ]

    def ax_probe() -> AxProbeResult:
        return AxProbeResult(summaries=listings[0])

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            listings[0] = listings[1]  # the surface changes, the pixels do not
            return _turn(Wait(type="wait", duration_ms=0, reason="look"))
        return _turn(Finish(type="finish", status="success", summary="done"))

    seen: list[str] = []

    def watching_provider(state: WorkingState) -> AgentTurn:
        if state.screenshot_b64:
            seen.append(state.screenshot_b64)
        return provider(state)

    runner = OodaRunner(
        provider=watching_provider,
        execute_physical=lambda _a: None,
        sensor=lambda: frame,
        vision_enabled=True,
        ax_probe=ax_probe,
        max_steps=4,
    )
    runner.run(goal="look at it")
    plain = capture_to_base64_png(frame)
    assert seen, "vision was on, so the model saw a frame"
    assert seen[0] != plain, "the boxes must be drawn onto the frame"
    assert seen[0] != seen[1], "a changed element list must re-encode the frame"


def test_marks_can_be_turned_off_without_losing_the_mark_channel() -> None:
    """--no-marks stops the drawing; selecting a target by mark still works."""
    executed: list[Action] = []
    frame = ScreenCapture(
        display_id=0, width=64, height=48, scale=1.0, data=bytes(64 * 48 * 4)
    )

    def ax_probe() -> AxProbeResult:
        return AxProbeResult(summaries=('Button "One" at (20,20) 16x10',))

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(ClickMark(type="click_mark", mark=1))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=executed.append,
        sensor=lambda: frame,
        vision_enabled=True,
        set_of_marks_enabled=False,
        ax_probe=ax_probe,
        max_steps=4,
    )
    final = runner.run(goal="press one")
    assert final.screenshot_b64 == capture_to_base64_png(frame), "no boxes drawn"
    assert len(executed) == 1


def test_the_prompt_numbers_elements_the_way_marks_are_numbered() -> None:
    """[N] in the prompt and mark N must be the same element, or nothing works."""
    summaries = (
        'Button "First" at (10,10) 8x8',
        "(AX grounding truncated at 64 elements)",
        'Link "Third" at (30,30) 20x6',
    )
    rendered = state_context(WorkingState(goal="g", ui_elements=summaries))
    assert '- [3] Link "Third" at (30,30) 20x6' in rendered
    marks = parse_ax_elements_to_marks(summaries)
    third = next(mark for mark in marks if mark.index == 3)
    assert "Third" in third.label, "the un-parseable line must still consume its index"


# --- SEC-02: a mark index only means something inside its own frame ---------


def test_mark_still_current_accepts_a_control_that_barely_moved() -> None:
    """A row above growing shifts a button a few points; it is the same button."""
    decided = parse_ax_elements_to_marks(('Button "Cancel" at (300, 200) 80x30',))[0]
    live = parse_ax_elements_to_marks(('Button "Cancel" at (300, 212) 80x30',))
    assert mark_still_current(decided, live, tolerance=MARK_DRIFT_TOLERANCE_PX)


def test_mark_still_current_rejects_a_control_that_moved_away() -> None:
    decided = parse_ax_elements_to_marks(('Button "Cancel" at (300, 200) 80x30',))[0]
    live = parse_ax_elements_to_marks(('Button "Cancel" at (300, 480) 80x30',))
    assert not mark_still_current(decided, live, tolerance=MARK_DRIFT_TOLERANCE_PX)


def test_mark_still_current_rejects_a_different_control_at_the_same_place() -> None:
    """The reported failure, reduced to its core.

    A notification arrives between two actions of one batch and the element
    list is renumbered; slot 2 now holds "Delete All" where the model chose
    "Cancel". Position alone would call that unchanged.
    """
    decided = parse_ax_elements_to_marks(('Button "Cancel" at (300, 200) 80x30',))[0]
    live = parse_ax_elements_to_marks(('Button "Delete All" at (300, 200) 80x30',))
    assert not mark_still_current(decided, live, tolerance=MARK_DRIFT_TOLERANCE_PX)


def test_a_batch_resolves_marks_against_the_frame_it_was_decided_from() -> None:
    """Renumbering between actions must not retarget the ones already chosen.

    Two marks at decision time; the second action's screen has them in the
    opposite order. Resolving against the *live* list would send click 2 to
    what is now slot 2 — a different control at a different point.
    """
    decided = parse_ax_elements_to_marks(
        (
            'Button "Save" at (100, 100) 60x20',
            'Button "Cancel" at (300, 200) 80x30',
        )
    )
    reordered = parse_ax_elements_to_marks(
        (
            'Button "Cancel" at (300, 200) 80x30',
            'Button "Save" at (100, 100) 60x20',
        )
    )
    action = ClickMark(type="click_mark", mark=2)
    from_decision = resolve_mark(action, decided)
    from_live = resolve_mark(action, reordered)
    assert isinstance(from_decision, MouseClick)
    assert isinstance(from_live, MouseClick)
    # Same index, two frames, two different controls — which is exactly why the
    # runner pins the decision frame for the whole batch.
    assert (from_decision.x, from_decision.y) == (300, 200)
    assert (from_live.x, from_live.y) == (100, 100)


def test_click_mark_resolution_on_untitled_icon_elements() -> None:
    """Untitled icon elements (hamburger ☰, close ✕, trash 🗑) resolve to centre."""
    summaries = (
        'Button "(untitled)" at (480, 24) 20x20',
        'Button "(untitled)" at (120, 24) 16x16',
    )
    marks = parse_ax_elements_to_marks(summaries)
    assert len(marks) == 2
    resolved_menu = resolve_mark(ClickMark(type="click_mark", mark=1), marks)
    assert isinstance(resolved_menu, MouseClick)
    assert (resolved_menu.x, resolved_menu.y) == (480, 24)

    resolved_close = resolve_mark(ClickMark(type="click_mark", mark=2), marks)
    assert isinstance(resolved_close, MouseClick)
    assert (resolved_close.x, resolved_close.y) == (120, 24)
