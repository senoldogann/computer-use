"""Tests for Law 3: skill distillation and two-stage retrieval.

``distiller`` and ``search`` are pure and run offline. ``SkillRegistry`` is a
connector, so its disk round-trip is tested against pytest's ``tmp_path`` to
avoid touching the real store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from computeruse.orchestrator.schemas import MouseClick, PressHotkey, TypeText, Wait
from computeruse.skills.distiller import Trajectory, distill, signature_of
from computeruse.skills.registry import SkillRegistry, search
from computeruse.skills.schemas import SkillDefinition, summary_of


def _definition(**overrides: object) -> SkillDefinition:
    base: dict[str, object] = {
        "skill_id": "example.abc123",
        "description": "export a report in Numbers",
        "app": "Numbers",
        "tags": ("export", "csv"),
        "steps": ("click Export", "choose CSV"),
        "signature": "deadbeef",
    }
    base.update(overrides)
    return SkillDefinition.model_validate(base)


def test_distill_too_short_is_rejected() -> None:
    traj = Trajectory(app="Numbers", description="x", steps=(MouseClick(type="mouse_click", x=1, y=1),))
    result = distill(traj, known_signatures=set())
    assert result.kind == "too_short"


def test_distill_produces_definition_with_stable_signature() -> None:
    steps = (
        MouseClick(type="mouse_click", x=10, y=20),
        PressHotkey(type="press_hotkey", key="s", modifiers=["command"]),
    )
    traj = Trajectory(app="Numbers", description="save", steps=steps, tags=("save",))
    result = distill(traj, known_signatures=set())

    assert result.kind == "skill"
    assert result.definition is not None
    assert result.signature == signature_of(traj)


def test_distill_signature_ignores_pacing_fields() -> None:
    """L14: two runs of the same flow with different wait/typing pacing share
    one signature — pacing is not workflow meaning, and hashing it would break
    de-dup between two runs of the same skill."""
    slow = (
        Wait(type="wait", duration_ms=1000, reason="settle"),
        TypeText(type="type_text", text="hello", wpm=30),
    )
    fast = (
        Wait(type="wait", duration_ms=50, reason="settle"),
        TypeText(type="type_text", text="hello", wpm=90),
    )
    sig_slow = signature_of(Trajectory(app="Chrome", description="x", steps=slow))
    sig_fast = signature_of(Trajectory(app="Chrome", description="x", steps=fast))
    assert sig_slow == sig_fast


def test_distill_deduplicates_identical_flow() -> None:
    steps = (
        MouseClick(type="mouse_click", x=5, y=5),
        PressHotkey(type="press_hotkey", key="s", modifiers=["command"]),
    )
    traj_a = Trajectory(app="Numbers", description="a", steps=steps)
    traj_b = Trajectory(app="Numbers", description="b", steps=steps)

    first = distill(traj_a, known_signatures=set())
    assert first.kind == "skill"
    # Second attempt across same store (same signatures) must not duplicate.
    second = distill(traj_b, known_signatures={first.signature or ""})
    assert second.kind == "duplicate"


def test_distill_distinguishes_clicks_with_different_intents() -> None:
    steps_a = (
        MouseClick(type="mouse_click", x=10, y=20),
        MouseClick(type="mouse_click", x=30, y=40),
    )
    steps_b = (
        MouseClick(type="mouse_click", x=100, y=200),
        MouseClick(type="mouse_click", x=300, y=400),
    )
    traj_a = Trajectory(
        app="Chrome",
        description="flow a",
        steps=steps_a,
        step_descriptions=("click new tab", "click omnibox"),
    )
    traj_b = Trajectory(
        app="Chrome",
        description="flow b",
        steps=steps_b,
        step_descriptions=("click download", "click confirm"),
    )

    first = distill(traj_a, known_signatures=set())
    assert first.kind == "skill"
    second = distill(traj_b, known_signatures={first.signature or ""})
    assert second.kind == "skill"
    assert second.signature != first.signature


def test_registry_round_trip_on_disk(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path)
    definition = _definition()
    registry.save(definition)

    reloaded = registry.load(definition.skill_id)
    assert reloaded == definition

    index = registry.index()
    assert len(index) == 1
    assert index[0].description == definition.description
    assert index[0].tags == definition.tags  # tags survive into the summary


def test_registry_load_missing_raises(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path)
    with pytest.raises(KeyError):
        registry.load("nope:nope")


def test_search_ranks_app_and_tags() -> None:
    summaries = [
        summary_of(_definition(skill_id="a.x", app="Numbers", tags=("export", "csv"))),
        summary_of(_definition(skill_id="b.x", app="Browser", tags=("payments",))),
    ]
    matches = search(summaries, "numbers export")
    assert matches, "expected at least one match"
    top = matches[0]
    assert top.summary.app == "Numbers"
    # app token is worth more than a tag token, so the Numbers hit ranks first
    # regardless of id ordering.
    assert top.score > 0


def test_search_no_hit_returns_empty() -> None:
    summaries = [summary_of(_definition(skill_id="a.x", app="Numbers"))]
    assert search(summaries, "spreadsheet") == []
