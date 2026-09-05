"""Keeping the actuation driver alive for the length of a run (ADR-1 shell).

ADR-1 puts actuation in a separate process precisely so that a wedged CGEvent
tap or a hung OS call takes down the driver and not the orchestrator. That
argument only holds if something brings the driver back: a run whose driver
died is a run with no hands, and every subsequent action fails as
``DRIVER_UNAVAILABLE`` until the recovery ladder gives up. The README claimed
this supervision existed; nothing implemented it, so the first driver crash
ended the run — the exact outcome the process split was chosen to avoid.

A restart is bounded and pure-policy-driven. :func:`restart_verdict` answers
"may we try again, and how long should we wait first?" with no clock, no
process and no filesystem, so the policy is testable on its own;
:class:`DriverSupervisor` is the connector that owns the child process and
acts on that answer.

The bound matters as much as the restart. A driver that dies immediately on
every spawn is telling us something a retry cannot fix — usually revoked
Accessibility consent — and a supervisor without a ceiling would spin on it
forever while the run's real budget drained away.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

LOGGER: Final = logging.getLogger(__name__)

#: How many times one run may bring the driver back. Three covers the failure
#: this exists for — a transient OS-level wedge — while a driver that cannot
#: survive three spawns has a cause the orchestrator cannot address.
DEFAULT_MAX_RESTARTS: Final[int] = 3

#: First backoff before a respawn. Doubles per attempt: the host has usually
#: just done something unpleasant (a tap timeout, a display reconfiguration),
#: and respawning into the middle of it repeats the crash.
RESTART_BACKOFF_BASE_SECONDS: Final[float] = 0.5

#: How long a wedged driver is given to exit on SIGTERM before SIGKILL. Long
#: enough for a healthy process to unwind its socket and release the event
#: tap, short enough that a run is not held hostage by one that will not.
TERMINATE_GRACE_SECONDS: Final[float] = 3.0


@dataclass(frozen=True)
class RestartVerdict:
    """Whether a dead driver may be respawned, and after how long (pure data)."""

    allowed: bool
    wait_seconds: float
    reason: str


def restart_verdict(
    *, restarts_used: int, max_restarts: int, backoff_base_seconds: float
) -> RestartVerdict:
    """Pure: may the driver be brought back, and how long should we wait first?

    ``restarts_used`` counts restarts already performed in this run, so the
    first call after a crash passes ``0``. The wait grows exponentially with
    the count for the reason in the module docstring: a host that just killed
    the driver is usually still in the state that killed it.
    """
    if max_restarts < 0:
        raise ValueError(f"max_restarts must not be negative, got {max_restarts}")
    if restarts_used >= max_restarts:
        return RestartVerdict(
            allowed=False,
            wait_seconds=0.0,
            reason=(
                f"the driver has already been restarted {restarts_used} time(s) this "
                f"run, which is the configured ceiling ({max_restarts}); it is "
                "failing for a reason a restart does not fix"
            ),
        )
    wait = backoff_base_seconds * (2**restarts_used)
    return RestartVerdict(
        allowed=True,
        wait_seconds=wait,
        reason=f"restart {restarts_used + 1} of {max_restarts} after {wait:.1f}s",
    )


class DriverSupervisorExhaustedError(RuntimeError):
    """The driver died and may not be restarted again.

    Distinct from :class:`~computeruse.orchestrator.client.DriverConnectionError`
    on purpose: that one means "could not reach the driver", which a restart
    might fix, while this one means "the driver keeps dying", which it will not.
    """


class DriverSupervisor:
    """Owns the driver child process and brings it back when it dies (connector).

    Deliberately narrow: it knows how to spawn one process, whether that
    process is alive, and how many times it has already been replaced. It does
    not know about sockets, actions or the OODA loop — the client calls
    :meth:`ensure_alive` when it cannot reach the driver, and everything else
    stays where it was.

    ``spawn`` is injected rather than built here so the CLI keeps owning the
    argv, the stderr drain and the socket-readiness wait it already implements,
    and so a test can supervise a fake process without a binary on disk.
    """

    def __init__(
        self,
        spawn: Callable[[], subprocess.Popen[bytes]],
        *,
        max_restarts: int,
        backoff_base_seconds: float,
        sleep: Callable[[float], None],
    ) -> None:
        self._spawn = spawn
        self._max_restarts = max_restarts
        self._backoff_base_seconds = backoff_base_seconds
        self._sleep = sleep
        self._process: subprocess.Popen[bytes] | None = None
        self._restarts_used = 0

    @property
    def restarts_used(self) -> int:
        """How many times the driver has been replaced during this run."""
        return self._restarts_used

    def adopt(self, process: subprocess.Popen[bytes]) -> None:
        """Take ownership of the process the run started with.

        Separate from construction because the first driver is spawned before
        anything needs supervising, and adopting it costs no restart: the run
        began with a live driver, and the ceiling counts replacements.
        """
        self._process = process

    def is_alive(self) -> bool:
        """Is the supervised process still running?

        ``None`` from ``poll()`` means running; anything else is the exit
        status of a process that has already gone. With no process adopted at
        all the answer is no — there is nothing alive to talk to.
        """
        if self._process is None:
            return False
        return self._process.poll() is None

    def ensure_alive(self) -> None:
        """Respawn the driver if it has died; do nothing if it has not.

        Raises :class:`DriverSupervisorExhaustedError` when the restart ceiling
        is reached, carrying the policy's own reason so the message says *why*
        no further attempt is being made rather than only that the driver is
        gone.
        """
        if self.is_alive():
            return
        exit_code = self._process.poll() if self._process is not None else None
        verdict = restart_verdict(
            restarts_used=self._restarts_used,
            max_restarts=self._max_restarts,
            backoff_base_seconds=self._backoff_base_seconds,
        )
        if not verdict.allowed:
            raise DriverSupervisorExhaustedError(
                f"actuation driver exited (code {exit_code}) and cannot be "
                f"restarted: {verdict.reason}"
            )
        LOGGER.warning(
            "actuation driver exited (code %s); %s",
            exit_code,
            verdict.reason,
        )
        self._sleep(verdict.wait_seconds)
        self._process = self._spawn()
        self._restarts_used += 1
        LOGGER.info("actuation driver restarted (pid %s)", self._process.pid)

    def restart_unresponsive(self) -> None:
        """End a driver that is running but not answering, then respawn it.

        ``ensure_alive`` returns early for a process that is still running,
        which is exactly right when the driver merely exited. A driver whose
        event tap or accessibility call has wedged looks identical from the
        outside — alive to ``poll()``, silent on the socket — and returning
        early there leaves the run retrying an RPC that will never be
        answered, until the step budget runs out.

        This is the client's recovery hook, and the client calls it only after
        its own retries are spent. "Still running" at that point means "not
        answering", so the process is ended before the respawn: SIGTERM first,
        so a driver that can still unwind releases its socket and event tap,
        then SIGKILL for one that cannot.
        """
        process = self._process
        if process is not None and process.poll() is None:
            LOGGER.warning(
                "actuation driver is alive but not answering; terminating pid %s",
                process.pid,
            )
            process.terminate()
            try:
                process.wait(timeout=TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                LOGGER.warning(
                    "actuation driver ignored SIGTERM; killing pid %s", process.pid
                )
                process.kill()
                process.wait()
        self.ensure_alive()


def supervisor_for(
    spawn: Callable[[], subprocess.Popen[bytes]],
    process: subprocess.Popen[bytes],
) -> DriverSupervisor:
    """A supervisor with the project's defaults, already owning ``process``."""
    supervisor = DriverSupervisor(
        spawn,
        max_restarts=DEFAULT_MAX_RESTARTS,
        backoff_base_seconds=RESTART_BACKOFF_BASE_SECONDS,
        sleep=time.sleep,
    )
    supervisor.adopt(process)
    return supervisor
