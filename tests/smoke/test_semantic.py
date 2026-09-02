"""Law 4.2 semantic memory tier + RETRIEVE-into-context tests.

The store persists app knowledge (preferences, patterns, shortcuts) as typed
entries; ``search_entries`` is the pure retrieval; ``Agent`` stages the app's
knowledge into the OODA working context as compact strings the provider sees.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from computeruse.agent import Agent, AgentConfig
from computeruse.memory.semantic import SemanticEntry, SemanticStore, search_entries
from computeruse.orchestrator.loop import OodaRunner, WorkingState
from computeruse.orchestrator.schemas import AgentTurn, Finish, MouseClick
from computeruse.security.autonomy import AutonomyLevel
from tests.smoke.conftest import SIMULATED_SETTLE, SOCKET_PATH


def _entry(**overrides: object) -> SemanticEntry:
    base: dict[str, object] = {
        "entry_id": "safari.shortcut.fullscreen",
        "app": "Safari",
        "key": "shortcut.fullscreen",
        "value": "Ctrl+Cmd+F",
        "kind": "shortcut",
        "tags": ("fullscreen", "view"),
    }
    base.update(overrides)
    return SemanticEntry.model_validate(base)


def _click_provider() -> Callable[[WorkingState], AgentTurn]:
    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click one",
                action=MouseClick(type="mouse_click", x=100, y=100),
            )
        return AgentTurn(
            thought="done",
            sub_goal="workflow complete",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    return provider


# --- Pure retrieval ----------------------------------------------------------


def test_search_entries_scores_by_value_and_tags() -> None:
    entries = (
        _entry(),
        _entry(entry_id="safari.preference.theme", key="preference.theme", value="dark mode", kind="preference", tags=("appearance",)),
        _entry(entry_id="chrome.shortcut.reload", app="Chrome", key="shortcut.reload", value="Cmd+R", kind="shortcut", tags=()),
    )
    # A query about fullscreen finds the shortcut via key/tags.
    assert [e.entry_id for e in search_entries(entries, "fullscreen")] == [
        "safari.shortcut.fullscreen"
    ]
    # A query about appearance finds the preference via its value.
    assert [e.entry_id for e in search_entries(entries, "dark")] == [
        "safari.preference.theme"
    ]
    # Higher-scoring match (more tokens hit) ranks first.
    assert search_entries(entries, "fullscreen view")[0].entry_id == "safari.shortcut.fullscreen"


def test_search_entries_app_scope_and_empty_query() -> None:
    entries = (
        _entry(),
        _entry(entry_id="chrome.shortcut.reload", app="Chrome", key="shortcut.reload", value="Cmd+R", tags=()),
    )
    # App-scoped: only Safari entries.
    assert [e.entry_id for e in search_entries(entries, "", app="Safari")] == [
        "safari.shortcut.fullscreen"
    ]
    # Empty query, no app scope: everything, sorted by id.
    assert [e.entry_id for e in search_entries(entries, "")] == [
        "chrome.shortcut.reload",
        "safari.shortcut.fullscreen",
    ]
    # App mismatch filters out even matching content.
    assert search_entries(entries, "Cmd+R", app="Safari") == ()


# --- Store shell -------------------------------------------------------------


def test_semantic_store_round_trip_and_collision(tmp_path) -> None:
    store = SemanticStore(tmp_path / "semantic")
    store.put(_entry())
    assert store.get("safari.shortcut.fullscreen").value == "Ctrl+Cmd+F"
    with pytest.raises(FileExistsError):
        store.put(_entry())  # never clobber knowledge silently
    store.delete("safari.shortcut.fullscreen")
    with pytest.raises(KeyError):
        store.get("safari.shortcut.fullscreen")
    assert store.entries() == ()


def test_semantic_store_search_round_trip(tmp_path) -> None:
    store = SemanticStore(tmp_path / "semantic")
    store.put(_entry())
    assert [e.entry_id for e in store.search("fullscreen", app="Safari")] == [
        "safari.shortcut.fullscreen"
    ]
    assert store.search("nothing-here") == ()


# --- Knowledge into the OODA working context ---------------------------------


def test_working_state_preserves_knowledge_through_decide_step() -> None:
    from computeruse.orchestrator.loop import decide_step
    from computeruse.orchestrator.schemas import MouseMove

    start = WorkingState(
        goal="x",
        knowledge=("[Safari] shortcut.fullscreen: Ctrl+Cmd+F",),
    )
    outcome = decide_step(
        start,
        AgentTurn(
            thought="",
            sub_goal="",
            action=MouseMove(type="mouse_move", x=1, y=1),
        ),
    )
    assert outcome.state.knowledge == start.knowledge


def test_runner_seeds_knowledge_into_provider_context() -> None:
    seen: list[tuple[str, ...]] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state.knowledge)
        return AgentTurn(
            thought="done",
            sub_goal="",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    runner = OodaRunner(provider=provider, execute_physical=lambda _a: None, knowledge=("k1", "k2"))
    runner.run(goal="x")
    assert seen and seen[0] == ("k1", "k2")


def test_agent_retrieves_app_knowledge_for_provider(tmp_path) -> None:
    """The semantic store's Safari knowledge reaches the provider verbatim."""
    SemanticStore(tmp_path / "store" / "semantic").put(_entry())
    seen: list[tuple[str, ...]] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state.knowledge)
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click one",
                action=MouseClick(type="mouse_click", x=100, y=100),
            )
        return AgentTurn(
            thought="done",
            sub_goal="workflow complete",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    config = AgentConfig(
        goal="enter fullscreen",
        app="Safari",
        provider=provider,
        socket_path=str(SOCKET_PATH),
        store_dir=tmp_path / "store",
        autonomy_level=AutonomyLevel.GUARDED,
        enable_visual_verification=False,
        max_steps=5,
        **SIMULATED_SETTLE,
    )
    result = Agent(config).run()
    assert seen and "[Safari] shortcut.fullscreen: Ctrl+Cmd+F" in seen[0]
    assert result.knowledge == seen[0]
