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