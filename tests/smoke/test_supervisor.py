"""Driver supervision (AUT-02): the process split only helps if something restarts.

ADR-1 puts actuation in its own process so a wedged OS call cannot take the
orchestrator with it. Nothing implemented the other half of that bargain, so
the first driver crash ended the run — every action after it failing as
``DRIVER_UNAVAILABLE`` until the recovery ladder gave up.

The policy is pure and tested on its own; the connector is tested against a
fake process, because the point is the lifecycle, not the binary.
"""

from __future__ import annotations

import subprocess
from typing import cast

import pytest

from computeruse.orchestrator.supervisor import (
    DriverSupervisor,
    DriverSupervisorExhaustedError,
    restart_verdict,
)


class _FakeProcess:
    """Stands in for ``Popen``: alive until ``die`` is called."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._returncode: int | None = None

    def die(self, code: int) -> None:
        self._returncode = code

    def poll(self) -> int | None:
        return self._returncode


def _as_popen(process: _FakeProcess) -> subprocess.Popen[bytes]:
    return cast(subprocess.Popen[bytes], process)


def _supervisor(
    processes: list[_FakeProcess], *, max_restarts: int
) -> tuple[DriverSupervisor, list[float]]:
    """A supervisor that hands out ``processes`` in order, recording its sleeps."""
    slept: list[float] = []
    spawned = iter(processes[1:])

    def spawn() -> subprocess.Popen[bytes]:
        return _as_popen(next(spawned))

    supervisor = DriverSupervisor(
        spawn,
        max_restarts=max_restarts,
        backoff_base_seconds=0.5,
        sleep=slept.append,
    )
    supervisor.adopt(_as_popen(processes[0]))
    return supervisor, slept


# --- policy -----------------------------------------------------------------


def test_the_first_restart_is_allowed_and_waits_the_base_backoff() -> None:
    verdict = restart_verdict(
        restarts_used=0, max_restarts=3, backoff_base_seconds=0.5
    )
    assert verdict.allowed
    assert verdict.wait_seconds == 0.5


def test_the_wait_doubles_with_each_restart() -> None:
    """A host that just killed the driver is usually still in that state."""
    waits = [
        restart_verdict(
            restarts_used=used, max_restarts=4, backoff_base_seconds=0.5
        ).wait_seconds
        for used in range(4)
    ]
    assert waits == [0.5, 1.0, 2.0, 4.0]


def test_the_ceiling_refuses_and_says_why() -> None:
    verdict = restart_verdict(
        restarts_used=3, max_restarts=3, backoff_base_seconds=0.5
    )
    assert not verdict.allowed
    assert "ceiling" in verdict.reason


def test_a_negative_ceiling_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        restart_verdict(restarts_used=0, max_restarts=-1, backoff_base_seconds=0.5)


# --- connector --------------------------------------------------------------


def test_a_living_driver_is_left_alone() -> None:
    """``ensure_alive`` is called on every failed connect; it must be cheap."""
    supervisor, slept = _supervisor([_FakeProcess(100)], max_restarts=3)
    supervisor.ensure_alive()
    assert supervisor.restarts_used == 0
    assert slept == []


def test_a_dead_driver_is_replaced() -> None:
    first, second = _FakeProcess(100), _FakeProcess(200)
    supervisor, slept = _supervisor([first, second], max_restarts=3)
    first.die(1)

    supervisor.ensure_alive()

    assert supervisor.restarts_used == 1
    assert supervisor.is_alive()
    assert slept == [0.5], "the respawn waits out the host's bad moment first"


def test_the_replacement_is_supervised_in_turn() -> None:
    """A driver that dies twice is brought back twice — the run keeps its hands."""
    first, second, third = _FakeProcess(100), _FakeProcess(200), _FakeProcess(300)
    supervisor, _ = _supervisor([first, second, third], max_restarts=3)

    first.die(1)
    supervisor.ensure_alive()
    second.die(1)
    supervisor.ensure_alive()

    assert supervisor.restarts_used == 2
    assert supervisor.is_alive()


def test_a_driver_that_keeps_dying_stops_being_restarted() -> None:
    """The bound is the point: a driver dying on every spawn has a cause a
    retry does not address (usually revoked Accessibility consent), and a
    supervisor without a ceiling would spin on it while the budget drained."""
    processes = [_FakeProcess(100 + i) for i in range(4)]
    supervisor, _ = _supervisor(processes, max_restarts=2)

    processes[0].die(1)
    supervisor.ensure_alive()
    processes[1].die(1)
    supervisor.ensure_alive()
    processes[2].die(1)

    with pytest.raises(DriverSupervisorExhaustedError, match="cannot be restarted"):
        supervisor.ensure_alive()
