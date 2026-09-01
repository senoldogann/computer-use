"""Focused-window perception tests — the OBSERVE step's window/cursor half.

The simulated driver serves a deterministic frontmost-app fixture (Safari,
pid 4242), so the full chain — RPC wire shape, typed client, pure summary,
working-state threading, prompt rendering, and Agent-level auto-discovery — is
exercised end to end through the real compiled driver.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from computeruse.agent import Agent, AgentConfig
from computeruse.memory.semantic import SemanticEntry, SemanticStore
from computeruse.orchestrator.client import ActuationClient, DriverRpcError
from computeruse.orchestrator.loop import OodaRunner, WorkingState
from computeruse.orchestrator.prompts import decision_prompt
from computeruse.orchestrator.schemas import AgentTurn, Finish, MouseClick
from computeruse.security.autonomy import AutonomyLevel
from computeruse.vision import FocusedWindow, window_summary
from tests.smoke.conftest import SOCKET_PATH, rpc_call

FIXTURE_SUMMARY = "Safari — GitHub — computeruse (cursor 420,300)"


def test_focused_window_wire_shape() -> None:
    payload = rpc_call({"method": "focused_window"})
    assert payload.get("ok") == "focused_window"
    assert payload.get("pid") == 4242
    assert payload.get("app_name") == "Safari"
    assert payload.get("window_title") == "GitHub — computeruse"
    assert payload.get("cursor_x") == 420.0
    assert payload.get("cursor_y") == 300.0


def test_typed_focused_window_via_client() -> None:
    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        focused = client.focused_window()
    assert isinstance(focused, FocusedWindow)
    assert (focused.pid, focused.app_name) == (4242, "Safari")
    assert focused.window_title == "GitHub — computeruse"
    assert (focused.cursor_x, focused.cursor_y) == (420.0, 300.0)


def test_window_summary_is_pure_and_compact() -> None:
    focused = FocusedWindow(
        pid=1,
        app_name="Safari",
        window_title="GitHub",
        cursor_x=420.5,
        cursor_y=300.0,
    )
    assert window_summary(focused) == "Safari — GitHub (cursor 420,300)"
    # A bare desktop (no window title) omits the window segment entirely.
    bare = FocusedWindow(pid=2, app_name="Finder", cursor_x=0.0, cursor_y=0.0)
    assert window_summary(bare) == "Finder (cursor 0,0)"


def _provider() -> Callable[[WorkingState], AgentTurn]:
    """Two clicks then finish — the distiller's minimum for a skill."""

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click the first thing",
                action=MouseClick(type="mouse_click", x=10, y=10),
            )
        if state.step_index == 1:
            return AgentTurn(
                thought="second",
                sub_goal="click the second thing",
                action=MouseClick(type="mouse_click", x=20, y=20),
            )
        return AgentTurn(
            thought="done",
            sub_goal="workflow complete",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    return provider


def test_loop_refreshes_active_window_before_every_decision() -> None:
    """OBSERVE folds the window summary into the state the provider sees."""
    seen: list[str | None] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state.active_window)
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click",
                action=MouseClick(type="mouse_click", x=1, y=1),
            )
        return AgentTurn(
            thought="done",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        window_probe=lambda: FocusedWindow(
            pid=4242,
            app_name="Safari",
            window_title="GitHub — computeruse",
            cursor_x=420.0,
            cursor_y=300.0,
        ),
    )
    state = runner.run("probe me")
    # Both provider turns (click + finish) see the summary; the pure step
    # reduction preserves it, so it never flickers out of the context.
    assert seen == [FIXTURE_SUMMARY, FIXTURE_SUMMARY]
    assert state.active_window == FIXTURE_SUMMARY


def test_probe_failure_warns_once_per_run(caplog) -> None:
    """A permanently failing probe warns once, not once per step.

    The user's real-run complaint: twenty identical consent-refusal lines for
    one run. Best-effort perception must degrade silently after the first
    loud warning (Law 6.3), not re-report the same permanent condition every
    step.
    """

    def failing_probe() -> FocusedWindow:
        raise DriverRpcError("focused_window", "consent revoked")

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click",
                action=MouseClick(type="mouse_click", x=1, y=1),
            )
        return AgentTurn(
            thought="done",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        window_probe=failing_probe,
    )
    with caplog.at_level(logging.WARNING):
        runner.run("g")
    warnings = [
        record for record in caplog.records if "focused-window probe failed" in record.getMessage()
    ]
    assert len(warnings) == 1


def test_probe_failure_degrades_without_aborting() -> None:
    """A perception gap must not kill the workflow (best-effort, Law 6.3)."""

    def failing_probe() -> FocusedWindow:
        raise DriverRpcError("focused_window", "consent revoked")

    seen: list[str | None] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state.active_window)
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click",
                action=MouseClick(type="mouse_click", x=1, y=1),
            )
        return AgentTurn(
            thought="done",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        window_probe=failing_probe,
    )
    state = runner.run("g")
    assert state.last_error is None, "a probe failure must not surface as a step error"
    assert seen == [None, None]


def test_decision_prompt_includes_active_window() -> None:
    state = WorkingState(goal="g", active_window=FIXTURE_SUMMARY)
    prompt = decision_prompt(state, app="Safari")
    assert f"Active window: {FIXTURE_SUMMARY}" in prompt
    # Without a probe the prompt is unchanged (backwards compatible).
    bare = WorkingState(goal="g")
    assert "Active window:" not in decision_prompt(bare, app="Safari")


def test_agent_auto_discovers_focused_app(tmp_path) -> None:
    """app=None discovers the frontmost app from the driver, end to end."""
    config = AgentConfig(
        goal="open the menu",
        app=None,
        provider=_provider(),
        socket_path=str(SOCKET_PATH),
        store_dir=tmp_path / "store",
        autonomy_level=AutonomyLevel.GUARDED,
        enable_visual_verification=False,  # simulated driver never renders
        max_steps=10,
    )
    result = Agent(config).run()
    assert result.app == "Safari"
    assert result.distilled is not None and result.distilled.kind == "skill"


def test_discovery_feeds_knowledge_and_window_context(tmp_path) -> None:
    """The discovered app drives both RETRIEVE and the per-turn OBSERVE probe."""
    store = SemanticStore(tmp_path / "store" / "semantic")
    store.put(
        SemanticEntry(
            entry_id="safari.shortcut.fullscreen",
            app="Safari",
            key="shortcut.fullscreen",
            value="Ctrl+Cmd+F",
            kind="shortcut",
            tags=("fullscreen",),
        )
    )
    seen: list[WorkingState] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state)
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click",
                action=MouseClick(type="mouse_click", x=1, y=1),
            )
        return AgentTurn(
            thought="done",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    config = AgentConfig(
        goal="open the menu",
        app=None,
        provider=provider,
        socket_path=str(SOCKET_PATH),
        store_dir=tmp_path / "store",
        autonomy_level=AutonomyLevel.GUARDED,
        enable_visual_verification=False,
        max_steps=10,
    )
    result = Agent(config).run()
    assert result.app == "Safari"
    assert any(
        "[Safari] shortcut.fullscreen: Ctrl+Cmd+F" in fact for fact in seen[0].knowledge
    )
    assert seen[0].active_window == FIXTURE_SUMMARY
