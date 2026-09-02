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
from typing import Final, Self, cast

from computeruse.orchestrator.schemas import (
    Action,
    ActivateApp,
    ClipboardPaste,
    MouseDrag,
    MouseMove,
    TypeText,
)
from computeruse.vision.ax import AXElement
from computeruse.vision.capture import ScreenCapture
from computeruse.vision.focus import FocusedWindow

LOGGER: Final = logging.getLogger(__name__)

# AX snapshot node budget (bundled into the ``ax_snapshot`` RPC, matching the
# driver's serialization default). Chrome with a large page open exposes tens
# of thousands of AX nodes; walking and serializing all of them costs 0.5-2s
# per OBSERVE turn and produces a megabyte-scale payload the orchestrator
# trims down to its own summary count anyway. The driver applies this cap
# web-first (page content before chrome) and drops the overflow, so a heavy
# page cannot balloon the per-turn perception cost (Law 4.3 context budget).
AX_MAX_NODES: Final[int] = 4096
#: Traversal depth cap. Chrome nests its ``AXWebArea`` ten levels below the
#: app root and page links another four to eight below that, so a shallow cap
#: silently hides every website's content; ``AX_MAX_NODES`` is the real bound
#: on response size.
AX_MAX_DEPTH: Final[int] = 20


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


class DriverTimeoutError(RuntimeError):
    """A driver call did not answer within its deadline.

    Distinct from :class:`DriverConnectionError` because the driver is alive
    and may still be *performing the action* — a 400-character ``type_text``
    legitimately takes two minutes. The stream is abandoned rather than reused:
    a late reply would otherwise be read as the response to the *next* request,
    silently pairing every answer with the wrong question for the rest of the
    run. The next call reconnects.
    """

    def __init__(self, method: str, timeout_seconds: float) -> None:
        super().__init__(
            f"driver did not answer {method} within {timeout_seconds:.1f}s; "
            "the connection was reset to keep request/response pairing honest"
        )
        self.method = method
        self.timeout_seconds = timeout_seconds


# Per-call deadlines. A single global timeout cannot serve both a 5ms ``ping``
# and a ``type_text`` that the driver deliberately paces at human speed: the
# old flat 10s cut long typing off mid-word, desynced the stream, and made
# every following response answer the previous request.
BASE_TIMEOUT_SECONDS: Final[float] = 10.0
# Screenshots move tens of megabytes of base64 over the socket.
CAPTURE_TIMEOUT_SECONDS: Final[float] = 30.0
# ``open -a`` may cold-launch an application.
ACTIVATE_TIMEOUT_SECONDS: Final[float] = 45.0
# A heavy page (YouTube, a large document) exposes tens of thousands of AX
# nodes; the driver's budgeted walk is bounded but not instant.
AX_TIMEOUT_SECONDS: Final[float] = 20.0
# Headroom over an action's own computed duration, for scheduling jitter.
TIMEOUT_MARGIN_SECONDS: Final[float] = 10.0
# The driver clamps its inter-keystroke delay to this band (quartz.rs);
# mirroring the ceiling here keeps the deadline an over-estimate, never an
# under-estimate, whatever WPM the model asks for.
MAX_KEYSTROKE_DELAY_SECONDS: Final[float] = 0.4


def action_timeout_seconds(action: Action) -> float:
    """How long a driver call for this action may legitimately take (pure).

    Derived from the action's own physical cost rather than a flat constant:
    human-paced typing, a long drag, and an app cold-start each have a
    predictable worst case, and a deadline shorter than that is guaranteed to
    fire on a perfectly healthy driver.
    """
    if isinstance(action, TypeText):
        return len(action.text) * MAX_KEYSTROKE_DELAY_SECONDS + TIMEOUT_MARGIN_SECONDS
    if isinstance(action, ClipboardPaste):
        return BASE_TIMEOUT_SECONDS + TIMEOUT_MARGIN_SECONDS
    if isinstance(action, (MouseDrag, MouseMove)):
        # The driver stretches long distances beyond the requested duration
        # (human cadence), so triple it before adding the margin.
        return action.duration_ms / 1000.0 * 3 + TIMEOUT_MARGIN_SECONDS
    if isinstance(action, ActivateApp):
        return ACTIVATE_TIMEOUT_SECONDS
    return BASE_TIMEOUT_SECONDS


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
        # How much of ``_buf`` has already been scanned for a newline. Without
        # this, every recv chunk re-scans the whole (up to 40MB screenshot)
        # buffer, costing ~1.7s per capture (measured); scanning only the
        # newly-appended bytes keeps the read O(payload), not O(payload^2).
        self._scan_pos = 0

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
        sock = self._require_socket()
        sock.sendall(payload.encode("utf-8"))
        response = self._read_response(
            action.type, timeout_seconds=action_timeout_seconds(action)
        )
        if response.get("ok") not in {"ack", "pong"}:
            message = str(response.get("message", "unknown driver error"))
            raise DriverRpcError(method=action.type, driver_message=message)

    def request(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        """Low-level request for diagnostics/perception (e.g. ``ping``).

        Parameterless methods (``ping``, ``focused_window``) omit the
        ``params`` key entirely, matching the driver's unit-variant wire shape.
        """
        sock = self._require_socket()
        body: dict[str, object] = {"method": method}
        if params:
            body["params"] = dict(params)
        payload = json.dumps(body, separators=(",", ":"))
        sock.sendall((payload + "\n").encode("utf-8"))
        return self._read_response(
            method,
            timeout_seconds=(
                self._recv_timeout_seconds if timeout_seconds is None else timeout_seconds
            ),
        )

    def _require_socket(self) -> socket.socket:
        """The live socket, reconnecting once if a previous call reset it.

        A timeout deliberately drops the stream (see :class:`DriverTimeoutError`),
        so the very next call must be able to stand the connection back up —
        otherwise one slow action would end an otherwise healthy run.
        """
        if self._sock is None:
            self.connect()
        assert self._sock is not None, "client not connected; call connect() first"
        return self._sock

    def capture(self, display_id: int = 0) -> ScreenCapture:
        """Capture the display through the driver and return a typed frame.

        ``display_id == 0`` means the main display. This is the OBSERVE step's
        sensor: the raw pixels are decoded (and diffed) by the pure vision
        layer, so the loop never sees the bytes. A driver-side refusal (e.g.
        missing Screen Recording consent) surfaces as :class:`DriverRpcError`
        with the driver's own message, so the ORIENT step can fold the real
        reason into ``last_error`` instead of a generic parse failure.
        """
        response = self.request(
            "screenshot",
            {"display_id": display_id},
            timeout_seconds=CAPTURE_TIMEOUT_SECONDS,
        )
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
        response = self.request(
            "activate_app", {"app": app}, timeout_seconds=ACTIVATE_TIMEOUT_SECONDS
        )
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

    def list_apps(self) -> tuple[str, ...]:
        """Display names of running applications with on-screen windows.

        Feeds autonomous target-app inference: the CLI resolves a goal's
        implied app ("Excel'de aç", "YouTube'da arat") against what the user
        actually runs instead of requiring ``--app``. Best-effort by contract:
        an empty tuple (or a refused probe) means "unknown", never "no apps
        are running" — the caller falls back to frontmost-app discovery.
        """
        response = self.request("list_apps")
        if response.get("ok") != "list_apps":
            message = str(response.get("message", "unknown driver error"))
            raise DriverRpcError(method="list_apps", driver_message=message)
        raw = response.get("apps")
        if not isinstance(raw, list):
            raise TypeError("malformed list_apps response: apps must be a list of strings")
        apps: list[str] = []
        for name in cast(list[object], raw):
            if not isinstance(name, str):
                raise TypeError(
                    "malformed list_apps response: apps must contain only strings"
                )
            apps.append(name)
        return tuple(apps)

    def ax_snapshot(
        self,
        pid: int,
        max_depth: int = AX_MAX_DEPTH,
        max_nodes: int = AX_MAX_NODES,
    ) -> AXElement:
        """Read an app's accessibility tree root (ADR-2 primary source).

        ``pid`` is the target application's process id; ``max_depth`` caps the
        traversal and ``max_nodes`` caps the total returned nodes (web-first
        on the driver side, so page content is never starved by chrome). The
        returned tree *generates* candidate coordinates that the pixel
        pipeline then verifies. A driver-side refusal (e.g. missing
        Accessibility consent) surfaces as :class:`DriverRpcError` with the
        driver's own message.
        """
        response = self.request(
            "ax_snapshot",
            {"pid": pid, "max_depth": max_depth, "max_nodes": max_nodes},
            timeout_seconds=AX_TIMEOUT_SECONDS,
        )
        if response.get("ok") != "ax_snapshot":
            message = str(response.get("message", "unknown driver error"))
            raise DriverRpcError(method="ax_snapshot", driver_message=message)
        root = response.get("root")
        if not isinstance(root, dict):
            raise TypeError("malformed ax_snapshot response: missing root element")
        return AXElement.model_validate(root)

    def _read_response(self, method: str, *, timeout_seconds: float) -> dict[str, object]:
        """Read exactly one newline-terminated response within a deadline.

        Bytes beyond the first ``\n`` (from a pipelined or coalesced send) stay
        in ``self._buf`` for the next call, so no data is ever dropped (F3).

        Reads in 1 MiB chunks, not 4 KiB: a Retina screenshot is ~40 MB of
        base64, and a 4 KiB drain loop turned every capture into a ~4 s
        round-trip (10,000 syscalls + a growing-buffer scan per chunk), which
        blocked the driver's ``write_all`` for seconds. The newline search is
        resumable (``_scan_pos``): scanning only the newly-arrived bytes keeps
        the read linear in the payload size instead of re-scanning the whole
        buffer on every chunk (~1.7s saved per capture).

        ``timeout_seconds`` bounds the *whole* response, not each recv, so a
        driver that trickles bytes forever cannot hold the loop open. On expiry
        the stream is discarded rather than reused: the driver may still be
        mid-action, and its late reply would otherwise be handed back as the
        answer to the following request.
        """
        sock = self._require_socket()
        deadline = time.monotonic() + timeout_seconds
        while True:
            # Leftovers from a coalesced read may already hold a full line;
            # scan from the resumption point so it is found without waiting
            # for more bytes (F3) and without re-scanning old data.
            newline = self._buf.find(b"\n", self._scan_pos)
            if newline != -1:
                self._scan_pos = 0
                line = bytes(self._buf[:newline])
                # Keep everything after the newline for the next read.
                del self._buf[: newline + 1]
                return json.loads(line.decode("utf-8"))
            self._scan_pos = len(self._buf)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._reset_stream()
                raise DriverTimeoutError(method, timeout_seconds)
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(1 << 20)
            except TimeoutError as exc:
                self._reset_stream()
                raise DriverTimeoutError(method, timeout_seconds) from exc
            if not chunk:
                self._reset_stream()
                raise DriverConnectionError(self._socket_path, self._connect_retries)
            self._buf.extend(chunk)

    def _reset_stream(self) -> None:
        """Drop the connection and every buffered byte on it.

        Called whenever the request/response pairing can no longer be trusted.
        Reconnecting is cheap; misattributing a driver reply is not.
        """
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self._buf.clear()
        self._scan_pos = 0

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self._buf.clear()
        self._scan_pos = 0

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()