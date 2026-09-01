"""Driver-spawn diagnostics: the *reason* for a driver startup failure.

``spawn_driver`` must never hide the driver's own diagnosis. When the real
driver refuses to start (e.g. missing Accessibility consent) it prints an
actionable line on stderr and exits 1; a bare "code 1" in the panel told the
user *that* it failed, never *why*. These tests pin the stderr-surfacing
behaviour with fake driver scripts (Law 6.3 explicit error propagation).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from computeruse.cli import spawn_driver

# The fake drivers are executable scripts (shebang) so the command
# ``[binary, socket_path]`` reaches them exactly like the real driver binary:
# the socket path lands in argv[1], not in python's script position.
_DRIVER_HEADER = f"#!{sys.executable}\n"


def _write_driver(tmp_path: object, name: str, body: str) -> str:
    script = tmp_path / name  # type: ignore[attr-defined]
    script.write_text(_DRIVER_HEADER + body)
    script.chmod(0o755)
    return str(script)


def test_spawn_driver_surfaces_stderr_on_startup_failure(tmp_path) -> None:
    """A driver that exits 1 must carry its stderr diagnosis in the error."""
    driver = _write_driver(
        tmp_path,
        "fail_driver.py",
        "import sys\n"
        "print('[driver] fatal: Accessibility consent required. Grant it in '\n"
        "      'System Settings > Privacy & Security > Accessibility', file=sys.stderr)\n"
        "sys.exit(1)\n",
    )
    with pytest.raises(RuntimeError) as exc:
        spawn_driver(driver, str(tmp_path / "gone.sock"), real=False)  # type: ignore[attr-defined]
    message = str(exc.value)
    assert "code 1" in message
    assert "Accessibility consent required" in message, (
        f"the driver's own diagnosis must reach the caller: {message}"
    )


def test_spawn_driver_returns_process_once_socket_exists(tmp_path) -> None:
    """A healthy driver that binds its socket is adopted, not misreported."""
    # Unix sockets cap the path at ~104 bytes; pytest's tmp dirs live under
    # /private/var/folders/… which is already too long, so use a short /tmp
    # path keyed by pid to stay parallel-safe.
    socket_path = Path(f"/tmp/cu-spawn-{os.getpid()}.sock")
    driver = _write_driver(
        tmp_path,
        "ok_driver.py",
        "import socket, sys, time\n"
        "s = socket.socket(socket.AF_UNIX)\n"
        "s.bind(sys.argv[1])\n"
        "print('[driver] listening', file=sys.stderr)\n"
        "time.sleep(30)\n",
    )
    process = spawn_driver(driver, str(socket_path), real=False)
    try:
        assert process.poll() is None, "spawned driver must still be alive"
        assert socket_path.exists()
    finally:
        process.terminate()
        process.wait(timeout=5)
        socket_path.unlink(missing_ok=True)
