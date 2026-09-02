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
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DRIVER_BIN = REPO_ROOT / "driver" / "target" / "debug" / "actuation-driver"
SOCKET_PATH = REPO_ROOT / "target" / "driver-test.sock"

#: Registered in the repository-root ``conftest.py`` (pytest only honours
#: ``pytest_addoption`` in the initial conftests). ``getoption`` raises on an
#: unknown name, so a rename there fails loudly here instead of silently
#: re-enabling the skip.
ALLOW_MISSING_DRIVER = "--allow-missing-driver"

DRIVER_MISSING_MESSAGE = (
    f"the actuation driver is not built ({DRIVER_BIN} does not exist). "
    "Every smoke test drives it over a Unix socket, so without it this suite "
    "proves nothing. Run `cargo build` in driver/, or pass "
    f"{ALLOW_MISSING_DRIVER} to deliberately skip instead."
)


@pytest.fixture(scope="session", autouse=True)
def driver(request: pytest.FixtureRequest) -> Iterator[subprocess.Popen[str]]:
    """Spawn one real driver for the whole smoke-test session.

    A missing binary ends the session with a usage error rather than skipping.
    Skipping looked harmless and was not: one absent build artifact turned the
    whole suite into 331 green skips, so a run that verified nothing was
    indistinguishable from a run that verified everything.
    """
    driver_dir = REPO_ROOT / "driver"
    src_dir = driver_dir / "src"
    cargo_toml = driver_dir / "Cargo.toml"
    if src_dir.exists():
        needs_build = not DRIVER_BIN.exists()
        if not needs_build:
            bin_mtime = DRIVER_BIN.stat().st_mtime
            if (cargo_toml.exists() and cargo_toml.stat().st_mtime > bin_mtime) or any(
                p.stat().st_mtime > bin_mtime for p in src_dir.rglob("*.rs")
            ):
                needs_build = True
        if needs_build:
            try:
                subprocess.run(
                    ["cargo", "build", "--manifest-path", str(cargo_toml)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (subprocess.SubprocessError, FileNotFoundError):
                pass

    if not DRIVER_BIN.exists():
        if request.config.getoption(ALLOW_MISSING_DRIVER):
            pytest.skip(DRIVER_MISSING_MESSAGE)
        pytest.exit(reason=DRIVER_MISSING_MESSAGE, returncode=pytest.ExitCode.USAGE_ERROR)
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
            if proc.poll() is not None:
                pytest.fail(f"driver exited prematurely with code {proc.returncode}")
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
