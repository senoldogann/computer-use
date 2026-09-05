"""Tests for cua_repl MCP server and protocol compliance."""

from __future__ import annotations

from computeruse.mcp.cua_repl import CuaReplServer
from computeruse.repl.engine import CuaReplEngine, CuaReplResult


class DummyEngine(CuaReplEngine):
    def execute(
        self, code: str, title: str | None = None, timeout_s: float = 60.0
    ) -> CuaReplResult:
        return CuaReplResult(
            status="completed",
            duration_ms=45,
            content=f"Executed: {code.strip()}",
            error=None,
        )


def test_mcp_server_initialize() -> None:
    server = CuaReplServer(engine=DummyEngine())
    resp = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp["id"] == 1
    assert resp["result"]["serverInfo"]["name"] == "cua_repl"
    assert "tools" in resp["result"]["capabilities"]


def test_mcp_server_tools_list() -> None:
    server = CuaReplServer(engine=DummyEngine())
    resp = server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = resp["result"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "js"
    assert "code" in tools[0]["inputSchema"]["properties"]


def test_mcp_server_tools_call_js() -> None:
    server = CuaReplServer(engine=DummyEngine())
    resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "js",
                "arguments": {
                    "code": 'var app = await cua.getApp("TextEdit");',
                    "title": "Open TextEdit",
                },
            },
        }
    )
    assert resp["id"] == 3
    result = resp["result"]
    assert not result["isError"]
    assert result["durationMs"] == 45
    assert (
        result["content"][0]["text"]
        == 'Executed: var app = await cua.getApp("TextEdit");'
    )
