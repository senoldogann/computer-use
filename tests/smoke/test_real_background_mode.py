"""Comprehensive verification for True Background-Mode Execution (Law 1 Hardening).

Verifies that in background mode:
1. Target application is NOT brought to the front (ActivateApp is suppressed).
2. The user's foreground application and window focus remain undisturbed.
3. Clicks are dispatched quietly via AXUIElement (quiet_press), NOT physical cursor moves.
4. Keystrokes/text are entered quietly (quiet_type) without stealing focus.
5. Focus-swapping is strictly prevented from being counted as background execution.
"""

from __future__ import annotations

import logging

from computeruse.orchestrator.loop import OodaRunner, WorkingState
from computeruse.orchestrator.schemas import (
    ActivateApp,
    AgentTurn,
    Finish,
    MouseClick,
    MouseMove,
    TypeText,
)
from computeruse.vision.coordinates import Point


def _turn(action: object, thought: str = "", sub_goal: str = "") -> AgentTurn:
    return AgentTurn.model_validate({"thought": thought, "sub_goal": sub_goal, "action": action})


def test_background_mode_suppresses_activate_app_and_preserves_foreground() -> None:
    """In background mode, activate_app must NOT pull the target app to the front."""
    physical_activations: list[str] = []

    def physical_executor(action: object) -> None:
        if isinstance(action, ActivateApp):
            physical_activations.append(action.app)

    quiet_pressed_points: list[Point] = []

    def fake_quiet_press(pt: Point) -> bool:
        quiet_pressed_points.append(pt)
        return True

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            # Model asks to activate Safari while running in background mode
            return _turn(ActivateApp(type="activate_app", app="Safari"), sub_goal="Switch to Safari")
        return _turn(Finish(type="finish", status="success", summary="Completed in background"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=physical_executor,
        app="Safari",
        quiet_press=fake_quiet_press,
        max_steps=5,
    )

    runner.run(goal="Perform background search in Safari")

    # Critical check: physical ActivateApp must NOT have been called!
    assert len(physical_activations) == 0, (
        f"ActivateApp was physically executed for {physical_activations}! "
        "Background mode must NOT disturb the user's foreground."
    )


def test_background_mode_routes_clicks_to_quiet_press_without_cursor_movement() -> None:
    """In background mode, clicks are handled via quiet_press (AX), never moving physical cursor."""
    physical_clicks: list[MouseClick] = []

    def physical_executor(action: object) -> None:
        if isinstance(action, MouseClick):
            physical_clicks.append(action)

    quiet_presses: list[Point] = []

    def fake_quiet_press(pt: Point) -> bool:
        quiet_presses.append(pt)
        return True

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=450, y=230), sub_goal="Click button quietly")
        return _turn(Finish(type="finish", status="success", summary="Clicked"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=physical_executor,
        app="Safari",
        quiet_press=fake_quiet_press,
        max_steps=5,
    )

    runner.run(goal="Quiet click in background")

    # Physical mouse movement/click must be ZERO
    assert len(physical_clicks) == 0, "Physical mouse click occurred during background mode!"
    # Quiet press must have received the coordinates
    assert len(quiet_presses) == 1
    assert quiet_presses[0].x == 450
    assert quiet_presses[0].y == 230


def test_background_mode_routes_typing_to_quiet_type_without_keyboard_focus() -> None:
    """In background mode, text input is delivered directly via quiet_type without hijacking keyboard."""
    physical_keystrokes: list[TypeText] = []

    def physical_executor(action: object) -> None:
        if isinstance(action, TypeText):
            physical_keystrokes.append(action)

    quiet_typed_strings: list[str] = []

    def fake_quiet_type(text: str) -> bool:
        quiet_typed_strings.append(text)
        return True

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(TypeText(type="type_text", text="background search query"), sub_goal="Type text quietly")
        return _turn(Finish(type="finish", status="success", summary="Typed"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=physical_executor,
        app="Safari",
        quiet_press=lambda _pt: True,
        quiet_type=fake_quiet_type,
        max_steps=5,
    )

    runner.run(goal="Quiet typing in background")

    assert len(physical_keystrokes) == 0, "Physical keyboard stroke was emitted in background mode!"
    assert quiet_typed_strings == ["background search query"]


def test_end_to_end_background_task_completes_without_focus_swapping() -> None:
    """A full multi-step task in background mode runs to completion with zero foreground disruptions."""
    foreground_disruptions = 0

    def physical_executor(action: object) -> None:
        nonlocal foreground_disruptions
        if isinstance(action, (ActivateApp, MouseClick, TypeText)):
            foreground_disruptions += 1

    quiet_log: list[str] = []

    def fake_quiet_press(pt: Point) -> bool:
        quiet_log.append(f"press({pt.x}, {pt.y})")
        return True

    def fake_quiet_type(text: str) -> bool:
        quiet_log.append(f"type({text})")
        return True

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(ActivateApp(type="activate_app", app="Safari"), sub_goal="Target Safari")
        if state.step_index == 1:
            return _turn(MouseClick(type="mouse_click", x=300, y=150), sub_goal="Focus search field")
        if state.step_index == 2:
            return _turn(TypeText(type="type_text", text="autonomous ai"), sub_goal="Input query")
        return _turn(Finish(type="finish", status="success", summary="Completed entire task quietly"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=physical_executor,
        app="Safari",
        quiet_press=fake_quiet_press,
        quiet_type=fake_quiet_type,
        max_steps=10,
    )

    runner.run(goal="Research autonomous ai in background")

    # Entire sequence completed
    assert foreground_disruptions == 0, "Foreground window or cursor was disturbed during background run!"
    assert quiet_log == ["press(300, 150)", "type(autonomous ai)"]


def test_background_mouse_move_is_loud_never_silent(caplog) -> None:
    """A cursor move has no accessibility equivalent, so background mode
    cannot do it quietly — and must not do it silently either.

    Observed risk (test 16's shape): a run labelled "background" that moves
    the user's cursor while claiming the foreground is undisturbed. The
    honest contract is loud-or-nothing: the mode warns AND fronts the
    target via a physical ActivateApp, so the disturbance is visible in
    the trace instead of masquerading as background execution.
    """
    physical: list[str] = []

    def physical_executor(action: object) -> None:
        physical.append(type(action).__name__)

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseMove(type="mouse_move", x=400, y=300), sub_goal="Move cursor")
        return _turn(Finish(type="finish", status="success", summary="Moved"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=physical_executor,
        app="Safari",
        quiet_press=lambda _pt: True,
        quiet_type=lambda _text: True,
        max_steps=5,
    )
    with caplog.at_level(logging.WARNING):
        runner.run(goal="Move the cursor in background")
    assert any("coming to the front" in r.getMessage() for r in caplog.records), (
        "background cursor move without a loud warning is silent disturbance"
    )
    assert "ActivateApp" in physical, (
        "a background cursor move must front the target loudly, never steal "
        "the cursor while claiming the foreground is undisturbed"
    )
