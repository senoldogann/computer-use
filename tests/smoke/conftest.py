"""Shared fixtures for smoke tests.

The Rust actuation driver is a *separate process* (ADR-1) with no Python
import path, so tests must spawn it explicitly and talk over its Unix socket.
One module-scoped fixture starts a single driver for all smoke tests; a raw
JSON-RPC helper keeps the low-level byte path testable independent of the
higher-level :class:`ActuationClient` assertions.
"""

from __future__ import annotations

import json
import socket as socket_mod
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DRIVER_BIN = REPO_ROOT / "driver" / "target" / "debug" / "actuation-driver"
SOCKET_PATH = REPO_ROOT / "target" / "driver-test.sock"


@pytest.fixture(scope="session", autouse=True)
def driver() -> subprocess.Popen[str]:
    """Spawn one real driver for the whole smoke-test session (or skip)."""
    if not DRIVER_BIN.exists():
        pytest.skip("actuation-driver not built; run `cargo build` in driver/")
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    proc = subprocess.Popen(
        [str(DRIVER_BIN), str(SOCKET_PATH)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Wait for the socket to appear so tests never race the bind.
        for _ in range(50):
            if SOCKET_PATH.exists():
                break
            time.sleep(0.05)
        else:
            pytest.fail("driver did not create its socket in time")
        yield proc
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def rpc_call(payload: dict[str, object]) -> dict[str, object]:
    """Send one newline-delimited JSON request, read one response line (raw)."""
    connection = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    connection.settimeout(5.0)
    try:
        connection.connect(str(SOCKET_PATH))
        connection.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(4096)
            if not chunk or b"\n" in chunk:
                if chunk:
                    tail = chunk.split(b"\n", 1)[0]
                    if tail:
                        chunks.append(tail)
                break
            chunks.append(chunk)
        response = json.loads(b"".join(chunks))
        assert isinstance(response, dict)
        return response
    finally:
        connection.close()


def rpc_call_raw(wire_line: bytes) -> dict[str, object]:
    """Send an already-serialized line verbatim; returns the parsed response."""
    connection = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    connection.settimeout(5.0)
    try:
        connection.connect(str(SOCKET_PATH))
        connection.sendall(wire_line if wire_line.endswith(b"\n") else wire_line + b"\n")
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunks[-1]:
                break
        body = b"".join(chunks).split(b"\n", 1)[0]
        response = json.loads(body.decode("utf-8"))
        assert isinstance(response, dict)
        return response
    finally:
        connection.close()


# The simulated backend serves a fixed fixture frame: it can never "settle",
# so the post-action render wait is pure dead time in every smoke test that
# drives it through the product shell. Production keeps the real budget.
SIMULATED_SETTLE: dict[str, int | float] = {
    "settle_max_polls": 0,
    "settle_interval_s": 0.0,
}
