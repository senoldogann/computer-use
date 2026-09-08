"""Inherited-stream bridge server and owned Rust-driver lifecycle.

The bridge is a deterministic adapter, not an agent. Its parent owns the
stdin/stdout pipes, so requests never cross a replaceable named bridge socket
and no authentication secret is carried in request frames.

The Rust actuation driver remains a child owned by this bridge instance and
is started with ``--allow-pid <bridge-pid>``. Shutdown never scans for arbitrary
processes: it releases inputs, closes the client, terminates only that child,
and removes only artifacts inside the bridge-owned private runtime directory.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from computeruse.bridge.controller import BridgeController, BridgeHostError
from computeruse.bridge.protocol import (
    MAX_REQUEST_BYTES,
    BridgeProtocolError,
    encode_error,
    encode_success,
    parse_request_line,
)
from computeruse.orchestrator.client import ActuationClient

MAX_RESPONSE_BYTES = 64 * 1024 * 1024
DRIVER_START_TIMEOUT_SECONDS = 10.0
DRIVER_STOP_TIMEOUT_SECONDS = 5.0


class BridgeServerError(RuntimeError):
    """Stable bridge startup/stream/lifecycle failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DispatchController(Protocol):
    """Structural controller contract used by the inherited stream server."""

    def dispatch(self, method: str, params: dict[str, object]) -> object: ...


class BridgeStdioServer:
    """Serve bounded secret-free requests over parent-owned stdin/stdout pipes."""

    def __init__(
        self,
        reader: BinaryIO,
        writer: BinaryIO,
        controller: DispatchController,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._controller = controller

    def serve_once(self) -> bool:
        """Process one frame; return False at EOF or after an oversized partial frame."""
        raw = self._reader.readline(MAX_REQUEST_BYTES + 1)
        if raw == b"":
            return False
        if len(raw) > MAX_REQUEST_BYTES:
            self._write_response(
                encode_error("BRIDGE_PROTOCOL_INVALID", "bridge request exceeds the frame limit")
            )
            return False

        try:
            request = parse_request_line(raw)
            result = self._controller.dispatch(request.method, request.params)
            response = encode_success(result)
        except BridgeProtocolError as exc:
            response = encode_error(exc.code, str(exc))
        except BridgeHostError as exc:
            response = encode_error(exc.code, str(exc))
        except BridgeServerError as exc:
            response = encode_error(exc.code, str(exc))
        except (OSError, RuntimeError, TypeError, ValueError):
            response = encode_error("DRIVER_UNAVAILABLE", "computer-use bridge request failed")

        if len(response) > MAX_RESPONSE_BYTES:
            response = encode_error(
                "DRIVER_UNAVAILABLE", "computer-use bridge response exceeded limit"
            )
        self._write_response(response)
        return True

    def serve_forever(self) -> None:
        """Serve sequentially until the owning parent closes stdin."""
        while self.serve_once():
            pass

    def _write_response(self, response: bytes) -> None:
        try:
            self._writer.write(response)
            self._writer.flush()
        except OSError as exc:
            raise BridgeServerError("BRIDGE_UNAVAILABLE", "bridge output stream is unavailable") from exc


def _secure_owned_directory(path: Path) -> None:
    """Create/harden one private runtime directory, refusing aliases and owners."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        entry = path.lstat()
    except OSError as exc:
        raise BridgeServerError("BRIDGE_SOCKET_UNSAFE", "runtime directory is unavailable") from exc
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
        raise BridgeServerError("BRIDGE_SOCKET_UNSAFE", "runtime path is not a real directory")
    if entry.st_uid != os.geteuid():
        raise BridgeServerError("BRIDGE_SOCKET_UNSAFE", "runtime directory owner is unsafe")
    if stat.S_IMODE(entry.st_mode) != 0o700:
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            raise BridgeServerError(
                "BRIDGE_SOCKET_UNSAFE", "runtime directory permissions are unsafe"
            ) from exc


def _cleanup_owned_runtime(runtime_dir: Path, driver_socket: Path) -> None:
    """Remove only the expected owned child socket and then the empty directory."""
    try:
        runtime_entry = runtime_dir.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISLNK(runtime_entry.st_mode)
        or not stat.S_ISDIR(runtime_entry.st_mode)
        or runtime_entry.st_uid != os.geteuid()
    ):
        return
    try:
        socket_entry = driver_socket.lstat()
    except FileNotFoundError:
        socket_entry = None
    if (
        socket_entry is not None
        and stat.S_ISSOCK(socket_entry.st_mode)
        and socket_entry.st_uid == os.geteuid()
    ):
        driver_socket.unlink(missing_ok=True)
    with suppress(OSError):
        runtime_dir.rmdir()


@dataclass
class OwnedDriver:
    """The one Rust driver child owned by one bridge process."""

    client: ActuationClient
    process: subprocess.Popen[bytes]
    controller: BridgeController
    driver_socket: Path
    runtime_dir: Path

    @classmethod
    def start(
        cls,
        *,
        driver_path: Path,
        runtime_parent: Path,
        real: bool,
    ) -> OwnedDriver:
        if not driver_path.is_absolute():
            raise BridgeServerError("DRIVER_UNAVAILABLE", "driver path must be absolute")
        if not runtime_parent.is_absolute():
            raise BridgeServerError("BRIDGE_SOCKET_UNSAFE", "runtime path must be absolute")
        try:
            driver_entry = driver_path.stat()
        except OSError as exc:
            raise BridgeServerError("DRIVER_UNAVAILABLE", "actuation driver is unavailable") from exc
        if not stat.S_ISREG(driver_entry.st_mode) or not os.access(driver_path, os.X_OK):
            raise BridgeServerError("DRIVER_UNAVAILABLE", "actuation driver is not executable")

        _secure_owned_directory(runtime_parent)
        runtime_dir = Path(tempfile.mkdtemp(prefix="driver-", dir=runtime_parent))
        os.chmod(runtime_dir, 0o700)
        driver_socket = runtime_dir / "driver.sock"
        command = [str(driver_path), str(driver_socket), "--allow-pid", str(os.getpid())]
        if real:
            command.append("--real")

        process: subprocess.Popen[bytes] | None = None
        client: ActuationClient | None = None
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
            deadline = time.monotonic() + DRIVER_START_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise BridgeServerError(
                        "DRIVER_UNAVAILABLE", "actuation driver exited during startup"
                    )
                if driver_socket.exists():
                    break
                time.sleep(0.05)
            else:
                raise BridgeServerError(
                    "DRIVER_UNAVAILABLE", "actuation driver did not create its socket"
                )

            client = ActuationClient(str(driver_socket), connect_retries=3)
            client.connect()
            health = client.health()
            if health.get("trusted") is not True:
                raise BridgeServerError("DRIVER_UNTRUSTED", "actuation driver is not trusted")
            if real and health.get("kill_listener_armed") is not True:
                raise BridgeServerError(
                    "DRIVER_UNTRUSTED", "emergency takeover listener is not armed"
                )
            controller = BridgeController(client)
            return cls(
                client=client,
                process=process,
                controller=controller,
                driver_socket=driver_socket,
                runtime_dir=runtime_dir,
            )
        except BaseException:
            if client is not None:
                with suppress(OSError, RuntimeError):
                    client.release_inputs()
                client.close()
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=DRIVER_STOP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=DRIVER_STOP_TIMEOUT_SECONDS)
            _cleanup_owned_runtime(runtime_dir, driver_socket)
            raise

    def close(self) -> None:
        """Fail-safe shutdown: release -> close client -> stop owned child -> cleanup."""
        with suppress(OSError, RuntimeError):
            self.client.release_inputs()
        self.client.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=DRIVER_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=DRIVER_STOP_TIMEOUT_SECONDS)
        _cleanup_owned_runtime(self.runtime_dir, self.driver_socket)


def install_termination_handler() -> None:
    """Convert SIGTERM into normal unwinding so owned resources reach finally."""

    def _terminate(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
