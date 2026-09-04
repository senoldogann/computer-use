"""Tests for Law 3: skill distillation and two-stage retrieval.

``distiller`` and ``search`` are pure and run offline. ``SkillRegistry`` is a
connector, so its disk round-trip is tested against pytest's ``tmp_path`` to
avoid touching the real store.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from computeruse.orchestrator.schemas import (
    LoadSkill,
    MouseClick,
    PressHotkey,
    TypeText,
    Wait,
)
from computeruse.skills.distiller import (
    TAG_LIMIT,
    Trajectory,
    derive_tags,
    distill,
    signature_of,
)
from computeruse.skills.registry import (
    SkillRegistry,
    is_demoted,
    search,
    track_record_bonus,
)
from computeruse.skills.schemas import SkillDefinition, SkillSummary, summary_of


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
    # A well-formed id that simply is not in the store. It used to read
    # "nope:nope", which now fails the id check first — a different error for a
    # different reason, and not what this test is about.
    with pytest.raises(KeyError):
        registry.load("nope.nope")


def test_registry_refuses_an_id_that_could_climb_out_of_the_store(
    tmp_path: Path,
) -> None:
    """The store re-checks the id because it is where a string becomes a path."""
    registry = SkillRegistry(tmp_path)
    for climbing in ("../../etc/passwd", "/etc/passwd", "..", "a/b"):
        with pytest.raises(ValueError, match="not a valid store id"):
            registry.load(climbing)


def test_load_skill_action_carries_the_store_id_constraint() -> None:
    """The id a *model* emits is constrained like the ones on disk.

    ``LoadSkill`` was the one place the pattern was missing, so a skill id
    assembled from screen-derived text reached a path join unvalidated.
    """
    with pytest.raises(ValidationError):
        LoadSkill(type="load_skill", skill_id="../../etc/passwd")
    assert LoadSkill(type="load_skill", skill_id="safari.export-pdf").skill_id


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


# --- retrieval: a skill nobody can find is a skill nobody learns from --------


def _summary(skill_id: str, description: str, app: str, tags: tuple[str, ...] = ()) -> SkillSummary:
    return SkillSummary(skill_id=skill_id, description=description, app=app, tags=tags)


def test_search_matches_the_description() -> None:
    """The description is the only field guaranteed to have content.

    Measured on a real store: 12 skills indexed, every realistic query returned
    nothing, because distillation left tags empty and a run's query is its goal
    text — which rarely repeats the application's name verbatim.
    """
    summaries = [
        _summary("chrome.a", "Open Hacker News and read the top story comments", "Google Chrome"),
        _summary("finder.b", "Open Finder and navigate to Downloads", "Finder"),
    ]
    hits = search(summaries, "open hacker news comments")
    assert [h.summary.skill_id for h in hits] == ["chrome.a"]


def test_app_match_outranks_a_coincidental_word_match() -> None:
    """Weights rank, they do not gate: the same-app skill sorts first."""
    summaries = [
        _summary("finder.b", "Open the downloads folder", "Finder"),
        _summary("chrome.a", "Open the downloads page", "Chrome"),
    ]
    hits = search(summaries, "chrome open downloads")
    assert hits[0].summary.skill_id == "chrome.a"
    assert len(hits) == 2  # the other still matches, it just ranks lower


def test_derived_tags_describe_the_run_and_drop_generic_words() -> None:
    """Tags come from the sub-goals — what the agent actually had to do."""
    trajectory = Trajectory(
        app="Google Chrome",
        description="open the comments",
        steps=(MouseClick(type="mouse_click", x=1, y=1),),
        step_descriptions=(
            "Navigate Chrome to news.ycombinator.com.",
            "Click the comments link for the top story.",
        ),
    )
    tags = derive_tags(trajectory)
    assert "ycombinator" in tags and "comments" in tags
    # Words common to every workflow distinguish nothing and are excluded.
    assert "click" not in tags and "navigate" not in tags and "the" not in tags
    # Deterministic and bounded, so two runs of one flow still de-duplicate.
    assert tags == derive_tags(trajectory)
    assert len(tags) <= TAG_LIMIT


def test_a_distilled_skill_carries_no_coordinates() -> None:
    """A stored coordinate is wrong by the time anyone replays it.

    The window at (404, 227) yesterday is a different link today. Measured:
    replaying a skill that named one took 18 steps where the cold run took 10.
    """
    trajectory = Trajectory(
        app="Google Chrome",
        description="open the top story comments",
        steps=(
            MouseClick(type="mouse_click", x=404, y=227),
            TypeText(type="type_text", text="hello", wpm=40),
        ),
        step_descriptions=("Click the comments link.", "Type a reply."),
    )
    result = distill(trajectory, known_signatures=())
    assert result.definition is not None
    rendered = " ".join(result.definition.steps)
    assert "404" not in rendered and "x=" not in rendered and "y=" not in rendered
    # What transfers is still there: the intent and the non-positional params.
    assert "Click the comments link." in rendered
    assert "hello" in rendered


# --- reinforcement: a skill is a claim, and claims get tested ----------------


def _scored(skill_id: str, uses: int, wins: int) -> SkillSummary:
    return SkillSummary(
        skill_id=skill_id,
        description="open hacker news comments",
        app="Google Chrome",
        uses=uses,
        wins=wins,
    )


def test_a_skill_that_only_ever_fails_stops_being_offered() -> None:
    """Distillation used to be the end of a skill's story.

    A recipe that led three runs astray was handed to a fourth with exactly the
    confidence of one that had worked every time. Withheld rather than ranked
    last: an actively harmful recipe offered as a fallback is still offered.
    """
    summaries = [_scored("bad", uses=3, wins=0), _scored("good", uses=3, wins=2)]
    found = [hit.summary.skill_id for hit in search(summaries, "hacker news comments")]
    assert found == ["good"]


def test_one_win_protects_a_skill_from_demotion() -> None:
    """Later failures are then far likelier to be about the screen it met than
    the route it describes."""
    assert is_demoted(_scored("proven", uses=9, wins=1)) is False
    assert is_demoted(_scored("unproven", uses=2, wins=0)) is False  # not yet
    assert is_demoted(_scored("failing", uses=3, wins=0)) is True


def test_an_untried_skill_is_neither_rewarded_nor_punished() -> None:
    """A new skill has said nothing yet; it should not be ranked as though it
    had failed, or nothing new would ever get tried."""
    assert track_record_bonus(_scored("new", uses=0, wins=0)) == 0
    assert track_record_bonus(_scored("proven", uses=1, wins=1)) == 1
    assert track_record_bonus(_scored("missing", uses=1, wins=0)) == -1


def test_a_proven_skill_wins_a_tie_but_does_not_beat_a_better_match() -> None:
    """The store must not ossify around whatever happened to be tried first."""
    proven_but_unrelated = SkillSummary(
        skill_id="proven", description="open the downloads folder", app="Finder",
        uses=5, wins=5,
    )
    untried_but_matching = SkillSummary(
        skill_id="matching", description="open hacker news comments", app="Chrome",
    )
    ranked = [h.summary.skill_id for h in search(
        [proven_but_unrelated, untried_but_matching], "hacker news comments"
    )]
    assert ranked[0] == "matching"


def test_outcomes_are_written_back_to_the_store(tmp_path) -> None:
    """Without this the counters stay at zero and the ranking is dead code."""
    registry = SkillRegistry(tmp_path)
    registry.save(_definition(skill_id="chrome.x"))
    registry.record_outcome("chrome.x", succeeded=True)
    registry.record_outcome("chrome.x", succeeded=False)
    stored = registry.load("chrome.x")
    assert (stored.uses, stored.wins) == (2, 1)


def test_recording_against_a_missing_skill_is_survivable(tmp_path) -> None:
    """The run has already finished; failing its bookkeeping would turn a
    completed task into an error."""
    SkillRegistry(tmp_path).record_outcome("nope.gone", succeeded=True)
