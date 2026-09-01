"""Smoke tests for Phase 2: Parametric skills and auto semantic memory learning."""

from __future__ import annotations

from pathlib import Path

from computeruse.memory.semantic import (
    SemanticStore,
    extract_facts_from_run,
)
from computeruse.orchestrator.schemas import MouseClick, TypeText
from computeruse.skills.schemas import (
    SkillDefinition,
    instantiate_skill,
    summary_of,
)


def test_instantiate_parametric_skill() -> None:
    """A skill definition with {{slots}} is instantiated with concrete values."""
    template = SkillDefinition(
        skill_id="chrome.search.template",
        description="search {{query}} on {{engine}}",
        app="Google Chrome",
        tags=("search", "web"),
        parameters=("query", "engine"),
        steps=(
            "Click search bar -> mouse_click:x=200,y=100",
            "Type search term -> type_text:text='{{query}}',wpm=60",
            "Submit query -> press_hotkey:key='return'",
        ),
        signature="template12345678",
    )
    summary = summary_of(template)
    assert summary.parameters == ("query", "engine")

    instantiated = instantiate_skill(
        template, {"query": "Freebuff AI", "engine": "Google"}
    )
    assert instantiated.description == "search Freebuff AI on Google"
    assert instantiated.steps[1] == "Type search term -> type_text:text='Freebuff AI',wpm=60"


def test_extract_facts_from_run_and_upsert(tmp_path: Path) -> None:
    """A successful run derives stable semantic patterns and persists them."""
    steps = (
        MouseClick(type="mouse_click", x=300, y=80),
        TypeText(type="type_text", text="github.com", wpm=60),
    )
    sub_goals = ("Click address bar", "Type URL")

    facts = extract_facts_from_run(
        app="Google Chrome",
        steps=steps,
        step_descriptions=sub_goals,
    )
    assert len(facts) == 2
    assert facts[0].app == "Google Chrome"
    assert "click address bar" in facts[0].key.lower()
    assert "300" in facts[0].value

    store = SemanticStore(tmp_path / "semantic")
    for fact in facts:
        store.upsert(fact)

    # Search for known patterns
    search_results = store.search("address bar", app="Google Chrome")
    assert len(search_results) >= 1
    assert search_results[0].key == "Click address bar"
