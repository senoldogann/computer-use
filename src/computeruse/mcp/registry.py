"""Configured MCP servers, and the tools they lend the agent.

The registry owns three decisions the protocol layer deliberately leaves open.

*What a server inherits.* A server is a subprocess the agent starts, and
handing it the parent's whole environment would give an arbitrary program every
key and token the user has exported. It receives a minimal environment plus
whatever its own config declares — the same reasoning that keeps the OpenAI key
out of the driver.

*What the model is told.* A tool's name and description are written by the
server, not the user, and they land in the model's prompt. That is the same
untrusted-text problem the screen already poses, and it gets the same
treatment: rendered inside a data block, with control characters stripped, so a
description cannot impersonate an instruction.

*What happens when a server misbehaves.* One broken server must not stop the
others or the run. A server that will not start is logged and skipped, and the
agent works with what it has.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from computeruse.mcp.protocol import (
    DEFAULT_TIMEOUT_SECONDS,
    McpClient,
    McpError,
    McpServerConfig,
    McpTool,
    ToolResult,
)

LOGGER: Final = logging.getLogger(__name__)

#: Where server definitions live. The filename and shape match what other MCP
#: hosts use, so an existing config can be copied across unchanged.
DEFAULT_CONFIG_PATH: Final[Path] = Path.home() / ".computeruse" / "mcp.json"

#: Variables a server subprocess inherits. Everything else is withheld: PATH is
#: needed to find the executable, HOME for a server's own config, and the rest
#: keep interpreters and terminals working. Secrets are not on this list, and a
#: server that needs one declares it in its own `env` block where the user can
#: see exactly what they are handing over.
INHERITED_ENV: Final[frozenset[str]] = frozenset(
    {"PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "TMPDIR", "TERM"}
)

#: How many tools to describe to the model. A dozen servers can offer hundreds;
#: the prompt has to stay a prompt.
MAX_ADVERTISED_TOOLS: Final[int] = 40
#: How much of a server-written description to show. Long enough to explain a
#: tool, short enough that one verbose server cannot crowd out the others.
MAX_DESCRIPTION_CHARS: Final[int] = 200


@dataclass(frozen=True)
class ToolCallOutcome:
    """What a tool call produced, as the loop wants to read it."""

    text: str
    failed: bool


def load_server_configs(path: Path = DEFAULT_CONFIG_PATH) -> tuple[McpServerConfig, ...]:
    """Read server definitions from disk.

    A missing file means "no servers", not an error: MCP is optional, and an
    agent with none configured should simply not mention it.
    """
    if not path.is_file():
        return ()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("could not read MCP config at %s: %s", path, exc)
        return ()
    if not isinstance(raw, dict):
        LOGGER.warning("MCP config at %s is not an object", path)
        return ()
    document = cast("dict[str, object]", raw)
    servers = document.get("mcpServers")
    if not isinstance(servers, dict):
        return ()
    entries = cast("dict[str, object]", servers)
    configs: list[McpServerConfig] = []
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        spec = cast("dict[str, object]", entry)
        command = str(spec.get("command") or "").strip()
        if not command:
            LOGGER.warning("MCP server %r has no command; skipping", name)
            continue
        raw_args = spec.get("args")
        args = (
            tuple(str(item) for item in cast("list[object]", raw_args))
            if isinstance(raw_args, list)
            else ()
        )
        raw_env = spec.get("env")
        env = (
            {str(k): str(v) for k, v in cast("dict[str, object]", raw_env).items()}
            if isinstance(raw_env, dict)
            else {}
        )
        configs.append(McpServerConfig(name=name, command=command, args=args, env=env))
    return tuple(configs)


def server_environment(config: McpServerConfig) -> dict[str, str]:
    """The environment one server subprocess gets (pure given os.environ).

    An allowlist rather than a denylist. A denylist is a promise to have
    thought of every secret anyone might export, which is not a promise worth
    making to a program the user downloaded from someone else.
    """
    inherited = {
        name: value for name, value in os.environ.items() if name in INHERITED_ENV
    }
    inherited.update(config.env)
    return inherited


def sanitize_for_prompt(text: str, *, limit: int) -> str:
    """Make server-written text safe to put in front of the model (pure).

    Newlines and control characters collapse to spaces so a description cannot
    forge the structure of the prompt around it — the same defence the screen's
    text already gets, for the same reason: this text was written by someone
    other than the user, and it is about to be read by something that follows
    instructions. Also defangs the observed-data delimiter and strips bidi /
    Unicode line separators so a tool description cannot break out of its block.
    """

    flattened = "".join(
        " " if character < " " or character == "\x7f" else character
        for character in text
    )
    # Strip the same structural characters the screen sanitizer removes.
    flattened = re.sub(
        "[\u2028\u2029\u200e\u200f\u202a-\u202e\u2066-\u2069]",
        " ",
        flattened,
    )
    flattened = re.sub(
        r"</?\s*observed[_\s-]*data\s*/?>",
        "[escaped-tag]",
        flattened,
        flags=re.IGNORECASE,
    )
    collapsed = " ".join(flattened.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


class McpRegistry:
    """Every configured server, and the tools they offer between them."""

    def __init__(self, configs: tuple[McpServerConfig, ...]) -> None:
        self._configs = configs
        self._clients: dict[str, McpClient] = {}
        self._tools: dict[str, McpTool] = {}
        # Rendered once. The description is recomputed only when the tool set
        # changes, which is at startup; it was previously re-sanitised on every
        # observation, so a per-character pass over forty names and forty
        # descriptions ran on every step of every run to produce the same
        # string each time.
        self._described: tuple[str, ...] | None = None

    @property
    def tools(self) -> tuple[McpTool, ...]:
        """Discovered tools, ordered by qualified name so runs are repeatable."""
        return tuple(self._tools[key] for key in sorted(self._tools))

    def start(self) -> None:
        """Start every server and discover its tools.

        One server failing is logged and skipped. The alternative — refusing to
        run because a single optional integration is broken — would make every
        added server a new way for the agent to stop working.
        """
        for config in self._configs:
            client = McpClient(config)
            try:
                client.start(env=server_environment(config))
                discovered = client.list_tools()
            except McpError as exc:
                LOGGER.warning("MCP server %r unavailable: %s", config.name, exc)
                client.close()
                continue
            self._clients[config.name] = client
            for tool in discovered:
                self._tools[tool.qualified_name] = tool
            self._described = None
            LOGGER.info(
                "MCP server %r ready with %d tool(s)", config.name, len(discovered)
            )

    def describe(self) -> tuple[str, ...]:
        """One line per tool, for the model's prompt.

        Both halves are sanitised: a server chooses its own tool names as well
        as its descriptions, and a name is just as capable of carrying a
        sentence that reads like an instruction.
        """
        if self._described is not None:
            return self._described
        lines: list[str] = []
        for tool in self.tools[:MAX_ADVERTISED_TOOLS]:
            name = sanitize_for_prompt(tool.qualified_name, limit=80)
            description = sanitize_for_prompt(
                tool.description, limit=MAX_DESCRIPTION_CHARS
            )
            arguments = _describe_arguments(tool.input_schema)
            suffix = f" — {description}" if description else ""
            lines.append(f"{name}({arguments}){suffix}")
        if len(self._tools) > MAX_ADVERTISED_TOOLS:
            lines.append(
                f"(+{len(self._tools) - MAX_ADVERTISED_TOOLS} more tools not listed)"
            )
        self._described = tuple(lines)
        return self._described

    def call(
        self,
        qualified_name: str,
        arguments: dict[str, object],
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> ToolCallOutcome:
        """Invoke a tool by its qualified name.

        An unknown name comes back as a failed outcome rather than an
        exception: the model picked it, and being told which names exist is
        something it can act on immediately.
        """
        tool = self._tools.get(qualified_name)
        if tool is None:
            known = ", ".join(sorted(self._tools)) or "none"
            return ToolCallOutcome(
                text=f"no such tool {qualified_name!r}. Available: {known}",
                failed=True,
            )
        client = self._clients.get(tool.server)
        if client is None:
            return ToolCallOutcome(
                text=f"MCP server {tool.server!r} is not running", failed=True
            )
        try:
            result: ToolResult = client.call_tool(
                tool.name, arguments, timeout=timeout
            )
        except McpError as exc:
            return ToolCallOutcome(text=str(exc), failed=True)
        return ToolCallOutcome(text=result.text, failed=result.failed)

    def close(self) -> None:
        """Shut every server down. Safe to call more than once."""
        for client in self._clients.values():
            client.close()
        self._clients.clear()


def _describe_arguments(schema: dict[str, object]) -> str:
    """A tool's parameters as a short signature (pure).

    The full JSON Schema is often larger than everything else in the prompt
    combined, and the model needs the argument names far more than it needs
    their descriptions; required ones are marked so it knows what it cannot
    omit.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return ""
    fields = cast("dict[str, object]", properties)
    raw_required = schema.get("required")
    required: set[str] = (
        {str(name) for name in cast("list[object]", raw_required)}
        if isinstance(raw_required, list)
        else set()
    )
    parts: list[str] = []
    for name in list(fields)[:8]:
        marker = "" if name in required else "?"
        parts.append(f"{sanitize_for_prompt(str(name), limit=40)}{marker}")
    if len(fields) > 8:
        parts.append("…")
    return ", ".join(parts)
