"""Authenticated local Unix-socket server and owned Rust-driver lifecycle.

The bridge is a deterministic adapter, not an agent. It accepts one bounded
request per Unix-socket connection, authenticates the in-memory startup
capability with a constant-time comparison, and dispatches only the controller's
typed computer-use vocabulary.

The Rust actuation driver is always a child owned by this bridge instance and
is started with ``--allow-pid <bridge-pid>``. Shutdown never scans for arbitrary
processes: it releases inputs, closes the client, terminates only that child,
and removes only artifacts inside the bridge-owned private runtime directory.
"""

from __future__ import annotations

import errno
import hmac
import json
import os
import signal
import socket
import stat
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from computeruse.bridge.controller import BridgeController, BridgeHostError
from computeruse.bridge.protocol import (
    CAPABILITY_PATTERN,
    MAX_REQUEST_BYTES,
    PROTOCOL_VERSION,
    BridgeProtocolError,
    encode_error,
    encode_success,
    parse_request_line,
)
from computeruse.orchestrator.client import ActuationClient

MAX_STARTUP_BYTES = 1_024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
DRIVER_START_TIMEOUT_SECONDS = 10.0
DRIVER_STOP_TIMEOUT_SECONDS = 5.0
SOCKET_PROBE_TIMEOUT_SECONDS = 0.2


class BridgeServerError(RuntimeError):
    """Stable bridge startup/socket/lifecycle failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StartupConfig:
    """The one secret-bearing frame accepted on bridge stdin."""

    version: int
    capability: str


class DispatchController(Protocol):
    """Structural controller contract used by the socket server."""

    def dispatch(self, method: str, params: dict[str, object]) -> object: ...


def parse_startup_line(raw: bytes) -> StartupConfig:
    """Validate the exact one-line startup contract without retaining raw input."""
    if not raw or len(raw) > MAX_STARTUP_BYTES or not raw.endswith(b"\n"):
        raise BridgeServerError("BRIDGE_PROTOCOL_INVALID", "invalid bridge startup frame")
    if raw.count(b"\n") != 1:
        raise BridgeServerError("BRIDGE_PROTOCOL_INVALID", "invalid bridge startup frame")
    try:
        decoded = raw[:-1].decode("utf-8")
        payload_object: object = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeServerError(
            "BRIDGE_PROTOCOL_INVALID", "invalid bridge startup frame"
        ) from exc
    if not isinstance(payload_object, dict):
        raise BridgeServerError("BRIDGE_PROTOCOL_INVALID", "invalid bridge startup frame")
    payload = cast(dict[object, object], payload_object)
    if set(payload) != {"version", "capability"}:
        raise BridgeServerError("BRIDGE_PROTOCOL_INVALID", "invalid bridge startup frame")
    version = payload.get("version")
    capability = payload.get("capability")
    if version != PROTOCOL_VERSION:
        raise BridgeServerError("BRIDGE_PROTOCOL_INVALID", "invalid bridge startup frame")
    if not isinstance(capability, str) or CAPABILITY_PATTERN.fullmatch(capability) is None:
        raise BridgeServerError("BRIDGE_PROTOCOL_INVALID", "invalid bridge startup frame")
    return StartupConfig(version=PROTOCOL_VERSION, capability=capability)


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


def _probe_existing_socket(path: Path) -> bool:
    """Return True when an existing Unix socket is accepting connections."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(SOCKET_PROBE_TIMEOUT_SECONDS)
    try:
        probe.connect(str(path))
        return True
    except OSError as exc:
        if exc.errno in {errno.ECONNREFUSED, errno.ENOENT}:
            return False
        raise BridgeServerError("BRIDGE_SOCKET_UNSAFE", "existing socket cannot be verified") from exc
    finally:
        probe.close()


def _cleanup_owned_runtime(runtime_dir: Path, driver_socket: Path) -> None:
    """Remove only the expected owned child artifact and then the empty directory."""
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
    if socket_entry is not None and socket_entry.st_uid == os.geteuid():
        driver_socket.unlink(missing_ok=True)
    with suppress(OSError):
        runtime_dir.rmdir()


class BridgeServer:
    """Private authenticated Unix-socket server with one request per connection."""

    def __init__(
        self,
        socket_path: Path,
        capability: str,
        controller: DispatchController,
    ) -> None:
        if not socket_path.is_absolute():
            raise BridgeServerError("BRIDGE_SOCKET_UNSAFE", "bridge socket path must be absolute")
        if CAPABILITY_PATTERN.fullmatch(capability) is None:
            raise BridgeServerError("BRIDGE_PROTOCOL_INVALID", "invalid bridge capability")
        self.socket_path = socket_path
        self._capability = capability
        self._controller = controller
        self._listener: socket.socket | None = None
        self._bound_identity: tuple[int, int] | None = None

    def bind(self) -> None:
        """Bind after proving that no unsafe or live object occupies the path."""
        if self._listener is not None:
            raise BridgeServerError("BRIDGE_SOCKET_IN_USE", "bridge server is already bound")
        _secure_owned_directory(self.socket_path.parent)
        self._prepare_socket_path()

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
            entry = self.socket_path.lstat()
            self._bound_identity = (entry.st_dev, entry.st_ino)
            os.chmod(self.socket_path, 0o600)
            entry = self.socket_path.lstat()
            if (
                not stat.S_ISSOCK(entry.st_mode)
                or entry.st_uid != os.geteuid()
                or stat.S_IMODE(entry.st_mode) != 0o600
                or (entry.st_dev, entry.st_ino) != self._bound_identity
            ):
                raise BridgeServerError("BRIDGE_SOCKET_UNSAFE", "bound bridge socket is unsafe")
            listener.listen(16)
        except BaseException:
            listener.close()
            self._unlink_bound_socket_if_owned()
            self._bound_identity = None
            raise
        self._listener = listener

    def _prepare_socket_path(self) -> None:
        try:
            entry = self.socket_path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise BridgeServerError("BRIDGE_SOCKET_UNSAFE", "bridge socket path is unreadable") from exc
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISSOCK(entry.st_mode):
            raise BridgeServerError("BRIDGE_SOCKET_UNSAFE", "bridge socket path is not a Unix socket")
        if entry.st_uid != os.geteuid():
            raise BridgeServerError("BRIDGE_SOCKET_UNSAFE", "bridge socket owner is unsafe")
        if _probe_existing_socket(self.socket_path):
            raise BridgeServerError("BRIDGE_SOCKET_IN_USE", "bridge socket is already in use")

        try:
            current = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISSOCK(current.st_mode)
            or current.st_uid != os.geteuid()
            or (current.st_dev, current.st_ino) != (entry.st_dev, entry.st_ino)
        ):
            raise BridgeServerError("BRIDGE_SOCKET_UNSAFE", "bridge socket changed during validation")
        self.socket_path.unlink()

    def serve_once(self) -> None:
        """Accept one connection and process at most one bounded request."""
        listener = self._listener
        if listener is None:
            raise BridgeServerError("BRIDGE_SOCKET_UNSAFE", "bridge server is not bound")
        connection, _ = listener.accept()
        try:
            self._handle_connection(connection)
        finally:
            connection.close()

    def _listener_closed(self) -> bool:
        """Read mutable listener state without static narrowing across an accept call."""
        return self._listener is None

    def serve_forever(self) -> None:
        """Serve sequentially until the listener is closed or the process exits."""
        while not self._listener_closed():
            try:
                self.serve_once()
            except OSError:
                if self._listener_closed():
                    return
                raise

    def _handle_connection(self, connection: socket.socket) -> None:
        try:
            raw = self._read_one_request(connection)
            request = parse_request_line(raw)
            if not hmac.compare_digest(request.capability, self._capability):
                response = encode_error("POLICY_DENIED", "bridge capability rejected")
            else:
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
            response = encode_error("DRIVER_UNAVAILABLE", "computer-use bridge response exceeded limit")
        connection.sendall(response)

    @staticmethod
    def _read_one_request(connection: socket.socket) -> bytes:
        buffer = bytearray()
        while True:
            remaining = MAX_REQUEST_BYTES + 1 - len(buffer)
            if remaining <= 0:
                raise BridgeServerError("BRIDGE_PROTOCOL_INVALID", "bridge request exceeds limit")
            chunk = connection.recv(min(4096, remaining))
            if not chunk:
                raise BridgeServerError("BRIDGE_PROTOCOL_INVALID", "bridge request ended early")
            buffer.extend(chunk)
            newline = buffer.find(b"\n")
            if newline >= 0:
                if newline != len(buffer) - 1:
                    raise BridgeServerError(
                        "BRIDGE_PROTOCOL_INVALID", "bridge connection must contain one request"
                    )
                if len(buffer) > MAX_REQUEST_BYTES:
                    raise BridgeServerError("BRIDGE_PROTOCOL_INVALID", "bridge request exceeds limit")
                return bytes(buffer)

    def close(self) -> None:
        """Stop accepting and remove only the socket this instance actually bound."""
        listener = self._listener
        self._listener = None
        if listener is not None:
            listener.close()
        self._unlink_bound_socket_if_owned()
        self._bound_identity = None

    def _unlink_bound_socket_if_owned(self) -> None:
        identity = self._bound_identity
        if identity is None:
            return
        try:
            entry = self.socket_path.lstat()
        except (FileNotFoundError, OSError):
            return
        if (
            stat.S_ISSOCK(entry.st_mode)
            and entry.st_uid == os.geteuid()
            and (entry.st_dev, entry.st_ino) == identity
        ):
            self.socket_path.unlink(missing_ok=True)


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
