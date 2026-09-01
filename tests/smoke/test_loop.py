"""Pure unit tests for the OODA loop (Law 6: no OS I/O).

``decide_step`` is a pure function, so these run without touching the driver.
The imperative shell (``OodaRunner``) tests use injected fakes so execution
stays deterministic and offline. The stuck-loop guard and the max-steps
termination contract are pinned here too: a degenerate provider must never
make the loop run forever, and a truncated run must never be silent.
"""

from __future__ import annotations

import logging

import pytest

from computeruse.orchestrator.loop import (
    MaxStepsError,
    OodaRunner,
    StuckLoopError,
    WorkingState,
    decide_step,
    repetition_diagnostic,
    same_physical_action,
)
from computeruse.orchestrator.schemas import (
    AgentTurn,
    Finish,
    MouseClick,
    MouseMove,
    Wait,
)
from computeruse.vision.focus import FocusedWindow


def _turn(action: object) -> AgentTurn:
    return AgentTurn.model_validate({"thought": "", "sub_goal": "", "action": action})

def _click(x: int, y: int) -> MouseClick:
    return MouseClick(type="mouse_click", x=x, y=y)


def test_decide_step_is_pure_and_routes_physical() -> None:
    start = WorkingState(goal="click")
    outcome = decide_step(start, _turn(MouseMove(type="mouse_move", x=10, y=10)))
    assert outcome.route == "physical"
    assert outcome.state.step_index == 1
    assert start.step_index == 0  # immutability: original untouched


def test_decide_step_routes_wait_as_internal() -> None:
    outcome = decide_step(
        WorkingState(goal="x"),
        _turn(Wait(type="wait", duration_ms=5, reason="settle")),
    )
    assert outcome.route == "internal_wait"


def test_runner_executes_physical_then_finishes() -> None:
    executed: list[str] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=1, y=1))
        return _turn(Finish(type="finish", status="success", summary="done"))

    def execute_physical(action: object) -> None:
        executed.append(str(action))

    runner = OodaRunner(provider=provider, execute_physical=execute_physical, max_steps=5)
    final = runner.run(goal="demo")
    assert final.step_index == 2
    assert executed, "physical action was never dispatched"


def test_runner_folds_failure_into_state() -> None:
    """A raising execute_physical failure must survive into the next state."""
    seen: list[str | None] = []

    def failing_provider(state: WorkingState) -> AgentTurn:
        seen.append(state.last_error)
        return _turn(MouseMove(type="mouse_move", x=0, y=0))

    def boom(_action: object) -> None:
        raise RuntimeError("driver gone")

    runner = OodaRunner(provider=failing_provider, execute_physical=boom, max_steps=3)
    # The provider never finishes, so the run now ends loudly (bounded
    # termination) instead of silently returning a truncated state.
    with pytest.raises(MaxStepsError):
        runner.run(goal="retry")
    # The failure was folded into the provider's second state, not swallowed.
    assert seen[0] is None
    assert seen[1] is not None and "driver gone" in seen[1]


def test_same_physical_action_compares_full_payload() -> None:
    """Two clicks are identical only when every parameter matches."""
    assert same_physical_action(_click(10, 10), _click(10, 10))
    assert not same_physical_action(_click(10, 10), _click(11, 10))
    assert not same_physical_action(_click(10, 10), MouseMove(type="mouse_move", x=10, y=10))
    assert "action repetition detected" in repetition_diagnostic(_click(10, 10), 3)


def test_stuck_loop_injects_corrective_hint_after_two() -> None:
    """Two identical clicks with no screen change fold a corrective hint."""
    seen: list[str | None] = []
    executed: list[tuple[int, int]] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state.last_error)
        if state.step_index >= 3:
            return _turn(Finish(type="finish", status="success", summary="ok"))
        return _turn(_click(42, 42))

    def execute_physical(action: object) -> None:
        if isinstance(action, MouseClick):
            executed.append((action.x, action.y))

    runner = OodaRunner(provider=provider, execute_physical=execute_physical, max_steps=10)
    final = runner.run(goal="click")
    assert final.step_index == 4
    # The hint appears after the 3rd identical click (streak hits REPEAT_WARN_AFTER=2
    # after the 3rd click: 1st→0, 2nd→1, 3rd→2=warn).
    hints = [e for e in seen if e is not None and "action repetition detected" in e]
    assert len(hints) == 1
    assert "emit finish" in hints[0]  # type: ignore[operator]
    assert executed == [(42, 42), (42, 42), (42, 42)]


def test_stuck_loop_aborts_before_fourth_execution() -> None:
    """A model that never varies aborts before the 4th identical click.

    Streak: 1st click→0, 2nd→1, 3rd→2 (warn), 4th would be 3 → abort.
    The 4th is refused before it touches the physical layer.
    """
    executed: list[tuple[int, int]] = []

    def provider(state: WorkingState) -> AgentTurn:
        return _turn(_click(7, 7))

    def execute_physical(action: object) -> None:
        if isinstance(action, MouseClick):
            executed.append((action.x, action.y))

    runner = OodaRunner(provider=provider, execute_physical=execute_physical, max_steps=10)
    with pytest.raises(StuckLoopError, match="stuck loop"):
        runner.run(goal="click")
    # The abort fires at decision time: only 3 clicks ever ran.
    assert len(executed) == 3


def test_stuck_guard_ignores_distinct_actions() -> None:
    """Alternating targets never trip the guard."""
    executed: list[tuple[int, int]] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index >= 6:
            return _turn(Finish(type="finish", status="success", summary="ok"))
        x = 10 if state.step_index % 2 == 0 else 20
        return _turn(_click(x, x))

    def execute_physical(action: object) -> None:
        if isinstance(action, MouseClick):
            executed.append((action.x, action.y))

    runner = OodaRunner(provider=provider, execute_physical=execute_physical, max_steps=10)
    final = runner.run(goal="alternate")
    assert final.step_index == 7
    assert len(executed) == 6


def test_mouse_move_repetition_is_not_a_stuck_signal() -> None:
    """Repeated identical moves (cursor positioning) never trip the guard."""
    executed: list[int] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index >= 5:
            return _turn(Finish(type="finish", status="success", summary="ok"))
        return _turn(MouseMove(type="mouse_move", x=100, y=100))

    def execute_physical(action: object) -> None:
        if isinstance(action, MouseMove):
            executed.append(action.x)

    runner = OodaRunner(provider=provider, execute_physical=execute_physical, max_steps=10)
    final = runner.run(goal="position")
    assert final.step_index == 6
    assert len(executed) == 5


def test_max_steps_raises_instead_of_silent_stop() -> None:
    """Exhausting max_steps surfaces a typed error, not a silent return."""

    def provider(state: WorkingState) -> AgentTurn:
        # Alternate coordinates so the stuck-loop guard never fires;
        # this test is about max_steps termination, not repetition.
        x = 1 + state.step_index
        return _turn(_click(x, x))

    def execute_physical(_action: object) -> None:
        return None

    runner = OodaRunner(provider=provider, execute_physical=execute_physical, max_steps=3)
    with pytest.raises(MaxStepsError, match="max_steps=3"):
        runner.run(goal="never")


def test_runner_logs_executed_physical_step(caplog) -> None:
    """Every executed physical action is visible at INFO (live run UX)."""

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(_click(42, 42))
        return _turn(Finish(type="finish", status="success", summary="ok"))

    def execute_physical(_action: object) -> None:
        return None

    with caplog.at_level(logging.INFO, logger="computeruse.orchestrator.loop"):
        runner = OodaRunner(provider=provider, execute_physical=execute_physical, max_steps=5)
        runner.run(goal="demo")
    # The interactive user sees the step + payload streaming on stderr while
    # the run is live — a silent terminal reads as "nothing is happening".
    assert "step_0:mouse_click" in caplog.text
    assert "'x': 42" in caplog.text


def test_repeated_action_with_changing_screen_is_not_stuck() -> None:
    """Same action repeated, but the screen IS changing → not stuck.

    A repeated action against a changing screen is legitimate (e.g. clicking
    through a multi-step wizard where each click advances a page). The streak
    resets when the screen fingerprint differs from the previous step.
    """
    executed: list[tuple[int, int]] = []
    seen_errors: list[str | None] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen_errors.append(state.last_error)
        if state.step_index >= 6:
            return _turn(Finish(type="finish", status="success", summary="ok"))
        return _turn(_click(50, 50))

    def execute_physical(action: object) -> None:
        if isinstance(action, MouseClick):
            executed.append((action.x, action.y))

    # Each step's state gets a different active_window, simulating screen change.
    def window_probe():
        return FocusedWindow(pid=1, app_name="App", window_title=f"page_{len(executed)}")

    runner = OodaRunner(
        provider=provider,
        execute_physical=execute_physical,
        window_probe=window_probe,
        max_steps=10,
    )
    final = runner.run(goal="click through wizard")
    assert final.step_index == 7
    assert len(executed) == 6
    # No stuck-loop hint was ever injected (screen was changing each step).
    hints = [e for e in seen_errors if e is not None and "action repetition" in e]
    assert hints == []