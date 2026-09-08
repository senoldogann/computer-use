from __future__ import annotations

import json
import socket
import stat
from io import BytesIO
from pathlib import Path
from typing import Any, cast

from computeruse.bridge.controller import BridgeController
from computeruse.bridge.protocol import MAX_REQUEST_BYTES
from computeruse.bridge.server import BridgeStdioServer, OwnedDriver

REPO_ROOT = Path(__file__).resolve().parents[2]
DRIVER_BIN = REPO_ROOT / "driver" / "target" / "debug" / "actuation-driver"


class RecordingController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def dispatch(self, method: str, params: dict[str, object]) -> object:
        self.calls.append((method, params))
        return {"method": method}


class RecordingClient:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def release_inputs(self) -> None:
        self.events.append("release_inputs")

    def close(self) -> None:
        self.events.append("client_close")


class RecordingProcess:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.terminated = False
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.events.append("terminate")
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.events.append("wait")
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.events.append("kill")
        self.returncode = -9


def _frame(method: str, params: dict[str, object] | None = None) -> bytes:
    return (
        json.dumps({"version": 1, "method": method, "params": params or {}}) + "\n"
    ).encode()


def _responses(writer: BytesIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in writer.getvalue().splitlines()]


def test_stdio_bridge_dispatches_secret_free_request() -> None:
    reader = BytesIO(_frame("health"))
    writer = BytesIO()
    controller = RecordingController()
    server = BridgeStdioServer(reader, writer, controller)

    assert server.serve_once() is True

    assert _responses(writer) == [{"ok": True, "result": {"method": "health"}}]
    assert controller.calls == [("health", {})]


def test_stdio_bridge_rejects_capability_frame_without_dispatch() -> None:
    reader = BytesIO(
        (
            json.dumps(
                {
                    "version": 1,
                    "capability": "a" * 64,
                    "method": "health",
                    "params": {},
                }
            )
            + "\n"
        ).encode()
    )
    writer = BytesIO()
    controller = RecordingController()
    server = BridgeStdioServer(reader, writer, controller)

    assert server.serve_once() is True

    assert _responses(writer) == [
        {
            "ok": False,
            "error": {
                "code": "BRIDGE_PROTOCOL_INVALID",
                "message": "bridge request fields are invalid",
            },
        }
    ]
    assert controller.calls == []


def test_stdio_bridge_handles_multiple_frames_in_order_until_eof() -> None:
    reader = BytesIO(_frame("health") + _frame("active_window"))
    writer = BytesIO()
    controller = RecordingController()
    server = BridgeStdioServer(reader, writer, controller)

    server.serve_forever()

    assert _responses(writer) == [
        {"ok": True, "result": {"method": "health"}},
        {"ok": True, "result": {"method": "active_window"}},
    ]
    assert controller.calls == [("health", {}), ("active_window", {})]


def test_stdio_bridge_stops_cleanly_at_eof() -> None:
    writer = BytesIO()
    controller = RecordingController()
    server = BridgeStdioServer(BytesIO(), writer, controller)

    assert server.serve_once() is False
    assert writer.getvalue() == b""
    assert controller.calls == []


def test_stdio_bridge_terminates_stream_after_oversized_partial_frame() -> None:
    writer = BytesIO()
    controller = RecordingController()
    server = BridgeStdioServer(BytesIO(b"x" * (MAX_REQUEST_BYTES + 1)), writer, controller)

    assert server.serve_once() is False
    assert _responses(writer) == [
        {
            "ok": False,
            "error": {
                "code": "BRIDGE_PROTOCOL_INVALID",
                "message": "bridge request exceeds the frame limit",
            },
        }
    ]
    assert controller.calls == []


def test_owned_driver_shutdown_order_is_fail_safe(tmp_path: Path) -> None:
    events: list[str] = []
    runtime_dir = tmp_path / "driver-runtime"
    runtime_dir.mkdir()
    driver_socket = runtime_dir / "driver.sock"
    bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound.bind(str(driver_socket))
    bound.close()
    client = RecordingClient(events)
    process = RecordingProcess(events)
    owned = OwnedDriver(
        client=cast(Any, client),
        process=cast(Any, process),
        controller=BridgeController(cast(Any, client)),
        driver_socket=driver_socket,
        runtime_dir=runtime_dir,
    )

    owned.close()

    assert events == ["release_inputs", "client_close", "terminate", "wait"]
    assert not driver_socket.exists()
    assert not runtime_dir.exists()


def test_owned_driver_starts_and_stops_simulated_rust_driver(tmp_path: Path) -> None:
    owned = OwnedDriver.start(
        driver_path=DRIVER_BIN,
        runtime_parent=tmp_path / "private",
        real=False,
    )
    process = owned.process
    runtime_dir = owned.runtime_dir
    driver_socket = owned.driver_socket
    try:
        health = owned.controller.dispatch("health", {})
        assert health == {
            "backend": "simulated",
            "trusted": True,
            "kill_listener_armed": None,
        }
        assert process.poll() is None
        assert stat.S_IMODE(runtime_dir.stat().st_mode) == 0o700
        assert stat.S_ISSOCK(driver_socket.lstat().st_mode)
    finally:
        owned.close()

    assert process.poll() is not None
    assert not driver_socket.exists()
    assert not runtime_dir.exists()
