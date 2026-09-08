from __future__ import annotations

import json
import os
import socket
import stat
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from computeruse.bridge.controller import BridgeController
from computeruse.bridge.server import (
    BridgeServer,
    BridgeServerError,
    OwnedDriver,
    StartupConfig,
    parse_startup_line,
)

CAPABILITY = "a" * 64
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


def _request(socket_path: Path) -> dict[str, object]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(3.0)
    try:
        connection.connect(str(socket_path))
        payload = {
            "version": 1,
            "method": "health",
            "params": {},
        }
        connection.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        parsed: object = json.loads(b"".join(chunks).split(b"\n", 1)[0])
        assert isinstance(parsed, dict)
        return cast(dict[str, object], parsed)
    finally:
        connection.close()


def test_parse_startup_line_accepts_exact_version_and_capability() -> None:
    startup = parse_startup_line(
        (json.dumps({"version": 1, "capability": CAPABILITY}) + "\n").encode("utf-8")
    )

    assert startup == StartupConfig(version=1, capability=CAPABILITY)


@pytest.mark.parametrize(
    "raw",
    [
        b"{}\n",
        b'{"version":2,"capability":"' + b"a" * 64 + b'"}\n',
        b'{"version":1,"capability":"short"}\n',
        b'{"version":1,"capability":"' + b"A" * 64 + b'"}\n',
        b'{"version":1,"capability":"' + b"a" * 64 + b'","extra":true}\n',
        b"not-json\n",
        b'{"version":1,"capability":"' + b"a" * 64 + b'"}',
    ],
)
def test_parse_startup_line_rejects_malformed_or_non_exact_frames(raw: bytes) -> None:
    with pytest.raises(BridgeServerError) as exc:
        parse_startup_line(raw)

    assert exc.value.code == "BRIDGE_PROTOCOL_INVALID"


def test_bind_hardens_parent_and_socket_permissions(tmp_path: Path) -> None:
    parent = tmp_path / "runtime"
    parent.mkdir(mode=0o755)
    socket_path = parent / "bridge.sock"
    server = BridgeServer(
        socket_path,
        CAPABILITY,
        RecordingController(),
        allowed_pid=os.getpid(),
    )

    server.bind()
    try:
        assert stat.S_IMODE(parent.stat().st_mode) == 0o700
        assert stat.S_ISSOCK(socket_path.lstat().st_mode)
        assert stat.S_IMODE(socket_path.lstat().st_mode) == 0o600
        assert socket_path.lstat().st_uid == os.geteuid()
    finally:
        server.close()


@pytest.mark.parametrize("kind", ["regular", "symlink"])
def test_bind_refuses_regular_file_or_symlink_at_socket_path(
    tmp_path: Path, kind: str
) -> None:
    parent = tmp_path / "runtime"
    parent.mkdir(mode=0o700)
    socket_path = parent / "bridge.sock"
    if kind == "regular":
        socket_path.write_text("do-not-delete", encoding="utf-8")
    else:
        target = parent / "target"
        target.write_text("do-not-delete", encoding="utf-8")
        socket_path.symlink_to(target)

    server = BridgeServer(
        socket_path,
        CAPABILITY,
        RecordingController(),
        allowed_pid=os.getpid(),
    )
    with pytest.raises(BridgeServerError) as exc:
        server.bind()

    assert exc.value.code == "BRIDGE_SOCKET_UNSAFE"
    assert socket_path.exists() or socket_path.is_symlink()


def test_bind_recovers_owned_stale_socket(tmp_path: Path) -> None:
    parent = tmp_path / "runtime"
    parent.mkdir(mode=0o700)
    socket_path = parent / "bridge.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()
    assert stat.S_ISSOCK(socket_path.lstat().st_mode)

    server = BridgeServer(
        socket_path,
        CAPABILITY,
        RecordingController(),
        allowed_pid=os.getpid(),
    )
    server.bind()
    try:
        assert stat.S_ISSOCK(socket_path.lstat().st_mode)
        assert stat.S_IMODE(socket_path.lstat().st_mode) == 0o600
    finally:
        server.close()


def test_bind_refuses_live_owned_socket(tmp_path: Path) -> None:
    parent = tmp_path / "runtime"
    parent.mkdir(mode=0o700)
    socket_path = parent / "bridge.sock"
    live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    live.bind(str(socket_path))
    live.listen(1)
    try:
        server = BridgeServer(
            socket_path,
            CAPABILITY,
            RecordingController(),
            allowed_pid=os.getpid(),
        )
        with pytest.raises(BridgeServerError) as exc:
            server.bind()
        assert exc.value.code == "BRIDGE_SOCKET_IN_USE"
    finally:
        live.close()
        socket_path.unlink(missing_ok=True)


def test_peer_pid_mismatch_is_constant_surface_and_never_dispatches(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "private" / "bridge.sock"
    controller = RecordingController()
    server = BridgeServer(
        socket_path,
        CAPABILITY,
        controller,
        allowed_pid=os.getpid() + 1,
    )
    server.bind()
    thread = threading.Thread(target=server.serve_once)
    thread.start()
    try:
        response = _request(socket_path)
    finally:
        thread.join(timeout=3.0)
        server.close()

    assert response == {
        "ok": False,
        "error": {"code": "POLICY_DENIED", "message": "bridge peer rejected"},
    }
    assert controller.calls == []
    assert not thread.is_alive()


def test_allowed_peer_dispatches_one_secret_free_request(tmp_path: Path) -> None:
    socket_path = tmp_path / "private" / "bridge.sock"
    controller = RecordingController()
    server = BridgeServer(
        socket_path,
        CAPABILITY,
        controller,
        allowed_pid=os.getpid(),
    )
    server.bind()
    thread = threading.Thread(target=server.serve_once)
    thread.start()
    try:
        response = _request(socket_path)
    finally:
        thread.join(timeout=3.0)
        server.close()

    assert response == {"ok": True, "result": {"method": "health"}}
    assert controller.calls == [("health", {})]
    assert not thread.is_alive()


def test_owned_driver_shutdown_order_is_fail_safe(tmp_path: Path) -> None:
    events: list[str] = []
    runtime_dir = tmp_path / "driver-runtime"
    runtime_dir.mkdir()
    driver_socket = runtime_dir / "driver.sock"
    driver_socket.touch()
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
