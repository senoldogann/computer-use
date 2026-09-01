"""Tests for Law 5.1: the autonomy-level permission guard."""

from __future__ import annotations

from computeruse.orchestrator.loop import OodaRunner, WorkingState
from computeruse.orchestrator.schemas import AgentTurn, ClipboardPaste
from computeruse.security.autonomy import (
    AutonomyLevel,
    PermissionConfirmationRequired,
    PermissionDecision,
    PermissionDeniedError,
    Risk,
    classify_risk,
    decide_permission,
)


def _turn(**action: object) -> AgentTurn:
    action_fields = {"type": "mouse_click", "x": 10, "y": 10}
    action_fields.update(action)
    return AgentTurn.model_validate(
        {"thought": "do it", "sub_goal": action_fields.get("sub", "click"), "action": action_fields}
    )


def test_destructive_risk_detected_in_typed_payload() -> None:
    turn = _turn(
        type="type_text",
        text="rm -rf /  # remove",
        wpm=40,
        sub="clean up files",
    )
    assert classify_risk(turn) is Risk.DESTRUCTIVE


def test_clipboard_command_is_destructive() -> None:
    turn = AgentTurn(
        thought="paste command",
        sub_goal="open terminal",
        action=ClipboardPaste(type="clipboard_paste", text="sudo rm -rf /"),
    )
    assert classify_risk(turn) is Risk.DESTRUCTIVE


def test_benign_click_is_none() -> None:
    assert classify_risk(_turn(sub="open editor")) is Risk.NONE


def test_routine_marker_is_routine() -> None:
    assert classify_risk(_turn(sub="confirm dialog", type="press_hotkey", key="enter")) is Risk.ROUTINE


def test_level0_observer_blocks_everything() -> None:
    assert decide_permission(AutonomyLevel.OBSERVER, Risk.NONE) is PermissionDecision.BLOCK
    assert decide_permission(AutonomyLevel.OBSERVER, Risk.DESTRUCTIVE) is PermissionDecision.BLOCK


def test_level1_supervised_confirms_even_benign() -> None:
    assert decide_permission(AutonomyLevel.SUPERVISED, Risk.NONE) is PermissionDecision.CONFIRM


def test_level2_guarded_blocks_destructive() -> None:
    assert decide_permission(AutonomyLevel.GUARDED, Risk.DESTRUCTIVE) is PermissionDecision.CONFIRM
    assert decide_permission(AutonomyLevel.GUARDED, Risk.NONE) is PermissionDecision.ALLOW


def test_level3_full_still_confirms_destructive_actions() -> None:
    # Full autonomy never bypasses the destructive-action safety boundary.
    assert decide_permission(AutonomyLevel.FULL, Risk.DESTRUCTIVE) is PermissionDecision.CONFIRM
    assert decide_permission(AutonomyLevel.FULL, Risk.NONE) is PermissionDecision.ALLOW


def test_ooda_runner_blocks_destructive_via_guard() -> None:
    destructive_exc: PermissionDeniedError | PermissionConfirmationRequired | None = None

    def provider(_state: WorkingState) -> AgentTurn:
        return _turn(type="type_text", text="rm -rf ~", wpm=40, sub="cleanup")

    def execute(_action: object) -> None:
        raise AssertionError("destructive action must never reach the driver")

    from computeruse.security.autonomy import (
        AutonomyLevel,
        classify_risk,
        decide_permission,
    )

    def guard(turn: AgentTurn) -> PermissionDecision:
        return decide_permission(AutonomyLevel.GUARDED, classify_risk(turn))

    runner = OodaRunner(
        provider=provider, execute_physical=execute, guard=guard, max_steps=3
    )
    # GUARDED + destructive -> at least CONFIRM; we assert *no* physical call.
    try:
        runner.run(goal="cleanup")
    except (PermissionDeniedError, PermissionConfirmationRequired) as exc:
        destructive_exc = exc
    assert destructive_exc is not None, "guard did not stop the destructive action"


def test_ooda_runner_guard_off_when_not_provided() -> None:
    """Without a guard the runner behaves exactly as before (feature flag off)."""
    executed: list[str] = []

    def provider(state: WorkingState) -> AgentTurn:

        if state.step_index == 0:
            return _turn(type="mouse_click", x=1, y=2, sub="click")
        return AgentTurn.model_validate(
            {"thought": "", "sub_goal": "", "action": {"type": "finish", "status": "success", "summary": "ok"}}
        )

    def execute(action: object) -> None:
        executed.append(str(action))

    runner = OodaRunner(provider=provider, execute_physical=execute, max_steps=5)
    final = runner.run(goal="ok")
    assert final.step_index >= 1
    assert executed, "physical action should have run when guard is absent"