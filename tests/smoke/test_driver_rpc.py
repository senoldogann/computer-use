"""End-to-end smoke test: drive the real Rust driver over its Unix socket.

Validates ADR-1's contract — the orchestrator reaches the physical layer only
through a separate-process driver speaking JSON-RPC; ping/move round-trip and
malformed input is rejected by the driver, not by the client.
"""

from __future__ import annotations

from tests.smoke.conftest import rpc_call


def test_ping_round_trip() -> None:
    payload = rpc_call({"method": "ping"})
    assert payload.get("ok") == "pong"


def test_health_round_trip() -> None:
    payload = rpc_call({"method": "health"})
    assert payload.get("ok") == "health"
    assert payload.get("backend") == "simulated"
    assert payload.get("trusted") is True
    # Consent and the kill-hotkey tap are separate answers on the wire. They
    # were briefly ANDed into ``trusted``, which made a dead event tap
    # indistinguishable from a missing Accessibility grant — two problems with
    # nothing in common and different fixes. The simulated backend installs no
    # tap at all (Law 1), so it reports null: "does not apply", not "broken".
    assert "kill_listener_armed" in payload
    assert payload.get("kill_listener_armed") is None


def test_client_health() -> None:
    from computeruse.orchestrator.client import ActuationClient
    from tests.smoke.conftest import SOCKET_PATH

    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        health = client.health()
        assert health.get("ok") == "health"
        assert health.get("backend") == "simulated"


def test_client_refuses_symlink_socket(tmp_path) -> None:
    """Fail-closed: never connect through a symlink (pre-bind swap race)."""
    import os

    from computeruse.orchestrator.client import ActuationClient, DriverConnectionError
    from tests.smoke.conftest import SOCKET_PATH

    link = tmp_path / "evil.sock"
    os.symlink(str(SOCKET_PATH), link)
    client = ActuationClient(str(link), connect_retries=1)
    # _connect_once raises the specific OSError; connect() surfaces it
    # as DriverConnectionError after retries (reason stays in the log).
    try:
        client._connect_once()
    except OSError as exc:
        assert "symlink" in str(exc)
    else:
        raise AssertionError("symlink socket was not refused")
    try:
        client.health()
    except DriverConnectionError:
        pass
    else:
        raise AssertionError("symlink socket was not refused")


def test_driver_rejects_wrong_peer_pid(tmp_path) -> None:
    """A driver started with --allow-pid 1 rejects our connection."""
    import os
    import socket as socket_mod
    import subprocess
    import time

    # pytest tmp paths exceed SUN_LEN (~104 chars); use a short /tmp name.
    from pathlib import Path

    from tests.smoke.conftest import DRIVER_BIN

    sock = Path(f"/tmp/pid-gated-{os.getpid()}-{time.time_ns() % 1_000_000}.sock")
    try:
        os.unlink(sock)
    except OSError:
        pass
    proc = subprocess.Popen(
        [str(DRIVER_BIN), str(sock), "--allow-pid", "1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(50):
            if sock.exists():
                break
            if proc.poll() is not None:
                _, errs = proc.communicate(timeout=5)
                raise AssertionError(f"gated driver exited early: {errs[-2000:]}")
            time.sleep(0.05)
        else:
            errs = ""
            raise AssertionError(
                f"gated driver did not bind in time (poll={proc.poll()}): {errs[-2000:]}"
            )
        conn = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        conn.settimeout(5.0)
        try:
            conn.connect(str(sock))
            conn.sendall(b'{"method":"ping"}\n')
            try:
                data = conn.recv(4096)
            except (TimeoutError, OSError):
                data = b""
            assert data == b"", "wrong-PID connection was not rejected"
        finally:
            conn.close()
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        try:
            os.unlink(sock)
        except OSError:
            pass


def test_mouse_move_ack() -> None:
    payload = rpc_call(
        {"method": "mouse_move", "params": {"x": 640, "y": 480, "duration_ms": 120}}
    )
    assert payload.get("ok") == "ack"


def test_activate_app_ack() -> None:
    """App activation round-trips over the wire (simulated backend ACKs)."""
    payload = rpc_call({"method": "activate_app", "params": {"app": "Safari"}})
    assert payload.get("ok") == "ack"


def test_list_apps_returns_running_apps() -> None:
    """The running-app list round-trips (autonomous target-app inference)."""
    payload = rpc_call({"method": "list_apps"})
    assert payload.get("ok") == "list_apps"
    apps = payload.get("apps")
    assert isinstance(apps, list)
    assert "Safari" in apps, "simulated fixture must report its running apps"


def test_malformed_method_rejected() -> None:
    payload = rpc_call({"method": "teleport", "params": {}})
    assert payload.get("ok") == "error"