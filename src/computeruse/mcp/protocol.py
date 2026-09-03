"""The MCP wire protocol, as plain synchronous JSON-RPC over a pipe.

Written directly against the specification rather than through the official
SDK, for two reasons that pull the same way. The SDK is asynchronous, and this
project's loop is not: bridging them means an event loop on a background
thread and every call marshalled across it, which is real machinery to own for
a protocol whose entire surface here is three requests and one notification.
And the shape is already familiar — ``ActuationClient`` speaks line-delimited
JSON-RPC to the driver over a socket, and this speaks it to a server over a
pipe.

The protocol is small enough to state completely:

* ``initialize`` announces the version and capabilities, and the server answers
  with its own.
* ``notifications/initialized`` says the client is ready. The specification
  requires it before any other request, and a server that follows the rules
  will simply not answer until it arrives.
* ``tools/list`` enumerates what the server offers, paginated by ``nextCursor``.
* ``tools/call`` invokes one.

Reference: https://modelcontextprotocol.io/specification/2025-06-18
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Final, cast

LOGGER: Final = logging.getLogger(__name__)

#: The version this client speaks. The specification says to send the latest
#: supported; a server that speaks something else answers with its own version,
#: and a client that cannot speak it should disconnect rather than guess.
PROTOCOL_VERSION: Final[str] = "2025-06-18"

CLIENT_NAME: Final[str] = "computeruse"
CLIENT_VERSION: Final[str] = "1.0.0"

#: How long to wait for one response. The specification asks every client to
#: bound its requests so a hung server cannot hold the loop forever; a tool
#: doing real work is the reason this is generous rather than snappy.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0
#: Handshake and shutdown are protocol chatter, not work, and should be quick.
HANDSHAKE_TIMEOUT_SECONDS: Final[float] = 20.0
SHUTDOWN_GRACE_SECONDS: Final[float] = 5.0


class McpError(RuntimeError):
    """The server could not be reached, or broke the protocol.

    Distinct from a tool *failing*, which is an ordinary result the model can
    read and act on. This one means the connection itself is unusable.
    """


@dataclass(frozen=True)
class McpServerConfig:
    """How to start one server (pure data).

    Mirrors the shape every MCP host already uses in its config file, so a user
    can paste the entry they already have instead of learning another format.
    """

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=lambda: cast("dict[str, str]", {}))


@dataclass(frozen=True)
class McpTool:
    """One tool a server offers.

    ``description`` and ``input_schema`` are written by the server, which is to
    say by someone other than the user. They are shown to the model, so they
    are treated as untrusted text everywhere they are rendered — the
    specification says as much about annotations, and the same reasoning covers
    anything else a server can put in front of a language model.
    """

    server: str
    name: str
    description: str
    input_schema: dict[str, object]

    @property
    def qualified_name(self) -> str:
        """``server.tool`` — unique across servers that reuse a plain name."""
        return f"{self.server}.{self.name}"


@dataclass(frozen=True)
class ToolResult:
    """What a tool call produced.

    ``failed`` carries the protocol's ``isError``, which marks a tool that ran
    and could not do its job — an API refusing, a file missing. That is
    information for the model, not a breakage, which is why it is a field here
    rather than an exception.
    """

    text: str
    failed: bool


class McpClient:
    """One server, spoken to over its stdin and stdout.

    Not thread-safe by construction: requests are serialised behind a lock
    because a pipe has no way to interleave two conversations, and the id in a
    response is the only thing tying it to its request.
    """

    def __init__(self, config: McpServerConfig) -> None:
        self._config = config
        self._process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._lock = threading.Lock()
        self._server_info: dict[str, object] = {}

    @property
    def name(self) -> str:
        return self._config.name

    def start(self, *, env: dict[str, str]) -> None:
        """Spawn the server and complete the handshake.

        ``env`` is passed in rather than read here so a caller decides what a
        subprocess inherits — a server started by an agent should not
        automatically receive every secret in the parent's environment.
        """
        if self._process is not None:
            return
        try:
            self._process = subprocess.Popen(
                [self._config.command, *self._config.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise McpError(
                f"could not start MCP server {self._config.name!r} "
                f"({self._config.command}): {exc}"
            ) from exc
        self._handshake()

    def _handshake(self) -> None:
        """initialize, check the version, then say we are ready."""
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                # No client capabilities are declared because none are
                # implemented: this client does not serve roots, sampling or
                # elicitation back to the server, and claiming otherwise would
                # invite requests it would have to ignore.
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
            timeout=HANDSHAKE_TIMEOUT_SECONDS,
        )
        self._server_info = result
        agreed = str(result.get("protocolVersion", ""))
        if agreed and agreed != PROTOCOL_VERSION:
            # The specification allows a server to answer with a version of its
            # own choosing; a client that cannot speak it is told to
            # disconnect rather than continue and misread every later message.
            LOGGER.warning(
                "MCP server %r speaks %s, this client speaks %s — continuing, "
                "but messages may not be understood",
                self._config.name,
                agreed,
                PROTOCOL_VERSION,
            )
        self._notify("notifications/initialized")

    def list_tools(self) -> tuple[McpTool, ...]:
        """Every tool the server offers, following pagination to the end.

        A server that pages its tools and is only asked for the first page
        silently hides the rest, and the agent would never know the tool it
        needed existed.
        """
        tools: list[McpTool] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, object] = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params)
            for entry in _as_list(result.get("tools")):
                tool = _parse_tool(self._config.name, entry)
                if tool is not None:
                    tools.append(tool)
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            if next_cursor in seen_cursors:
                # A server that repeats a cursor would page forever. Stop with
                # what we have rather than hang on someone else's bug.
                LOGGER.warning(
                    "MCP server %r repeated pagination cursor; stopping",
                    self._config.name,
                )
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return tuple(tools)

    def call_tool(
        self, name: str, arguments: dict[str, object], *, timeout: float
    ) -> ToolResult:
        """Invoke one tool and flatten its answer to text for the model."""
        result = self._request(
            "tools/call", {"name": name, "arguments": arguments}, timeout=timeout
        )
        return ToolResult(
            text=flatten_content(result),
            failed=bool(result.get("isError", False)),
        )

    def close(self) -> None:
        """Shut the server down the way the specification asks.

        Close stdin first so a well-behaved server exits on its own; escalate
        only if it does not. Killing outright would deny it the chance to flush
        or clean up after itself.
        """
        process = self._process
        if process is None:
            return
        self._process = None
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=SHUTDOWN_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=SHUTDOWN_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()

    def _request(
        self,
        method: str,
        params: dict[str, object],
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        """One request/response round trip, serialised against other callers."""
        with self._lock:
            process = self._process
            if process is None or process.stdin is None or process.stdout is None:
                raise McpError(f"MCP server {self._config.name!r} is not running")
            request_id = self._next_id
            self._next_id += 1
            self._write(process, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return self._read_response(process, request_id, method, timeout)

    def _notify(self, method: str) -> None:
        """Fire-and-forget: a notification carries no id and gets no answer."""
        with self._lock:
            process = self._process
            if process is None or process.stdin is None:
                raise McpError(f"MCP server {self._config.name!r} is not running")
            self._write(process, {"jsonrpc": "2.0", "method": method})

    def _write(self, process: subprocess.Popen[str], message: dict[str, object]) -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpError(
                f"MCP server {self._config.name!r} closed its input: {exc}"
            ) from exc

    def _read_response(
        self,
        process: subprocess.Popen[str],
        request_id: int,
        method: str,
        timeout: float,
    ) -> dict[str, object]:
        """Read until the answer to *this* request arrives.

        A server may interleave its own notifications and logging with
        responses, so anything without our id is skipped rather than mistaken
        for the answer. The deadline is enforced by the reader thread the
        caller set up, not here, because a blocking pipe read cannot be
        interrupted by a timer.
        """
        assert process.stdout is not None
        deadline = threading.Event()
        timer = threading.Timer(timeout, deadline.set)
        timer.daemon = True
        timer.start()
        try:
            while True:
                if deadline.is_set():
                    raise McpError(
                        f"MCP server {self._config.name!r} did not answer {method!r} "
                        f"within {timeout:.0f}s"
                    )
                line = process.stdout.readline()
                if not line:
                    raise McpError(
                        f"MCP server {self._config.name!r} closed its output during {method!r}"
                    )
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    # Servers occasionally print to stdout despite the
                    # transport reserving it. Skipping is kinder than failing
                    # the call over someone else's stray print statement.
                    LOGGER.debug("non-JSON line from MCP server %r: %r", self._config.name, line[:200])
                    continue
                if not isinstance(message, dict):
                    continue
                envelope = cast("dict[str, object]", message)
                if envelope.get("id") != request_id:
                    continue
                error = envelope.get("error")
                if isinstance(error, dict):
                    detail = cast("dict[str, object]", error)
                    raise McpError(
                        f"MCP server {self._config.name!r} refused {method!r}: "
                        f"{detail.get('message', 'unknown error')}"
                    )
                result = envelope.get("result")
                return cast("dict[str, object]", result) if isinstance(result, dict) else {}
        finally:
            timer.cancel()


def flatten_content(result: dict[str, object]) -> str:
    """Render a tool result's content blocks as text for the model (pure).

    A result may carry text, images, audio, resource links and embedded
    resources. Only text reaches a text prompt intact, so the rest are named
    rather than dropped: an agent told "[image: image/png]" knows something
    came back and can decide what to do, while silence would read as an empty
    answer.
    """
    structured = result.get("structuredContent")
    blocks = _as_list(result.get("content"))
    parts: list[str] = []
    for entry in blocks:
        if not isinstance(entry, dict):
            continue
        block = cast("dict[str, object]", entry)
        kind = str(block.get("type", ""))
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind}: {block.get('mimeType', 'unknown type')}]")
        elif kind == "resource_link":
            parts.append(f"[resource: {block.get('uri', '')}]")
        elif kind == "resource":
            resource = block.get("resource")
            if isinstance(resource, dict):
                nested = cast("dict[str, object]", resource)
                parts.append(str(nested.get("text") or f"[resource: {nested.get('uri', '')}]"))
    text = "\n".join(part for part in parts if part)
    if not text and structured is not None:
        # A tool that returned only structured content still said something.
        return json.dumps(structured, ensure_ascii=False, default=str)
    return text


def _parse_tool(server: str, entry: object) -> McpTool | None:
    """One tool definition, or None when the server sent something unusable."""
    if not isinstance(entry, dict):
        return None
    data = cast("dict[str, object]", entry)
    name = str(data.get("name") or "").strip()
    if not name:
        return None
    schema = data.get("inputSchema")
    return McpTool(
        server=server,
        name=name,
        description=str(data.get("description") or data.get("title") or "").strip(),
        input_schema=cast("dict[str, object]", schema) if isinstance(schema, dict) else {},
    )


def _as_list(value: object) -> list[Any]:
    """A JSON array, or an empty list when the field is absent or malformed."""
    return cast("list[Any]", value) if isinstance(value, list) else []
