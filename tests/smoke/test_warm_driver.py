"""Warm driver adoption: attach to a live matching driver, spawn otherwise.

Spawning unconditionally made every CLI invocation pay process start, AppKit
init and consent checks, then kill the driver at exit. Adoption reuses a
driver already serving the socket — but only with a proven backend match:
attaching a real run to a simulated driver would click through a backend
that touches nothing.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

from computeruse.cli import adopt_warm_driver, driver_matches_mode


def test_driver_matches_mode_needs_a_known_backend() -> None:
    assert driver_matches_mode({"backend": "simulated"}, real=False) is True
    assert driver_matches_mode({"backend": "simulated"}, real=True) is False
    assert driver_matches_mode({"backend": "quartz/real"}, real=True) is True
    assert driver_matches_mode({"backend": "quartz/real"}, real=False) is False
    assert driver_matches_mode({}, real=False) is False
    assert driver_matches_mode({"backend": 42}, real=False) is False


def test_adopt_rejects_a_missing_socket() -> None:
    assert adopt_warm_driver("/tmp/cu-warm-missing.sock", real=False) is False


def _serve_once(socket_path: Path, payload: bytes) -> threading.Thread:
    """Fake driver: answer one request, then hold the socket briefly."""

    def _run() -> None:
        srv = socket.socket(socket.AF_UNIX)
        try:
            # The client refuses group/other-readable sockets; bind 0700
            # like the real spawn path's secured directory does.
            old_umask = os.umask(0o077)
            try:
                srv.bind(str(socket_path))
            finally:
                os.umask(old_umask)
            srv.listen(1)
            srv.settimeout(10)
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                return
            with conn:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                conn.sendall(payload)
        finally:
            srv.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    for _ in range(200):
        if socket_path.exists():
            break
        time.sleep(0.01)
    return thread


def _sock(name: str) -> Path:
    # Unix sockets cap the path at ~104 bytes; pytest tmp dirs nest deep.
    return Path(f"/tmp/cu-warm-{os.getpid()}-{name}.sock")


def test_adopt_accepts_a_matching_driver() -> None:
    path = _sock("match")
    _serve_once(path, b'{"ok":"health","backend":"simulated"}\n')
    try:
        assert adopt_warm_driver(str(path), real=False) is True
    finally:
        path.unlink(missing_ok=True)


def test_adopt_rejects_a_mode_mismatch() -> None:
    """A real run must never attach to a simulated driver (or vice versa):
    the clicks would land nowhere while the trace claims success."""
    path = _sock("mismatch")
    _serve_once(path, b'{"ok":"health","backend":"simulated"}\n')
    try:
        assert adopt_warm_driver(str(path), real=True) is False
    finally:
        path.unlink(missing_ok=True)


def test_adopt_rejects_garbage_and_dead_files() -> None:
    path = _sock("garbage")
    _serve_once(path, b"not json at all\n")
    try:
        assert adopt_warm_driver(str(path), real=False) is False
    finally:
        path.unlink(missing_ok=True)
    # A stale regular file where the socket should be is not a driver.
    stale = _sock("stale")
    stale.write_text("leftover")
    try:
        assert adopt_warm_driver(str(stale), real=False) is False
    finally:
        stale.unlink(missing_ok=True)
