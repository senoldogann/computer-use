"""Regression tests for research evidence retention and verification discipline."""

from __future__ import annotations

from computeruse.orchestrator.loop import OodaRunner, WorkingState
from computeruse.orchestrator.prompts import ACTION_CONTRACT, COMPLETION_AUDIT_CONTRACT
from computeruse.orchestrator.schemas import AgentTurn, CallTool, Finish


def _search_turn(query: str, sub_goal: str) -> AgentTurn:
    return AgentTurn(
        thought="collect another independent source set",
        sub_goal=sub_goal,
        action=CallTool(
            type="call_tool",
            tool="exa.web_search_exa",
            arguments={"query": query},
        ),
    )


def test_repeated_same_tool_calls_preserve_distinct_actor_evidence(monkeypatch) -> None:
    """Different queries through one search tool must not overwrite each other.

    The live AI-news benchmark called ``exa.web_search_exa`` repeatedly with
    different queries. Tool evidence in the actor trail was keyed only by tool
    name, so each call replaced the previous answer even though the queries and
    findings were different.
    """
    seen: list[WorkingState] = []
    answers = iter(
        (
            "Reuters: OpenAI launches Astra",
            "Nature: clinical AI evaluation shifts toward patient outcomes",
        )
    )

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state)
        if state.step_index == 0:
            return _search_turn("AI Reuters Astra", "collect Reuters candidates")
        if state.step_index == 1:
            return _search_turn("AI Nature clinical", "collect Nature candidates")
        return AgentTurn(
            thought="research evidence collected",
            sub_goal="finish",
            action=Finish(type="finish", status="success", summary="done"),
        )

    monkeypatch.setattr(OodaRunner, "_run_tool", lambda _self, _action: next(answers))
    OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        max_steps=5,
    ).run(goal="research two distinct AI developments")

    actor_trail = seen[-1].observed_trail
    assert any("Reuters: OpenAI launches Astra" in item for item in actor_trail), actor_trail
    assert any("Nature: clinical AI evaluation" in item for item in actor_trail), actor_trail


def test_tool_history_preserves_tool_provenance_for_completion_audit(monkeypatch) -> None:
    """The auditor must know whether evidence came from search, fetch, or another tool."""
    answers = iter(("Reuters result",))

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _search_turn("AI Reuters", "discover candidates")
        return AgentTurn(
            thought="done",
            sub_goal="finish",
            action=Finish(type="finish", status="success", summary="done"),
        )

    monkeypatch.setattr(OodaRunner, "_run_tool", lambda _self, _action: next(answers))
    final = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        max_steps=4,
    ).run(goal="research one AI development")

    assert final.tool_history
    assert final.tool_history[0].startswith("call_tool exa.web_search_exa: ")


def test_research_contract_requires_source_verification() -> None:
    """Search snippets discover candidates; selected claims require source evidence."""
    assert "SEARCH RESULTS ARE DISCOVERY, NOT SOURCE VERIFICATION" in ACTION_CONTRACT
    assert "open or fetch" in ACTION_CONTRACT
    assert "source you actually rely on" in ACTION_CONTRACT
    assert "collect more candidates" in ACTION_CONTRACT
    assert "requested final count" in ACTION_CONTRACT
    assert "distinguish discovery evidence from source verification" in COMPLETION_AUDIT_CONTRACT
