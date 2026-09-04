"""Keep the display awake while the agent works (Law 1 reachability).

An unattended run is worthless if the machine sleeps through it: when macOS
dims and locks the display, ``CGDisplay`` goes dark and ``CGEvent`` lands on
the login screen, so ``--autonomous`` spends the night blind. Holding a
``caffeinate`` assertion for the duration of a run keeps display sleep, idle
sleep and disk sleep off while the agent owns the machine.

:class:`WakeLock` is the whole mechanism, and it is deliberately a context
manager rather than start/stop calls: a run can end normally, by exception,
by ``SIGINT`` or by kill-switch takeover, and every one of those paths runs
``__exit__``. ``atexit`` registration is the backstop for the one ending a
context manager cannot cover — the interpreter itself going down — so a
``caffeinate`` child is never orphaned into holding the machine awake after
its run is gone.
"""

from __future__ import annotations

import atexit
import logging
import subprocess
import sys
from collections.abc import Callable
from types import TracebackType
from typing import Final, Self

LOGGER: Final = logging.getLogger(__name__)

#: What we ask macOS to hold off: display sleep (-d), idle sleep (-i) and
#: disk sleep (-m), for as long as the child lives. The flags are the
#: requirement, not an optimisation — without -d the screen still locks.
CAFFEINATE_CMD: Final[tuple[str, ...]] = ("caffeinate", "-d", "-i", "-m")

#: How long a SIGTERM gets to work before SIGKILL. caffeinate exits on
#: SIGTERM immediately when healthy; the wait is for a wedged child, and a
#: wedged child must not hold ``__exit__`` (and therefore the run's teardown)
#: hostage.
TERMINATE_TIMEOUT_SECONDS: Final[float] = 5.0

#: Injectable process factory, so tests can assert the lifecycle without
#: ever spawning a real assertion on the host.
SpawnFn = Callable[..., "subprocess.Popen[bytes]"]


class WakeLock:
    """A display-wake assertion held for exactly one ``with`` block.

    On macOS (``enabled``) entering spawns ``caffeinate`` and exiting
    terminates it — SIGTERM first, SIGKILL after a bounded wait — then
    reaps it, so no zombie survives the run. Anywhere else (or when the
    binary is missing) entering degrades to a no-op that still exits
    cleanly: a missing power assertion must never fail a run, it only
    leaves the host's own sleep policy in charge.
    """

    def __init__(self, *, enabled: bool, spawn: SpawnFn) -> None:
        self._enabled = enabled
        self._spawn = spawn
        self._process: subprocess.Popen[bytes] | None = None
        self._registered_hook: Callable[[], None] | None = None

    @property
    def active(self) -> bool:
        """Whether this lock currently holds a live child (introspection)."""
        return self._process is not None and self._process.poll() is None

    def __enter__(self) -> Self:
        if not self._enabled:
            return self
        try:
            # The argv goes as ONE argument: Popen's second positional is
            # bufsize, so splatting the tuple would land "-d" in it and
            # die with "bufsize must be an integer" (measured on 3.14).
            self._process = self._spawn(
                list(CAFFEINATE_CMD),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, ValueError) as exc:
            # No caffeinate (stripped PATH, minimal container): degrade with
            # the reason attached, never fail the run over a power hint.
            LOGGER.warning("wake lock unavailable (%s); running without one", exc)
            return self
        # One stored reference, used for both register and unregister: the
        # backstop must remove exactly what entering installed.
        self._registered_hook = self._terminate
        atexit.register(self._registered_hook)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # The interpreter-shutdown backstop is this context's job now that
        # it is closing normally; unregistering a missing hook is a no-op.
        if self._registered_hook is not None:
            atexit.unregister(self._registered_hook)
            self._registered_hook = None
        self._terminate()

    def _terminate(self) -> None:
        """Stop the child if any, escalating to SIGKILL when it wedges.

        Idempotent: safe to call from ``__exit__`` and from ``atexit`` in
        either order, and safe when the child already exited on its own.
        """
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError as exc:
            # Died between the poll and the signal: already the state we
            # want, with no child left to reap beyond the wait below.
            LOGGER.debug("wake lock child already gone: %s", exc)
        try:
            process.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            LOGGER.warning(
                "wake lock child ignored SIGTERM; sending SIGKILL"
            )
            try:
                process.kill()
            except OSError as exc:
                LOGGER.debug("wake lock child already gone: %s", exc)
                return
            try:
                process.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
            except (subprocess.TimeoutExpired, OSError) as exc:
                LOGGER.warning("wake lock child could not be reaped: %s", exc)
        except OSError as exc:
            LOGGER.debug("wake lock wait failed: %s", exc)


def wake_lock() -> WakeLock:
    """The production wake lock: armed on macOS, silent elsewhere.

    The composition root (``cli.py``) holds this for a run; tests drive
    :class:`WakeLock` directly with explicit arguments instead.
    """
    return WakeLock(enabled=sys.platform == "darwin", spawn=subprocess.Popen)
