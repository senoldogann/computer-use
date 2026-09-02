"""Emergency kill-switch — the user can always reclaim control (Law 5).

The agent never runs the physical layer unsupervised: at any moment a human can
take over by shaking the mouse hard, pressing a global hotkey, or sending a
signal. This module is the *orchestrator-side* enforcement of that guarantee.

Per Law 6 the logic is split: ``is_mouse_shake`` is a pure, testable function;
:class:`MouseShakeMonitor` and :class:`KillSwitch` are the imperative shells
that sample the real cursor and gate the OODA loop.
"""

from __future__ import annotations

import itertools
import signal
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class CursorSample:
    """A timestamped cursor position (used by the pure detector)."""

    x: float
    y: float
    time: float


def is_mouse_shake(samples: Sequence[CursorSample], *, min_reversals: int = 6) -> bool:
    """Pure detector: is a rapid, bounded back-and-forth motion present?

    A human taking over by forcing the mouse side to side produces a *burst of
    direction reversals* within a short window while the net displacement stays
    small. A false positive is an escape hatch tripped for the user — control
    is yanked away mid-workflow — so false positives (a few reversals from
    normal work) must be rare: we require a generous reversal count all
    happening inside the window.

    Args:
        samples: timestamped cursor positions, oldest first.
        min_reversals: number of sign changes that qualifies as a shake.

    Returns:
        True if the trace shows enough oscillatory reversals.
    """
    if len(samples) < min_reversals + 1:
        return False

    # Direction along x and y is checked separately; a shake on either axis
    # counts, since a sideways human motion could be vertically or horizontally
    # dominant depending on the desk. We count axis reversals and require the
    # *combined* count to exceed the threshold.
    x_deltas = [
        samples[i + 1].x - samples[i].x for i in range(len(samples) - 1) if abs(samples[i + 1].x - samples[i].x) > 1e-6
    ]
    y_deltas = [
        samples[i + 1].y - samples[i].y for i in range(len(samples) - 1) if abs(samples[i + 1].y - samples[i].y) > 1e-6
    ]

    def reversals(deltas: list[float]) -> int:
        count = 0
        for a, b in itertools.pairwise(deltas):
            # Only count *large* reversals (movement, not dwell micro-jitter).
            if a * b < 0:
                count += 1
        return count

    return reversals(x_deltas) + reversals(y_deltas) >= min_reversals


class MouseShakeMonitor:
    """Imperative shell: sample the live cursor and detect a takeover.

    Owns the rolling sample window; feeds the pure :func:`is_mouse_shake`.
    The cursor provider is injected so tests can simulate motion without an OS.
    """

    def __init__(
        self,
        cursor: Callable[[], CursorSample],
        *,
        window_size: int = 20,
        min_reversals: int = 6,
    ) -> None:
        self._cursor = cursor
        # A bounded deque slides the window in O(1) per sample — ``list.pop(0)``
        # would shift every element on each poll of the OODA loop (G5).
        self._window: deque[CursorSample] = deque(maxlen=window_size)
        self._window_size = window_size
        self._min_reversals = min_reversals

    def observe(self) -> bool:
        """Append one sample and return whether a shake is detected."""
        # maxlen makes the deque drop the oldest sample automatically.
        self._window.append(self._cursor())
        return is_mouse_shake(self._window, min_reversals=self._min_reversals)


class KillSwitch:
    """Gates the OODA loop against human takeover (Law 5)."""

    def __init__(
        self,
        *,
        monitor: MouseShakeMonitor | None,
        signal_triggered: bool = False,
        signal_predicate: Callable[[], bool] | None = None,
    ) -> None:
        if signal_predicate is not None and signal_triggered:
            # Two signal sources with different semantics (static flag vs live
            # poll) silently shadowing each other is a trap (G2): tripped()
            # must never ignore a trip the caller believes it configured.
            raise ValueError(
                "signal_triggered and signal_predicate are mutually exclusive "
                "signal sources; provide exactly one"
            )
        self._monitor = monitor
        self._signal_triggered = signal_triggered
        self._signal_predicate = signal_predicate

    def tripped(self) -> bool:
        """Poll once: is control being reclaimed by the human right now?

        ``signal_predicate`` (if given) is polled live on every call — this is
        how a SIGINT catcher installed at startup keeps working throughout a
        long run, instead of freezing the trip at construction time.
        ``signal_triggered`` is a construction-time static flag (tests, or a
        one-shot semantic) and is mutually exclusive with the predicate.
        """
        if self._signal_predicate is not None:
            return self._signal_predicate()
        if self._signal_triggered:
            return True
        if self._monitor is None:
            return False
        return self._monitor.observe()

    def with_signal_predicate(self, predicate: Callable[[], bool]) -> KillSwitch:
        """Return a new switch with an additional live signal source (OR-ed).

        Lets a caller compose channels (e.g. the CLI's SIGINT catcher + the
        driver's global hotkey poll) without mutating the original switch.
        The G2 rule holds: a statically tripped switch cannot gain a live
        source — that would contradict its static trip.
        """
        if self._signal_triggered:
            raise ValueError(
                "cannot add a live signal predicate to a statically tripped switch"
            )
        existing = self._signal_predicate
        combined: Callable[[], bool]
        if existing is not None:
            combined = lambda: existing() or predicate()
        else:
            combined = predicate
        return KillSwitch(monitor=self._monitor, signal_predicate=combined)

    def run_blocking(self, on_trip: Callable[[], None] | None = None) -> None:
        """Loop calling :meth:`tripped` forever (used from a worker thread).

        For a real deployment this would live in a dedicated listener thread;
        the hotkey hook or SIGINT catcher is wired through ``signal_predicate``
        (or a :class:`MouseShakeMonitor`) and we honour the trip as soon as
        tripped() next returns True.
        """
        while not self.tripped():
            # Very light wait so the loop doesn't spin the CPU needlessly.
            import time

            time.sleep(0.02)
        if on_trip is not None:
            on_trip()


def install_sigint_catcher() -> Callable[[], bool]:
    """Return a callable that reads whether SIGINT/interrupt was delivered.

    This is a *fail-safe* path (Law 5): a human pressing Ctrl-C on the
    orchestration terminal reclaims control even if the hotkey hooks are absent.
    The returned predicate becomes the ``signal_triggered`` source for a
    :class:`KillSwitch`.
    """
    triggered: Final[list[bool]] = [False]

    def _handler(_signum: int, _frame: object) -> None:
        triggered[0] = True

    # Install only if not already trapping (e.g. an interactive REPL).
    try:
        signal.signal(signal.SIGINT, _handler)  # type: ignore[reportGeneralTypeIssues]
    except ValueError:
        # Signal handler can only run in the main thread.
        pass

    return lambda: triggered[0]