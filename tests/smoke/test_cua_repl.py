"""Tests for CUA REPL execution engine and MCP JSON contract."""

from __future__ import annotations

from computeruse.repl.engine import CuaReplEngine, CuaReplResult
from computeruse.vision.ax import AXElement


def test_cua_repl_result_mcp_json_contract() -> None:
    res = CuaReplResult(
        status="completed",
        duration_ms=125,
        content="The following is a diff from the previous accessibility tree...",
        error=None,
    )
    mcp_call = res.to_mcp_tool_call(
        call_id="call_sBq7TMnqcocA58PdHVS4mMAj",
        code='var textEditApp = await cua.getApp("TextEdit");',
        title="TextEdit test belgelerinin durumunu incele",
    )

    assert mcp_call["type"] == "mcpToolCall"
    assert mcp_call["id"] == "call_sBq7TMnqcocA58PdHVS4mMAj"
    assert mcp_call["tool"] == "js"
    assert mcp_call["server"] == "cua_repl"
    assert mcp_call["status"] == "completed"
    assert (
        mcp_call["arguments"]["code"]
        == 'var textEditApp = await cua.getApp("TextEdit");'
    )
    assert (
        mcp_call["arguments"]["title"] == "TextEdit test belgelerinin durumunu incele"
    )
    assert mcp_call["appContext"] is None
    assert mcp_call["error"] is None
    assert mcp_call["durationMs"] == 125
    assert "The following is a diff" in mcp_call["result"]["content"]


def test_cua_repl_engine_eval_basic_javascript() -> None:
    engine = CuaReplEngine()
    try:
        res = engine.execute("const a = 10; const b = 25; `Result: ${a + b}`;")
        assert res.status == "completed"
        assert res.error is None
        assert res.content == "Result: 35"
        assert res.duration_ms >= 0
    finally:
        engine.stop()


def test_cua_repl_engine_get_app_and_ax_diffing() -> None:
    # Custom snapshot provider simulating window state evolution
    turn_count = 0

    def mock_provider(app_name: str) -> tuple[AXElement, str]:
        nonlocal turn_count
        turn_count += 1
        if turn_count == 1:
            # Initial state
            root = AXElement(
                role="Window",
                title="Open",
                width=600,
                height=400,
                children=(
                    AXElement(
                        role="Button", title="Vazgeç", x=100, y=100, width=80, height=30
                    ),
                    AXElement(
                        role="Button", title="Aç", x=200, y=100, width=80, height=30
                    ),
                ),
            )
        else:
            # After click: "Aç" window closed, new document opened
            root = AXElement(
                role="Window",
                title="Untitled",
                width=800,
                height=600,
                children=(
                    AXElement(
                        role="TextArea",
                        title="Document",
                        value="Hello World",
                        x=50,
                        y=50,
                        width=700,
                        height=500,
                    ),
                ),
            )
        return root, "Open" if turn_count == 1 else "Untitled"

    engine = CuaReplEngine(snapshot_provider=mock_provider)
    try:
        # Step 1: getApp
        code_1 = """
        var textEditApp = await cua.getApp("TextEdit");
        """
        res_1 = engine.execute(code_1, title="TextEdit durumunu incele")
        assert res_1.status == "completed"
        assert 'Window: "Open", App: TextEdit.' in res_1.content
        assert '[1] Button "Vazgeç"' in res_1.content
        assert '[2] Button "Aç"' in res_1.content

        # Step 2: click and diff
        code_2 = """
        var textEditApp = await cua.getApp("TextEdit");
        await textEditApp.click(2);
        return await textEditApp.getAXState();
        """
        res_2 = engine.execute(code_2, title="Aç butonuna tıkla")
        assert res_2.status == "completed"
        assert (
            'The following is a diff from the previous accessibility tree for Window: "Untitled"'
            in res_2.content
        )
        assert '+ [1] TextArea "Document"' in res_2.content
    finally:
        engine.stop()


def test_cua_repl_engine_handles_syntax_and_runtime_errors() -> None:
    engine = CuaReplEngine()
    try:
        res = engine.execute("await nonExistentFunction();")
        assert res.status == "failed"
        assert res.error is not None
        assert "nonExistentFunction is not defined" in res.error
    finally:
        engine.stop()
