"""P0-2: the display must stay awake while the agent owns the machine.

When macOS sleeps the display, the driver photographs darkness and clicks
land on a login screen — an overnight ``--autonomous`` session goes blind
without ever erroring. :class:`WakeLock` holds a ``caffeinate`` assertion
for exactly one ``with`` block, and these tests pin the lifecycle that
makes that safe: started with the display/idle/disk flags, SIGTERM on a
clean exit, SIGKILL when the child wedges, nothing orphaned when the body
raises, and total silence off macOS.

Every test drives a fake spawn — no test here may hold a real power
assertion on the host, and none may depend on the host's platform.
"""

from __future__ import annotations

import subprocess
from typing import cast

import pytest

from computeruse.power import CAFFEINATE_CMD, WakeLock


class _FakeProcess:
    """Stands in for ``Popen``: alive until signalled, hangs on demand."""

    def __init__(self, *, hang_on_wait: bool = False) -> None:
        self.signalled: list[str] = []
        self.wait_calls = 0
        self._returncode: int | None = None
        self._hang_on_wait = hang_on_wait

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.signalled.append("TERM")
        self._returncode = -15

    def kill(self) -> None:
        self.signalled.append("KILL")
        self._returncode = -9

    def wait(self, timeout: float | None = None) -> int | None:
        del timeout
        self.wait_calls += 1
        if self._hang_on_wait and self.wait_calls == 1:
            raise subprocess.TimeoutExpired(cmd="caffeinate", timeout=0.0)
        return self._returncode


def _spawned() -> tuple[list[tuple[tuple[object, ...], dict[str, object]]], _FakeProcess, WakeLock]:
    """A lock wired to a recording fake, plus the recordings."""
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    process = _FakeProcess()

    def spawn(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        calls.append((args, kwargs))
        return cast(subprocess.Popen[bytes], process)

    return calls, process, WakeLock(enabled=True, spawn=spawn)


def test_enter_spawns_caffeinate_with_display_idle_and_disk_flags() -> None:
    """The flags are the requirement: without -d the screen still locks."""
    calls, _process, lock = _spawned()
    with lock:
        assert lock.active
    # Exactly one positional: the argv list itself. Splatting it would land
    # "-d" in Popen's bufsize slot ("bufsize must be an integer" on 3.14) —
    # the fake accepts anything, so this assertion is the only thing that
    # would catch that shape against the real constructor.
    assert [c[0] for c in calls] == [(list(CAFFEINATE_CMD),)]
    assert CAFFEINATE_CMD == ("caffeinate", "-d", "-i", "-m")


def test_real_popen_argv_shape_against_a_harmless_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fake accepts any call shape; the real constructor does not.

    This test spawns through the real ``Popen`` against a two-word command,
    so a splatted argv — which dies with "bufsize must be an integer" —
    fails here instead of in the first nightly run. (A single-word probe
    would not catch it: the splat only breaks from the second word on.)
    It is the test that would have caught the shape bug the fakes missed.
    """
    import computeruse.power as power_module

    monkeypatch.setattr(power_module, "CAFFEINATE_CMD", ("echo", "shape-probe"))
    with WakeLock(enabled=True, spawn=subprocess.Popen):
        pass


def test_clean_exit_terminates_and_reaps_without_sigkill() -> None:
    """A healthy child gets SIGTERM and a reap — escalation is for wedges."""
    _calls, process, lock = _spawned()
    with lock:
        pass
    assert process.signalled == ["TERM"]
    assert process.wait_calls == 1
    assert not lock.active


def test_exception_in_body_still_terminates_the_child() -> None:
    """SIGINT, kill-switch takeover, budget stop — all unwind through __exit__.

    The exception must propagate untouched (the lock never suppresses), and
    the child must still be signalled: an orphaned caffeinate would hold the
    machine awake after its run died.
    """
    _calls, process, lock = _spawned()
    with pytest.raises(RuntimeError, match="boom"), lock:
        raise RuntimeError("boom")
    assert process.signalled == ["TERM"]
    assert not lock.active


def test_wedged_child_is_sigkilled_after_the_bounded_wait() -> None:
    """A child that ignores SIGTERM must not hold teardown hostage."""
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    process = _FakeProcess(hang_on_wait=True)

    def spawn(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        calls.append((args, kwargs))
        return cast(subprocess.Popen[bytes], process)

    with WakeLock(enabled=True, spawn=spawn):
        pass
    assert process.signalled == ["TERM", "KILL"]
    assert process.wait_calls == 2


def test_disabled_lock_spawns_nothing_and_exits_cleanly() -> None:
    """Off macOS the lock is silence: no process, no error, no behaviour."""
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def spawn(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        calls.append((args, kwargs))
        raise AssertionError("must not spawn when disabled")

    with WakeLock(enabled=False, spawn=spawn):
        pass
    assert calls == []


def test_missing_caffeinate_binary_degrades_instead_of_failing() -> None:
    """A stripped PATH must cost the assertion, never the run it guards."""

    def spawn(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        raise FileNotFoundError("caffeinate not on PATH")

    with WakeLock(enabled=True, spawn=spawn):
        pass


def test_already_dead_child_gets_no_signal() -> None:
    """Signalling a reaped pid risks hitting a stranger's process (cf. the
    stop watchdog's pid discipline) — a dead child is already the goal."""
    _calls, process, lock = _spawned()
    process._returncode = 0
    with lock:
        pass
    assert process.signalled == []


def test_double_exit_terminates_exactly_once() -> None:
    """__exit__ and the atexit backstop may run in either order — once total."""
    _calls, process, lock = _spawned()
    with lock:
        pass
    lock._terminate()
    assert process.signalled == ["TERM"]
