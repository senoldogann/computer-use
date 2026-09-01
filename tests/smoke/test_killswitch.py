"""Tests for Law 5: the emergency kill-switch.

The pure detector and the OODA gate are tested offline (no OS). ``monitor`` is
an imperative shell, exercised with an injected fake cursor so it stays fully
deterministic.
"""

from __future__ import annotations

import pytest

from computeruse.orchestrator.loop import KillSwitchTripped, OodaRunner, WorkingState
from computeruse.orchestrator.schemas import AgentTurn, TypeText
from computeruse.security.killswitch import (
    CursorSample,
    KillSwitch,
    MouseShakeMonitor,
    is_mouse_shake,
)


def _shake_points(count: int, axis: str = "x") -> list[CursorSample]:
    """Build a synthetic back-and-forth trace along one axis."""
    out: list[CursorSample] = []
    for i in range(count):
        direction = 1.0 if i % 2 == 0 else -1.0
        delta = direction * 30.0
        prev_x = out[-1].x if out else 0.0
        prev_y = out[-1].y if out else 0.0
        out.append(
            CursorSample(
                x=prev_x + (delta if axis == "x" else 0.0),
                y=prev_y + (delta if axis == "y" else 0.0),
                time=float(i) * 0.02,
            )
        )
    return out


def test_is_mouse_shake_detects_horizontal_burst() -> None:
    assert is_mouse_shake(_shake_points(12), min_reversals=6)


def test_is_mouse_shake_rejects_drifting_line() -> None:
    # A steady diagonal line — no reversals — must never trip.
    line = [
        CursorSample(x=float(i) * 3.0, y=float(i) * 1.5, time=float(i))
        for i in range(30)
    ]
    assert not is_mouse_shake(line, min_reversals=6)


def test_is_mouse_shake_rejects_short_trace() -> None:
    assert not is_mouse_shake(_shake_points(3), min_reversals=6)


def test_monitor_trips_on_oscillation() -> None:
    positions = iter(_shake_points(12))
    monitor = MouseShakeMonitor(
        lambda: next(positions), window_size=20, min_reversals=6
    )
    tripped = False
    # Feed every sample; by the end the window must contain the shake.
    try:
        for _ in range(12):
            if monitor.observe():
                tripped = True
                break
    except StopIteration:
        pass
    assert tripped


def test_mid_action_trip_aborts_before_any_physical_effect() -> None:
    """A kill-switch tripped *during* a long action stops it instantly.

    The cancellable executor receives the live ``is_cancelled`` poll; when it
    trips mid-action the run aborts and nothing is recorded as executed — a
    half-typed paste or half-drag must never enter the trajectory (F2).
    """

    class TripsOnThirdPoll:
        """Polls 1-2 (iteration top, pre-action) pass; poll 3 (mid-action) trips."""

        def __init__(self) -> None:
            self._polls = 0

        def tripped(self) -> bool:
            self._polls += 1
            return self._polls >= 3

    def provider(_state: WorkingState) -> AgentTurn:
        return AgentTurn(
            thought="",
            sub_goal="",
            action=TypeText(type="type_text", text="hello world", wpm=40),
        )

    physical_effects: list[str] = []

    def cancellable(action: object, is_cancelled: object) -> None:
        # The executor polls the live flag; the second poll (ours) trips, so
        # the action aborts before any key event is posted.
        if is_cancelled():  # type: ignore[operator]
            raise KillSwitchTripped("human reclaimed control mid-type")
        physical_effects.append(str(action))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        execute_physical_cancellable=cancellable,  # type: ignore[arg-type]
        kill_switch=TripsOnThirdPoll(),  # type: ignore[arg-type]
        max_steps=5,
    )
    with pytest.raises(KillSwitchTripped, match="mid-type"):
        runner.run(goal="type a message")
    assert physical_effects == [], "no physical effect may run after a mid-action trip"


def test_ooda_runner_raises_on_trip() -> None:
    calls = 0

    def provider(_state: WorkingState) -> AgentTurn:
        nonlocal calls
        calls += 1
        return AgentTurn.model_validate(
            {"thought": "", "sub_goal": "", "action": {"type": "mouse_move", "x": 1, "y": 2}}
        )

    def execute(_action: object) -> None:
        return None

    class AlwaysTripped:
        def tripped(self) -> bool:
            return True

    runner = OodaRunner(
        provider=provider,
        execute_physical=execute,
        kill_switch=AlwaysTripped(),  # type: ignore[arg-type]
        max_steps=5,
    )
    with pytest.raises(KillSwitchTripped):
        runner.run(goal="impossible")


def test_ooda_runner_without_kill_switch_is_unaffected() -> None:
    calls = 0

    def provider(state: WorkingState) -> AgentTurn:
        nonlocal calls
        calls += 1

        if state.step_index == 0:
            return AgentTurn.model_validate(
                {"thought": "", "sub_goal": "", "action": {"type": "mouse_move", "x": 1, "y": 2}}
            )
        return AgentTurn.model_validate(
            {"thought": "", "sub_goal": "", "action": {"type": "finish", "status": "success", "summary": "ok"}}
        )

    def execute(_action: object) -> None:
        return None

    runner = OodaRunner(provider=provider, execute_physical=execute, max_steps=5)
    final = runner.run(goal="demo")
    assert final.step_index >= 1


def test_killswitch_signal_predicate() -> None:
    # Without installing a real signal (which needs a main thread), a manual
    # flag is the closest deterministic proxy: tripped() must honor it.
    flag: list[bool] = [True]
    switch = KillSwitch(signal_triggered=flag[0], monitor=None)
    assert switch.tripped() is True


def test_killswitch_polls_signal_predicate_live() -> None:
    """A signal predicate installed at startup must be polled on every call,
    not frozen at construction — Ctrl-C mid-run has to trip the loop."""
    flag: list[bool] = [False]
    switch = KillSwitch(monitor=None, signal_predicate=lambda: flag[0])
    assert switch.tripped() is False
    flag[0] = True  # a SIGINT lands later, during the run
    assert switch.tripped() is True