"""Isolated tests for the 3 Destructive-Action Channels (Safety Gate Hardening).

Verifies that human confirmation is strictly mandatory and no physical actuation occurs for:
1. Destructive shell commands inside clipboard_paste (e.g. `rm -rf`).
2. Destructive commands / payload passed via call_tool (e.g. MCP bash `rm`, delete tools).
3. Destructive UI controls (e.g. clicking 'Empty Trash', 'Delete', or Cmd+Delete hotkey).

Wired against the real VALIDATE gate (``decide_permission`` + ``classify_risk``
+ runner ``guard=``): the assertion that matters is not the risk label but
that zero bytes reach the physical layer without a human.
"""

from __future__ import annotations

from computeruse.orchestrator.loop import (
    Observation,
    OodaRunner,
    WorkingState,
    target_element_label,
)
from computeruse.orchestrator.schemas import (
    AgentTurn,
    CallTool,
    ClipboardPaste,
    Finish,
    MouseClick,
    PressHotkey,
)
from computeruse.security.autonomy import (
    AutonomyLevel,
    Risk,
    classify_risk,
    decide_permission,
)
from computeruse.security.permissions import (
    PermissionConfirmationRequired,
    PermissionDecision,
    PermissionDeniedError,
)


def _turn(action: object, sub_goal: str = "Perform step") -> AgentTurn:
    return AgentTurn.model_validate({"thought": "safety test", "sub_goal": sub_goal, "action": action})


def _guarded_runner(provider: object, executed: list[str]) -> OodaRunner:
    """Production VALIDATE wiring: guard on, no human attached, tripwire driver."""

    def guard(turn: AgentTurn, observation: Observation) -> object:
        label = target_element_label(turn.action, observation)
        return decide_permission(
            AutonomyLevel.GUARDED, classify_risk(turn, target_label=label)
        )

    def execute(action: object) -> None:
        executed.append(str(action))
        raise AssertionError("destructive action must never reach the driver")

    return OodaRunner(
        provider=provider,  # type: ignore[arg-type]
        execute_physical=execute,
        guard=guard,  # type: ignore[arg-type]
        max_steps=3,
    )


def _blocked(provider: object, executed: list[str], goal: str) -> None:
    runner = _guarded_runner(provider, executed)
    try:
        runner.run(goal=goal)
    except (PermissionDeniedError, PermissionConfirmationRequired):
        pass
    else:
        raise AssertionError("guard did not stop the destructive action")
    assert executed == [], "destructive action reached the driver without confirmation"


# ---------------------------------------------------------------------------
# Channel 1: clipboard_paste containing destructive shell command
# ---------------------------------------------------------------------------


def test_destructive_clipboard_paste_is_flagged_and_blocked() -> None:
    """A clipboard_paste containing a destructive shell command must be DESTRUCTIVE and require confirmation."""
    turn = _turn(
        ClipboardPaste(type="clipboard_paste", text="rm -rf /Users/test/Documents"),
        sub_goal="Clean up directory",
    )

    # 1. Autonomy classification check
    risk = classify_risk(turn)
    assert risk == Risk.DESTRUCTIVE, f"Expected DESTRUCTIVE, got {risk}"

    # 2. Permission gate check
    assert (
        decide_permission(AutonomyLevel.GUARDED, risk) is PermissionDecision.CONFIRM
    ), "gate must require confirmation for destructive paste"

    # 3. Execution check in runner: physical executor must NEVER be called without approval
    executed_physical: list[str] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return turn
        return _turn(Finish(type="finish", status="success", summary="done"))

    _blocked(provider, executed_physical, "Test destructive paste blocking")


# ---------------------------------------------------------------------------
# Channel 2: call_tool with destructive command / arguments
# ---------------------------------------------------------------------------


def test_destructive_call_tool_is_flagged_and_blocked() -> None:
    """An MCP tool call executing a destructive command must be DESTRUCTIVE and require confirmation."""
    # Test 2a: Tool name itself is destructive
    turn_tool_name = _turn(
        CallTool(type="call_tool", tool="delete_database_table", arguments={"table": "users"}),
        sub_goal="Remove database table",
    )
    assert classify_risk(turn_tool_name) == Risk.DESTRUCTIVE

    # Test 2b: Tool arguments contain destructive shell command
    turn_tool_args = _turn(
        CallTool(type="call_tool", tool="bash", arguments={"command": "rm -rf /var/log/*"}),
        sub_goal="Truncate logs",
    )
    assert classify_risk(turn_tool_args) == Risk.DESTRUCTIVE

    # Permission gate must gate both
    assert (
        decide_permission(AutonomyLevel.GUARDED, classify_risk(turn_tool_name))
        is PermissionDecision.CONFIRM
    )
    assert (
        decide_permission(AutonomyLevel.GUARDED, classify_risk(turn_tool_args))
        is PermissionDecision.CONFIRM
    )

    # Execution check: the guard stops the run before any routing
    executed_physical: list[str] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return turn_tool_args
        return _turn(Finish(type="finish", status="success", summary="done"))

    _blocked(provider, executed_physical, "Test destructive tool blocking")


# ---------------------------------------------------------------------------
# Channel 3: Destructive UI controls (click or hotkey)
# ---------------------------------------------------------------------------


def test_destructive_ui_control_is_flagged_and_blocked() -> None:
    """Clicking a destructive UI control or issuing a destructive hotkey must be DESTRUCTIVE."""
    # Test 3a: Clicking a button titled "Empty Trash" / "Delete Account"
    click_turn = _turn(
        MouseClick(type="mouse_click", x=500, y=500),
        sub_goal="Proceed with deletion",
    )
    assert classify_risk(click_turn, target_label="Empty Trash") == Risk.DESTRUCTIVE
    assert classify_risk(click_turn, target_label="Delete Account") == Risk.DESTRUCTIVE
    assert classify_risk(click_turn, target_label="Move to Trash") == Risk.DESTRUCTIVE

    # Test 3b: Pressing Command+Delete hotkey
    hotkey_turn = _turn(
        PressHotkey(type="press_hotkey", key="delete", modifiers=["command"]),
        sub_goal="Delete selected items",
    )
    assert classify_risk(hotkey_turn) == Risk.DESTRUCTIVE

    # Execution check: Physical click or hotkey must NOT execute without human confirmation
    executed_physical: list[str] = []
    destructive_click = _turn(
        MouseClick(type="mouse_click", x=500, y=500),
        sub_goal="Delete the file",
    )

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return destructive_click
        return _turn(Finish(type="finish", status="success", summary="done"))

    _blocked(provider, executed_physical, "Test destructive UI button blocking")
