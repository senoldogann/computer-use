"""The suite's own honesty gates (see the repository-root ``conftest.py``).

A test suite that skips itself into a green result is the one failure a test
suite cannot report, so these gates are exercised the only way that proves
anything: by running pytest in a scratch tree where the actuation driver is
genuinely absent, and asserting on the *process exit code*.

The scratch tree mirrors the real layout (root ``conftest.py`` plus
``tests/smoke/conftest.py``) because the smoke conftest derives the driver path
from its own location — copied under a temporary root, it looks for a binary
that cannot exist.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from tests.smoke.conftest import REPO_ROOT

_PROBE_TEST = '''
def test_probe() -> None:
    assert True
'''


def _scratch_suite(root: Path) -> None:
    """Recreate the real conftest layout under ``root`` with no driver built."""
    smoke = root / "tests" / "smoke"
    smoke.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "conftest.py", root / "conftest.py")
    shutil.copy(REPO_ROOT / "tests" / "smoke" / "conftest.py", smoke / "conftest.py")
    (root / "tests" / "__init__.py").write_text("")
    (smoke / "__init__.py").write_text("")
    (smoke / "test_probe.py").write_text(_PROBE_TEST)


def _run_pytest(root: Path, *args: str, ci: str | None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(root)}
    if ci is not None:
        env["CI"] = ci
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_missing_driver_is_a_usage_error_not_a_skip(tmp_path: Path) -> None:
    """Without the driver the session stops hard — it never reports green."""
    _scratch_suite(tmp_path)
    result = _run_pytest(tmp_path, ci=None)
    assert result.returncode != 0, "a driverless run must never exit 0"
    output = result.stdout + result.stderr
    assert "actuation driver is not built" in output, output[-2000:]
    assert "--allow-missing-driver" in output, output[-2000:]


def test_allow_missing_driver_opts_back_into_skipping(tmp_path: Path) -> None:
    """The escape hatch is explicit: ask for skips and you get skips."""
    _scratch_suite(tmp_path)
    result = _run_pytest(tmp_path, "--allow-missing-driver", ci=None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "skipped" in result.stdout, result.stdout


def test_ci_fails_a_run_that_skipped_almost_everything(tmp_path: Path) -> None:
    """Even behind the opt-out, CI refuses to call a skipped suite green."""
    _scratch_suite(tmp_path)
    result = _run_pytest(tmp_path, "--allow-missing-driver", ci="true")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "covers almost nothing" in result.stdout, result.stdout
