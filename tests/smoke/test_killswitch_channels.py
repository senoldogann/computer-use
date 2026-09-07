"""Separated verification for each emergency kill-switch channel (Law 5 Hardening).

Independently tests and verifies:
1. SIGINT / Ctrl-C signal channel.
2. Command+Shift+Escape global hotkey tap channel (driver RPC).
3. Physical mouse reclaim / shake gesture channel.
4. Immediate cessation of hardware actuation and absence of false trips.
"""

from __future__ import annotations

import pytest

from computeruse.orchestrator.loop import KillSwitchTripped, OodaRunner, WorkingState
from computeruse.orchestrator.schemas import AgentTurn, MouseClick, TypeText
from computeruse.security.killswitch import (
    CursorSample,
    KillSwitch,
    MouseShakeMonitor,
)


def _turn(action: object) -> AgentTurn:
    return AgentTurn.model_validate({"thought": "killswitch test", "sub_goal": "action", "action": action})


def test_channel_1_sigint_interruption() -> None:
    """Channel 1: SIGINT / Ctrl-C terminates execution cleanly without hardware leakage."""
    sigint_delivered = False

    def sigint_poll() -> bool:
        return sigint_delivered

    kill_switch = KillSwitch(monitor=None, signal_predicate=sigint_poll)
    executed_hardware_actions: list[str] = []

    def physical_executor(action: object) -> None:
        executed_hardware_actions.append(str(action))

    step_count = 0

    def provider(state: WorkingState) -> AgentTurn:
        nonlocal step_count, sigint_delivered
        step_count += 1
        if state.step_index == 1:
            # Deliver SIGINT at step 1
            sigint_delivered = True
        return _turn(MouseClick(type="mouse_click", x=100 + state.step_index, y=100))

    runner = OodaRunner(
        provider=provider,
        execute_physical=physical_executor,
        kill_switch=kill_switch,
        max_steps=5,
    )

    # Must raise KillSwitchTripped on step 2 before physical execution
    with pytest.raises(KillSwitchTripped):
        runner.run(goal="sigint kill test")

    assert step_count == 2
    # Step 0 succeeded, but step 1 was aborted immediately when SIGINT was detected
    assert len(executed_hardware_actions) == 1, "Hardware execution must stop immediately upon SIGINT"


def test_channel_2_global_hotkey_interruption() -> None:
    """Channel 2: Command+Shift+Escape hotkey (driver CGEventTap) halts execution instantly."""
    driver_hotkey_tripped = False

    def driver_hotkey_rpc() -> bool:
        return driver_hotkey_tripped

    # Wired exactly as agent.py wires the driver hotkey
    base_switch = KillSwitch(monitor=None, signal_predicate=None)
    wired_switch = base_switch.with_signal_predicate(driver_hotkey_rpc)

    executed_hardware_actions: list[str] = []

    def physical_executor(action: object) -> None:
        executed_hardware_actions.append(str(action))

    step_count = 0

    def provider(state: WorkingState) -> AgentTurn:
        nonlocal step_count, driver_hotkey_tripped
        step_count += 1
        if state.step_index == 0:
            # User presses Command+Shift+Escape during the first step
            driver_hotkey_tripped = True
        return _turn(TypeText(type="type_text", text=f"step_{state.step_index}"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=physical_executor,
        kill_switch=wired_switch,
        max_steps=5,
    )

    with pytest.raises(KillSwitchTripped):
        runner.run(goal="hotkey kill test")

    # Step 0's turn ran, but before step 1 could execute physical actuation, hotkey tripped
    assert step_count == 1
    assert len(executed_hardware_actions) == 0, 'Hotkey must prevent physical action'


def test_channel_3_mouse_shake_reclaim_interruption() -> None:
    """Channel 3: Human physical takeover via rapid cursor shake halts the agent immediately."""
    # Build oscillatory mouse shake trace
    shake_trace: list[CursorSample] = []
    for i in range(14):
        direction = 1.0 if i % 2 == 0 else -1.0
        shake_trace.append(
            CursorSample(
                x=500.0 + (direction * 35.0),
                y=500.0,
                time=float(i) * 0.02,
            )
        )

    trace_iter = iter(shake_trace)

    def live_cursor_poll() -> CursorSample:
        try:
            return next(trace_iter)
        except StopIteration:
            return shake_trace[-1]

    shake_monitor = MouseShakeMonitor(live_cursor_poll, window_size=20, min_reversals=6)
    kill_switch = KillSwitch(monitor=shake_monitor)

    executed_hardware_actions: list[str] = []

    def physical_executor(action: object) -> None:
        executed_hardware_actions.append(str(action))

    def provider(state: WorkingState) -> AgentTurn:
        return _turn(MouseClick(type="mouse_click", x=50, y=50))

    runner = OodaRunner(
        provider=provider,
        execute_physical=physical_executor,
        kill_switch=kill_switch,
        max_steps=20,
    )

    with pytest.raises(KillSwitchTripped):
        runner.run(goal="shake reclaim test")

    # The loop should have terminated as soon as reversals accumulated in the monitor
    assert len(executed_hardware_actions) < 10, "Loop must terminate promptly on human mouse shake"


def test_channels_are_independent_and_composable() -> None:
    """All three channels can be composed together without interfering with one another."""
    sigint_tripped = False
    hotkey_tripped = False

    idle_samples = [CursorSample(x=10.0, y=10.0, time=float(i)) for i in range(20)]
    idle_iter = iter(idle_samples)

    shake_monitor = MouseShakeMonitor(
        lambda: next(idle_iter, idle_samples[-1]),
        window_size=20,
        min_reversals=6,
    )

    switch = KillSwitch(monitor=shake_monitor, signal_predicate=lambda: sigint_tripped)
    switch = switch.with_signal_predicate(lambda: hotkey_tripped)

    # 1. Initially none are tripped
    assert switch.tripped() is False

    # 2. Hotkey alone trips it
    hotkey_tripped = True
    assert switch.tripped() is True
    hotkey_tripped = False
    assert switch.tripped() is False

    # 3. SIGINT alone trips it
    sigint_tripped = True
    assert switch.tripped() is True
    sigint_tripped = False
    assert switch.tripped() is False
