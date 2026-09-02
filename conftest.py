"""Session-level gates that keep a green test run honest.

Both gates exist because a suite can report success while proving nothing, and
that is strictly worse than a red build: the failure is invisible.

* **A missing driver is a hard stop.** The smoke suite drives the *real* Rust
  actuation binary over a Unix socket (ADR-1), so an unbuilt ``driver/`` used
  to make one autouse fixture skip every single test — 331 skips, exit code 0,
  a green CI badge over zero coverage. The fixture now stops the session with a
  usage error instead, and ``--allow-missing-driver`` is the explicit,
  deliberate opt-out for someone who really does want the driverless subset.

* **A mostly-skipped CI run fails.** The gate above closes the one hole we
  know about; this one closes the class. Any future fixture that starts
  skipping wholesale (a missing consent, an absent optional dependency) turns
  CI red rather than quietly shrinking what the badge covers.

This file lives at the repository root deliberately: ``pytest_addoption`` is
only honoured in the *initial* conftests, which for a bare ``pytest`` invocation
means the rootdir's alone.
"""

from __future__ import annotations

import os
from typing import Final

import pytest

#: Opt-out flag for the missing-driver hard stop. Read by
#: ``tests/smoke/conftest.py``; ``getoption`` raises if the two ever drift.
ALLOW_MISSING_DRIVER: Final[str] = "--allow-missing-driver"

#: Fraction of collected tests that may be skipped before a CI run is failed.
#: A handful of platform-gated skips is normal; a suite that skips a tenth of
#: itself is no longer measuring what the badge claims.
MAX_SKIP_RATIO: Final[float] = 0.10


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        ALLOW_MISSING_DRIVER,
        action="store_true",
        default=False,
        help=(
            "Skip (instead of failing) the smoke suite when the Rust actuation "
            "driver has not been built. Without this flag an unbuilt driver is a "
            "usage error, so a driverless run can never look green."
        ),
    )


def running_in_ci() -> bool:
    """Whether this process is a CI run (pure, environment-derived)."""
    return os.environ.get("CI", "").strip().lower() not in ("", "0", "false", "no")


def skip_ratio_verdict(skipped: int, collected: int, max_ratio: float) -> str | None:
    """The failure reason when too much of the suite was skipped (pure).

    ``None`` means the run is within budget. Kept pure so the threshold policy
    is testable without a CI environment or a second pytest session.
    """
    if collected <= 0 or skipped <= 0:
        return None
    ratio = skipped / collected
    if ratio <= max_ratio:
        return None
    return (
        f"{skipped} of {collected} collected tests were skipped "
        f"({ratio:.0%} > {max_ratio:.0%} allowed in CI): a green result here "
        "covers almost nothing. Build the actuation driver (cargo build in "
        "driver/) or fix whatever is skipping."
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Turn a mostly-skipped CI run into a failure (never a green badge)."""
    if exitstatus != 0 or not running_in_ci():
        return
    reporter = session.config.pluginmanager.getplugin("terminalreporter")
    if reporter is None:
        return
    stats: dict[str, list[object]] = getattr(reporter, "stats", {})
    reason = skip_ratio_verdict(
        len(stats.get("skipped", [])), session.testscollected, MAX_SKIP_RATIO
    )
    if reason is None:
        return
    reporter.write_line(f"ERROR: {reason}", red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
