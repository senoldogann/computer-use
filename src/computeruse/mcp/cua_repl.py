"""MCP Server implementation for `cua_repl` (CUA JavaScript REPL).

Implements the Model Context Protocol (MCP) server providing the `js` tool
for executing programmatic multi-action computer use scripts.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from computeruse.repl.engine import CuaReplEngine

LOGGER = logging.getLogger(__name__)

JS_TOOL_SCHEMA: dict[str, Any] = {
    "name": "js",
    "description": (
        "Execute JavaScript in the CUA REPL environment with `globalThis.cua` for native Mac "
        "app control. Batch UI operations and call `await app.getAXState()` to return state/diff."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The JavaScript code to execute.",
            },
            "title": {
                "type": "string",
                "description": "Optional human-readable title describing the action.",
            },
        },
        "required": ["code"],
    },
}


class CuaReplServer:
    """Synchronous stdio MCP server for cua_repl."""

    def __init__(self, engine: CuaReplEngine | None = None) -> None:
        self.engine = engine or CuaReplEngine()

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Route an incoming MCP JSON-RPC request."""
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "cua_repl",
                        "version": "1.0.0",
                    },
                },
            }

        if method == "notifications/initialized":
            return {}

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [JS_TOOL_SCHEMA],
                },
            }

        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})

            if tool_name != "js":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32601,
                        "message": f"Unknown tool: {tool_name}",
                    },
                }

            code = arguments.get("code", "")
            title = arguments.get("title")

            exec_res = self.engine.execute(code, title=title)

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": exec_res.content
                            if exec_res.status == "completed"
                            else (exec_res.error or "Error"),
                        }
                    ],
                    "isError": exec_res.status == "failed",
                    "durationMs": exec_res.duration_ms,
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32601,
                "message": f"Method not found: {method}",
            },
        }

    def run_stdio(self) -> None:
        """Run the MCP server reading from sys.stdin and writing to sys.stdout."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                resp = self.handle_request(req)
                if resp:
                    sys.stdout.write(json.dumps(resp) + "\n")
                    sys.stdout.flush()
            except Exception as exc:  # noqa: BLE001
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": str(exc)},
                }
                sys.stdout.write(json.dumps(err_resp) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    server = CuaReplServer()
    try:
        server.run_stdio()
    finally:
        server.engine.stop()
