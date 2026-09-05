"""Tests for CUA REPL execution engine and MCP JSON contract."""

from __future__ import annotations

from typing import Any

from computeruse.orchestrator.schemas import (
    Action,
    ClipboardPaste,
    MouseClick,
    MouseDrag,
    MouseMove,
    MouseScroll,
    PressHotkey,
    TypeText,
)
from computeruse.repl.engine import (
    CuaReplEngine,
    CuaReplResult,
    parse_hotkey_action,
)
from computeruse.vision.ax import AXElement


class MockDriverClient:
    """Mock client capturing driver calls for verification."""

    def __init__(self) -> None:
        self.sent_actions: list[Action] = []
        self.activated_apps: list[str] = []

    def send(self, action: Action) -> None:
        self.sent_actions.append(action)

    def activate_app(self, app_name: str) -> None:
        self.activated_apps.append(app_name)

    def list_apps(self) -> list[str]:
        return ["TextEdit", "Safari", "Finder"]

    def screenshot(self) -> Any:
        class FakeCap:
            data = b"fake_png_data"

        return FakeCap()


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


def test_parse_hotkey_action_various_formats() -> None:
    # Key combination string
    res1 = parse_hotkey_action("Cmd+Shift+P")
    assert res1.modifiers == ["command", "shift"]
    assert res1.key == "p"

    # Separate modifier list
    res2 = parse_hotkey_action("a", ["Command"])
    assert res2.modifiers == ["command"]
    assert res2.key == "a"

    # Aliases
    res3 = parse_hotkey_action("ctrl+opt+enter")
    assert res3.modifiers == ["control", "alt"]
    assert res3.key == "return"

    res4 = parse_hotkey_action("Escape")
    assert res4.modifiers == []
    assert res4.key == "escape"


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


def test_cua_repl_engine_sleep_and_wait() -> None:
    engine = CuaReplEngine()
    try:
        res = engine.execute("await cua.sleep(20); await cua.wait(20); 'done';")
        assert res.status == "completed"
        assert res.content == "done"
    finally:
        engine.stop()


def test_cua_repl_engine_actions_dispatch() -> None:
    driver = MockDriverClient()
    engine = CuaReplEngine(driver_client=driver)
    try:
        code = """
        var app = await cua.getApp("TextEdit");
        await app.click([150, 200]);
        await app.doubleClick([150, 200]);
        await app.rightClick([150, 200]);
        await app.drag([100, 100], [300, 300]);
        await app.scroll([150, 200], "down", 2);
        await app.pressKey("Cmd+S");
        await app.typeText("Save me");
        await app.paste("Pasted text");
        const b64 = await app.getScreenshot();
        return b64;
        """
        res = engine.execute(code)
        assert res.status == "completed"
        assert "data:image/png;base64," in res.content

        # Verify dispatched actions in order
        actions = driver.sent_actions
        # 1. click: MouseMove, MouseClick(click_count=1, button="left")
        assert any(
            isinstance(a, MouseMove) and a.x == 150 and a.y == 200
            for a in actions
        )
        assert any(
            isinstance(a, MouseClick) and a.click_count == 1 and a.button == "left"
            for a in actions
        )
        # 2. doubleClick: MouseClick(click_count=2)
        assert any(
            isinstance(a, MouseClick) and a.click_count == 2 for a in actions
        )
        # 3. rightClick: MouseClick(button="right")
        assert any(
            isinstance(a, MouseClick) and a.button == "right" for a in actions
        )
        # 4. drag: MouseDrag
        assert any(
            isinstance(a, MouseDrag)
            and a.start_x == 100
            and a.start_y == 100
            and a.end_x == 300
            and a.end_y == 300
            for a in actions
        )
        # 5. scroll: MouseScroll(dy=240)
        assert any(
            isinstance(a, MouseScroll) and a.dy == 240 for a in actions
        )
        # 6. pressKey: PressHotkey(modifiers=["command"], key="s")
        assert any(
            isinstance(a, PressHotkey)
            and a.modifiers == ["command"]
            and a.key == "s"
            for a in actions
        )
        # 7. typeText: TypeText(text="Save me")
        assert any(
            isinstance(a, TypeText) and a.text == "Save me" for a in actions
        )
        # 8. paste: ClipboardPaste(text="Pasted text")
        assert any(
            isinstance(a, ClipboardPaste) and a.text == "Pasted text"
            for a in actions
        )
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

        res_syntax = engine.execute("const a = ;")
        assert res_syntax.status == "failed"
        assert res_syntax.error is not None
    finally:
        engine.stop()


def test_cua_repl_engine_list_apps_and_get_state() -> None:
    driver = MockDriverClient()
    engine = CuaReplEngine(driver_client=driver)
    try:
        res = engine.execute("const apps = await cua.listApps(); apps.length;")
        assert res.status == "completed"
        assert res.content == "3"

        res_state = engine.execute("const st = await cua.getState(); JSON.stringify(st);")
        assert res_state.status == "completed"
        assert '{"apps":[]}' in res_state.content
    finally:
        engine.stop()
