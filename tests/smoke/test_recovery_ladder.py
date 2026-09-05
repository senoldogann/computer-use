"""P1: a frozen app gets first aid, not an infinite loop of clicks.

When macOS stops answering an app's accessibility tree while its windows
stay visible (spinning beachball), the old loop degraded each snapshot to
the previous context and kept clicking into the hang until the step budget
ran out — burning a whole unattended run on a beachball. These tests pin
the replacement contract:

* repeated AX silence with live windows classifies ``APP_FROZEN``;
* the first rung presses Escape and the second re-asserts the app, both
  emitted by the loop itself rather than suggested to the model;
* the run still ends boundedly (REPLAN, then ABORT) with the frozen cause;
* a single hiccup, a dead driver, and a human takeover never arm the reflex.

All offline: fake probes and a recording ``execute_physical``.
"""

from __future__ import annotations

from typing import Self

from computeruse.orchestrator.client import DriverRpcError
from computeruse.orchestrator.failures import (
    FailureKind,
    RecoveryAction,
    UnrecoverableFailureError,
    classify_failure,
    recovery_for,
)
from computeruse.orchestrator.loop import (
    AppFrozenError,
    OodaRunner,
    WorkingState,
)
from computeruse.orchestrator.schemas import AgentTurn, Finish, Wait
from computeruse.security.killswitch import KillSwitch
from computeruse.vision.focus import FocusedWindow

_FROZEN_APP = "Vim"


def _turn(action: object) -> AgentTurn:
    return AgentTurn.model_validate({"thought": "", "sub_goal": "", "action": action})


def _dead_ax() -> object:
    raise DriverRpcError("ax_snapshot", "the application is not responding")


def _live_window() -> FocusedWindow:
    return FocusedWindow(pid=1, app_name=_FROZEN_APP, window_title="frozen")


def _wait_then_give_up(state: WorkingState) -> AgentTurn:
    if state.step_index == 0:
        return _turn(Wait(type="wait", duration_ms=0, reason="settle"))
    return _turn(Finish(type="finish", status="failed", summary="stuck"))


def _keep_waiting(state: WorkingState) -> AgentTurn:
    """An actor that keeps working while its app hangs — the beachball trap.

    Unlike giving up (which ends the run on the next finish), endless Waits
    let the frozen signal accumulate until the ladder itself ends the run.
    """
    del state
    return _turn(Wait(type="wait", duration_ms=0, reason="waiting on the app"))


# --- classification ------------------------------------------------------------


def test_app_frozen_error_classifies_as_app_frozen() -> None:
    """The runner's own signal must land on its own failure kind."""
    failure = classify_failure(AppFrozenError("not answering"), None)
    assert failure.kind is FailureKind.APP_FROZEN


def test_frozen_failures_climb_the_existing_ladder_unchanged() -> None:
    """No ladder fork: escape rides RETRY, refocus rides ALTERNATE."""
    assert recovery_for(1) is RecoveryAction.RETRY
    assert recovery_for(2) is RecoveryAction.ALTERNATE
    assert recovery_for(3) is RecoveryAction.REPLAN
    assert recovery_for(4) is RecoveryAction.ABORT


# --- the deterministic reflex ----------------------------------------------------


def test_first_rung_presses_escape_without_asking_the_model() -> None:
    """A stuck sheet/modal/menu is what Escape is for — no LLM turn spent."""
    acted: list[object] = []
    runner = OodaRunner(
        provider=_wait_then_give_up,
        execute_physical=acted.append,
        app=_FROZEN_APP,
    )
    runner._register_failure(AppFrozenError("not answering"), None, "edit")
    assert len(acted) == 1
    (action,) = acted
    assert action.type == "press_hotkey"
    assert action.key == "escape"  # type: ignore[union-attr]
    assert action.modifiers == []  # type: ignore[union-attr]


def test_second_rung_reasserts_the_app_to_the_front() -> None:
    """Focus drift reads identically to a freeze from the AX side."""
    acted: list[object] = []
    runner = OodaRunner(
        provider=_wait_then_give_up,
        execute_physical=acted.append,
        app=_FROZEN_APP,
    )
    runner._register_failure(AppFrozenError("not answering"), None, "edit")
    runner._register_failure(AppFrozenError("not answering"), None, "edit")
    assert [a.type for a in acted] == ["press_hotkey", "activate_app"]
    assert acted[1].app == _FROZEN_APP  # type: ignore[union-attr]


def test_reflex_never_fires_during_a_human_takeover() -> None:
    """First aid during a takeover would fight the human for the machine."""
    acted: list[object] = []
    runner = OodaRunner(
        provider=_wait_then_give_up,
        execute_physical=acted.append,
        app=_FROZEN_APP,
        kill_switch=KillSwitch(monitor=None, signal_triggered=True),
    )
    hint = runner._register_failure(AppFrozenError("not answering"), None, "edit")
    assert acted == []
    assert hint


# --- the full stuck-app run --------------------------------------------------------


class _expect_frozen:
    """Context manager asserting the run aborts naming the frozen app."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, _tb: object) -> bool:
        assert isinstance(exc, UnrecoverableFailureError)
        assert exc.failure.kind is FailureKind.APP_FROZEN
        return True


def test_hung_app_ends_bounded_with_the_frozen_cause() -> None:
    """Three silent snapshots arm the signal; the ladder ends the run loudly.

    Two degrading turns (below the threshold), then escape, refocus, a
    replan hint, and ABORT — a beachball costs six turns, never the budget.
    """
    acted: list[object] = []
    runner = OodaRunner(
        provider=_keep_waiting,
        execute_physical=acted.append,
        app=_FROZEN_APP,
        ax_probe=_dead_ax,  # type: ignore[arg-type]
        window_probe=_live_window,
        max_steps=20,
    )
    with _expect_frozen():
        runner.run(goal="edit the file")


def test_reflex_order_is_escape_then_refocus() -> None:
    """End-to-end shape of the first aid, in emission order."""
    acted: list[object] = []
    runner = OodaRunner(
        provider=_keep_waiting,
        execute_physical=acted.append,
        app=_FROZEN_APP,
        ax_probe=_dead_ax,  # type: ignore[arg-type]
        window_probe=_live_window,
        max_steps=20,
    )
    try:
        runner.run(goal="edit the file")
    except UnrecoverableFailureError:
        pass
    kinds = [a.type for a in acted]  # type: ignore[union-attr]
    assert kinds[:2] == ["press_hotkey", "activate_app"]


# --- what must NOT arm the signal ----------------------------------------------------


def test_single_hiccup_never_declares_a_freeze() -> None:
    """One failed snapshot is a driver hiccup and stays best-effort."""
    calls = {"count": 0}

    def flaky_ax() -> object:
        calls["count"] += 1
        if calls["count"] == 1:
            raise DriverRpcError("ax_snapshot", "transient hiccup")
        raise AssertionError("ax_probe called after recovery; summaries should flow")

    acted: list[object] = []
    runner = OodaRunner(
        provider=_wait_then_give_up,
        execute_physical=acted.append,
        app=_FROZEN_APP,
        ax_probe=flaky_ax,  # type: ignore[arg-type]
        window_probe=_live_window,
        max_steps=5,
    )
    final = runner.run(goal="edit the file")
    assert final.completed_steps
    assert [a.type for a in acted] == []  # type: ignore[union-attr]


def test_dead_driver_reads_as_driver_down_not_frozen_app() -> None:
    """Both probes failing means the driver is gone — a different failure."""
    acted: list[object] = []

    def dead_window() -> object:
        raise DriverRpcError("ax_snapshot", "cannot reach driver")

    runner = OodaRunner(
        provider=_wait_then_give_up,
        execute_physical=acted.append,
        app=_FROZEN_APP,
        ax_probe=_dead_ax,  # type: ignore[arg-type]
        window_probe=dead_window,  # type: ignore[arg-type]
        max_steps=5,
    )
    final = runner.run(goal="edit the file")
    assert final.completed_steps
    assert [a.type for a in acted] == []  # type: ignore[union-attr]


def test_different_marks_are_different_failure_targets() -> None:
    """Failing on [1] then trying [2] is new work, not one target refusing twice.

    Every ClickMark used to collapse to the bare action type, so the streak
    counted four *different* elements as one repeat and the ladder aborted a
    run that had never retried the same thing. The mark is the target's
    identity here, the way a coordinate is for a raw click.
    """
    from computeruse.orchestrator.failures import classify_failure
    from computeruse.orchestrator.schemas import ClickMark

    exc = RuntimeError("element did not respond")
    first = classify_failure(exc, ClickMark(type="click_mark", mark=1))
    second = classify_failure(exc, ClickMark(type="click_mark", mark=2))
    same = classify_failure(exc, ClickMark(type="click_mark", mark=1))

    assert first.signature != second.signature
    assert first.signature == same.signature
