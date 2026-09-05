"""Tests for CUA REPL Code-as-Action integration in the OODA Loop and Prompts."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from computeruse.orchestrator.loop import (
    AxProbeResult,
    FocusedWindow,
    OodaRunner,
    WorkingState,
)
from computeruse.orchestrator.prompts import parse_decision
from computeruse.orchestrator.schemas import (
    Action,
    AgentTurn,
    CallTool,
    Finish,
)
from computeruse.repl.engine import CuaReplEngine
from computeruse.vision.ax import AXElement


def test_parse_decision_openai_mcp_tool_call_format() -> None:
    """OpenAI CUA mcpToolCall JSON format should be cleanly normalized into CallTool."""
    raw_payload = {
        "type": "mcpToolCall",
        "id": "call_sBq7TMnqcocA58PdHVS4mMAj",
        "tool": "js",
        "server": "cua_repl",
        "status": "completed",
        "arguments": {
            "code": 'var textEditApp = await cua.getApp("TextEdit");',
            "title": "TextEdit test belgelerinin durumunu incele",
        },
    }
    raw_json = json.dumps(raw_payload)
    turn = parse_decision(raw_json)

    assert isinstance(turn, AgentTurn)
    assert turn.action.type == "call_tool"
    assert isinstance(turn.action, CallTool)
    assert turn.action.tool == "js"
    assert turn.action.arguments.get("code") == 'var textEditApp = await cua.getApp("TextEdit");'
    assert turn.sub_goal == "TextEdit test belgelerinin durumunu incele"


def test_parse_decision_direct_js_markdown_block() -> None:
    """Direct JavaScript markdown blocks should be parsed into CallTool actions."""
    raw_text = (
        "I need to query the TextEdit window.\n"
        "```javascript\n"
        'var app = await cua.getApp("TextEdit");\n'
        "await app.click(0);\n"
        "```\n"
    )
    turn = parse_decision(raw_text)

    assert isinstance(turn, AgentTurn)
    assert turn.action.type == "call_tool"
    assert isinstance(turn.action, CallTool)
    assert turn.action.tool == "js"
    assert "await cua.getApp" in str(turn.action.arguments.get("code"))


def test_parse_decision_action_type_js_alias() -> None:
    """Action type 'js' or 'execute_js' should normalize to call_tool 'js'."""
    raw_json = json.dumps({
        "thought": "Execute script",
        "sub_goal": "Run script",
        "action": {
            "type": "js",
            "code": "await cua.sleep(50);",
        },
    })
    turn = parse_decision(raw_json)

    assert isinstance(turn, AgentTurn)
    assert turn.action.type == "call_tool"
    assert isinstance(turn.action, CallTool)
    assert turn.action.tool == "js"
    assert turn.action.arguments.get("code") == "await cua.sleep(50);"


def test_ooda_runner_executes_cua_repl_call_tool() -> None:
    """OodaRunner should directly execute CallTool('js') via internal CuaReplEngine."""
    mock_client = MagicMock()
    mock_client.app_pid.return_value = 1234
    mock_client.focused_window.return_value = {
        "title": "TextEdit",
        "app": "TextEdit",
        "app_name": "TextEdit",
        "pid": 1234,
        "window_id": 100,
        "x": 0.0,
        "y": 0.0,
        "width": 800.0,
        "height": 600.0,
    }
    mock_ax_root = AXElement(
        role="AXApplication",
        title="TextEdit",
        x=0.0,
        y=0.0,
        width=800.0,
        height=600.0,
        children=[
            AXElement(
                role="AXButton",
                title="New Document",
                x=50.0,
                y=50.0,
                width=100.0,
                height=30.0,
            )
        ],
    )
    mock_client.ax_snapshot.return_value = (mock_ax_root, "TextEdit")

    engine = CuaReplEngine(driver_client=mock_client)
    engine.start()

    executed_actions: list[Action] = []

    def mock_execute(act: Action) -> None:
        executed_actions.append(act)

    turns = [
        AgentTurn(
            thought="Inspect TextEdit via CUA REPL",
            sub_goal="Get AX state",
            action=CallTool(
                type="call_tool",
                tool="js",
                arguments={
                    "code": 'var app = await cua.getApp("TextEdit");'
                },
            ),
        ),
        AgentTurn(
            thought="We got the state, now finish",
            sub_goal="Complete goal",
            action=Finish(
                type="finish",
                status="success",
                summary="TextEdit inspected successfully",
            ),
        ),
    ]

    turn_iter = iter(turns)
    captured_states: list[WorkingState] = []

    def mock_provider(state: WorkingState) -> AgentTurn:
        captured_states.append(state)
        return next(turn_iter)

    runner = OodaRunner(
        provider=mock_provider,
        execute_physical=mock_execute,
        cua_repl=engine,
        window_probe=lambda: FocusedWindow(
            app="TextEdit",
            app_name="TextEdit",
            pid=1234,
            title="TextEdit",
            window_id=100,
            x=0,
            y=0,
            width=800,
            height=600,
        ),
        frontmost_probe=lambda: FocusedWindow(
            app="TextEdit",
            app_name="TextEdit",
            pid=1234,
            title="TextEdit",
            window_id=100,
            x=0,
            y=0,
            width=800,
            height=600,
        ),
        ax_probe=lambda: AxProbeResult(summaries=()),
        app="TextEdit",
    )

    result_state = runner.run("Inspect TextEdit")

    engine.stop()

    assert result_state.step_index == 2
    # Verify trajectory contains the CallTool action
    assert runner.executed_trajectory is not None
    assert len(runner.executed_trajectory) >= 1
    call_tool_action = runner.executed_trajectory[0]
    assert call_tool_action.type == "call_tool"

    # Verify tool result was delivered to the model in the next turn
    assert len(captured_states) == 2
    turn_2_state = captured_states[1]
    assert turn_2_state.tool_result is not None
    assert "call_tool js returned:" in turn_2_state.tool_result
    assert "## Computer Use" in turn_2_state.tool_result
    assert "New Document" in turn_2_state.tool_result


def test_cua_repl_click_action_dispatches_mouse_click() -> None:
    """CUA REPL app.click(idx) should dispatch real physical mouse_click to driver client."""
    mock_client = MagicMock()
    mock_client.app_pid.return_value = 1234
    mock_client.focused_window.return_value = {
        "title": "TextEdit",
        "app": "TextEdit",
        "app_name": "TextEdit",
        "pid": 1234,
        "window_id": 100,
        "x": 0.0,
        "y": 0.0,
        "width": 800.0,
        "height": 600.0,
    }
    mock_ax_root = AXElement(
        role="AXApplication",
        title="TextEdit",
        x=0.0,
        y=0.0,
        width=800.0,
        height=600.0,
        children=[
            AXElement(
                role="AXButton",
                title="Save Button",
                x=100.0,
                y=200.0,
                width=80.0,
                height=30.0,
            )
        ],
    )
    mock_client.ax_snapshot.return_value = (mock_ax_root, "TextEdit")

    engine = CuaReplEngine(driver_client=mock_client)
    engine.start()

    res = engine.execute(
        'var app = await cua.getApp("TextEdit"); await app.click(0);'
    )
    engine.stop()

    assert not res.is_error
    # mock_client.mouse_click should have been called at center of (100, 200, 80, 30) -> (140, 215)
    sent_actions = [call.args[0] for call in mock_client.send.call_args_list]
    assert any(getattr(act, "type", None) == "mouse_click" and act.x == 140 and act.y == 215 for act in sent_actions)


def test_agent_config_has_enable_cua_repl_flag() -> None:
    """AgentConfig should include enable_cua_repl defaulting to True."""
    from pathlib import Path

    from computeruse.agent import AgentConfig

    cfg = AgentConfig(
        goal="test",
        provider=lambda s: AgentTurn(thought="", sub_goal="", action=Finish(type="finish", status="success", summary="done")),
        socket_path="/tmp/test.sock",
        store_dir=Path("/tmp"),
    )
    assert cfg.enable_cua_repl is True
