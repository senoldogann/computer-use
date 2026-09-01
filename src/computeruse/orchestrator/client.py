"""Typed JSON-RPC client for the Rust actuation micro-driver.

This is the *only* contract boundary the orchestrator uses to reach the
physical layer (ADR-1). The driver is always a separate process behind a Unix
socket — never an imported module — so if it hangs or crashes we can reconnect,
and only this connector carries the OS-facing wait/backoff behaviour.

The pure transformation ``action_to_request`` is kept separate from the socket
I/O so the mapping (Python ``Action`` -> wire payload) is unit-testable and,
more importantly, so a fragile parse step never shares code with a blocking
``recv`` (Law 6 functional core / imperative shell split). We explicitly
separate ``method`` (written) from parameters so both sides hold one source of
contract truth that the drift test in ``tests/`` enforces.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from collections.abc import Mapping
from typing import Final, Self

from computeruse.orchestrator.schemas import Action
from computeruse.vision.ax import AXElement
from computeruse.vision.capture import ScreenCapture
from computeruse.vision.focus import FocusedWindow

LOGGER: Final = logging.getLogger(__name__)


class DriverRpcError(RuntimeError):
    """A JSON-RPC response arrived with ``ok != ack/pong`` — driver refused."""

    # Rich debug context (Law 6.3): method that failed plus the driver's message.
    def __init__(self, method: str, driver_message: str) -> None:
        super().__init__(f"driver rejected {method}: {driver_message}")
        self.method = method
        self.driver_message = driver_message


class DriverConnectionError(RuntimeError):
    """The Unix socket could not be reached after the configured retries."""

    def __init__(self, socket_path: str, attempts: int) -> None:
        super().__init__(f"cannot reach driver at {socket_path} after {attempts} attempts")
        self.socket_path = socket_path
        self.attempts = attempts


def action_to_request(action: Action) -> str:
    """Serialize a validated action into the driver's wire protocol.

    The driver expects ``{"method": <snake_case>, "params": {...}}``; the
    Python models use ``type`` as the discriminator, so we translate here.
    Raises :class:`ValueError` for orchestrator-internal actions (``wait``,
    ``finish``, ``load_skill``) which never travel over the socket (Law 6.3:
    explicit error, never a silent skip).
    """
    payload = action.model_dump(exclude_none=True)
    action_type = payload.pop("type")
    method = _method_for_action_type(action_type)
    request = {"method": method, "params": payload}
    return json.dumps(request, separators=(",", ":")) + "\n"


def _method_for_action_type(action_type: str) -> str:
    mapping: Mapping[str, str] = {
        "mouse_move": "mouse_move",
        "mouse_click": "mouse_click",
        "mouse_drag": "mouse_drag",
        "mouse_scroll": "mouse_scroll",
        "type_text": "type_text",
        "clipboard_paste": "clipboard_paste",
        "press_hotkey": "press_hotkey",
        "activate_app": "activate_app",
    }
    try:
        return mapping[action_type]
    except KeyError:
        raise ValueError(
            f"action {action_type!r} is orchestrator-internal and must not reach the driver"
        ) from None


class ActuationClient:
    """Imperative shell around the driver socket (Law 6: connector class).

    A client is intentionally *not* reusable across threads: a single control
    stream matches the driver's line-oriented request/response model and keeps
    ordering deterministic for the OODA loop.
    """

    def __init__(
        self,
        socket_path: str,
        *,
        connect_retries: int = 3,
        retry_delay_seconds: float = 0.2,
        recv_timeout_seconds: float = 10.0,
    ) -> None:
        self._socket_path = socket_path
        self._connect_retries = connect_retries
        self._retry_delay_seconds = retry_delay_seconds
        self._recv_timeout_seconds = recv_timeout_seconds
        self._sock: socket.socket | None = None
        # Persistent read buffer: a single `recv()` may carry *two* response
        # lines (or a partial line). Keeping leftover bytes here (instead of a
        # throwaway local) means a split response is reassembled and a pipelined
        # second response is not silently discarded (F3).
        self._buf = bytearray()

    @property
    def is_connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        """Open the Unix socket with exponential backoff (Law 6.2)."""
        for attempt in range(1, self._connect_retries + 1):
            try:
                self._sock = self._connect_once()
                LOGGER.debug("connected to driver at %s", self._socket_path)
                return
            except OSError as exc:
                wait = self._retry_delay_seconds * (2 ** (attempt - 1))
                LOGGER.warning(
                    "driver connect attempt %s/%s failed (%s); retrying in %.1fs",
                    attempt,
                    self._connect_retries,
                    exc,
                    wait,
                )
                time.sleep(wait)
        raise DriverConnectionError(self._socket_path, self._connect_retries)

    def _connect_once(self) -> socket.socket:
        if not os.path.exists(self._socket_path):
            raise OSError(f"socket file {self._socket_path} does not exist")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._recv_timeout_seconds)
        try:
            sock.connect(self._socket_path)
        except OSError:
            sock.close()
            raise
        return sock

    def send(self, action: Action) -> None:
        """Serialize an action, send it, and assert the driver accepted it.

        Orchestrator-internal actions (wait/finish/load_skill) raise
        :class:`ValueError` before any bytes are written.
        """
        payload = action_to_request(action)
        assert self._sock is not None, "client not connected; call connect() first"
        self._sock.sendall(payload.encode("utf-8"))
        response = self._read_response()
        if response.get("ok") not in {"ack", "pong"}:
            message = str(response.get("message", "unknown driver error"))
            raise DriverRpcError(method=action.type, driver_message=message)

    def request(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Low-level request for diagnostics/perception (e.g. ``ping``).

        Parameterless methods (``ping``, ``focused_window``) omit the
        ``params`` key entirely, matching the driver's unit-variant wire shape.
        """
        assert self._sock is not None, "client not connected; call connect() first"
        body: dict[str, object] = {"method": method}
        if params:
            body["params"] = dict(params)
        payload = json.dumps(body, separators=(",", ":"))
        self._sock.sendall((payload + "\n").encode("utf-8"))
        return self._read_response()

    def capture(self, display_id: int = 0) -> ScreenCapture:
        """Capture the display through the driver and return a typed frame.

        ``display_id == 0`` means the main display. This is the OBSERVE step's
        sensor: the raw pixels are decoded (and diffed) by the pure vision
        layer, so the loop never sees the bytes. A driver-side refusal (e.g.
        missing Screen Recording consent) surfaces as :class:`DriverRpcError`
        with the driver's own message, so the ORIENT step can fold the real
        reason into ``last_error`` instead of a generic parse failure.
        """
        response = self.request("screenshot", {"display_id": display_id})
        if response.get("ok") != "screenshot":
            message = str(response.get("message", "unknown driver error"))
            raise DriverRpcError(method="screenshot", driver_message=message)
        return ScreenCapture.from_response(response)

    def activate_app(self, app: str) -> None:
        """Bring an application to the front (LaunchServices ``open -a``).

        The named app becomes the frontmost process so OBSERVE grounds against
        the app the caller actually meant — not whatever happened to be
        frontmost (typically the terminal the CLI was launched from). Only
        meaningful on the real backend; the simulated backend logs and ACKs
        (Law 1: it never touches the host). A driver-side refusal (e.g. an
        app name LaunchServices cannot resolve) surfaces as
        :class:`DriverRpcError` with the driver's message.
        """
        response = self.request("activate_app", {"app": app})
        if response.get("ok") != "ack":
            message = str(response.get("message", "unknown driver error"))
            raise DriverRpcError(method="activate_app", driver_message=message)

    def hotkey_state(self) -> bool:
        """Read the driver's global kill-hotkey state (Law 5.2).

        True once the user pressed the kill combo (Command+Shift+Escape) on
        the host; the OODA kill-switch polls this before every step. The
        simulated driver always reports False — there is no real event stream
        to listen to (Law 1: no accidental host interaction).
        """
        response = self.request("hotkey_state")
        if response.get("ok") != "hotkey_state":
            message = str(response.get("message", "unknown driver error"))
            raise DriverRpcError(method="hotkey_state", driver_message=message)
        tripped = response.get("tripped")
        if not isinstance(tripped, bool):
            raise TypeError("malformed hotkey_state response: tripped must be a bool")
        return tripped

    def focused_window(self) -> FocusedWindow:
        """Read the frontmost app, its focused window, and the cursor.

        This is the OBSERVE step's window/cursor half (§5): the active window
        title and cursor position the loop folds into the provider's context,
        plus the pid that feeds :meth:`ax_snapshot` when no app was named.
        A driver-side refusal (e.g. missing Accessibility consent) surfaces as
        :class:`DriverRpcError` with the driver's own message.
        """
        response = self.request("focused_window")
        if response.get("ok") != "focused_window":
            message = str(response.get("message", "unknown driver error"))
            raise DriverRpcError(method="focused_window", driver_message=message)
        return FocusedWindow.model_validate(response)

    def ax_snapshot(self, pid: int, max_depth: int = 8) -> AXElement:
        """Read an app's accessibility tree root (ADR-2 primary source).

        ``pid`` is the target application's process id; ``max_depth`` caps the
        traversal. The returned tree *generates* candidate coordinates that
        the pixel pipeline then verifies. A driver-side refusal (e.g. missing
        Accessibility consent) surfaces as :class:`DriverRpcError` with the
        driver's own message.
        """
        response = self.request("ax_snapshot", {"pid": pid, "max_depth": max_depth})
        if response.get("ok") != "ax_snapshot":
            message = str(response.get("message", "unknown driver error"))
            raise DriverRpcError(method="ax_snapshot", driver_message=message)
        root = response.get("root")
        if not isinstance(root, dict):
            raise TypeError("malformed ax_snapshot response: missing root element")
        return AXElement.model_validate(root)

    def _read_response(self) -> dict[str, object]:
        """Read exactly one newline-terminated response, preserving leftovers.

        Bytes beyond the first ``\n`` (from a pipelined or coalesced send) stay
        in ``self._buf`` for the next call, so no data is ever dropped (F3).
        """
        assert self._sock is not None
        while True:
            newline = self._buf.find(b"\n")
            if newline != -1:
                line = bytes(self._buf[:newline])
                # Keep everything after the newline for the next read.
                del self._buf[: newline + 1]
                return json.loads(line.decode("utf-8"))
            chunk = self._sock.recv(4096)
            if not chunk:
                raise DriverConnectionError(self._socket_path, self._connect_retries)
            self._buf.extend(chunk)

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self._buf.clear()

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()