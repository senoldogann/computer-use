"""Ranked-memory contract for self-directed autonomous work."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from computeruse.autonomous import propose_goal
from computeruse.memory.episodic import EpisodicStore, episode_from_trace
from computeruse.orchestrator.report import UsageRecord
from computeruse.orchestrator.schemas import MouseClick
from computeruse.scheduler import GoalProposal
from computeruse.skills.registry import SkillRegistry
from computeruse.skills.schemas import SkillDefinition


def _skill(skill_id: str, uses: int, wins: int) -> SkillDefinition:
    return SkillDefinition(
        skill_id=skill_id,
        description=f"do the thing for {skill_id}",
        app="Google Chrome",
        steps=("a step",),
        signature=skill_id,
        uses=uses,
        wins=wins,
    )


def _failed_episode(description: str):
    return episode_from_trace(
        app="Finder",
        description=description,
        steps=(MouseClick(type="mouse_click", x=1, y=1),),
        step_descriptions=("click",),
        outcome="failure",
        retrospective="lost",
    )


def _usage(run_id: str, goal: str, cost: float) -> UsageRecord:
    return UsageRecord(
        run_id=run_id,
        goal=goal,
        app="Finder",
        outcome="failure",
        steps=1,
        total_tokens=100,
        cost_usd=cost,
        elapsed_seconds=1.0,
        recorded_at=datetime(2026, 9, 7, tzinfo=UTC),
    )


def _propose(
    skills: SkillRegistry,
    episodes: EpisodicStore,
    *,
    usage: tuple[UsageRecord, ...] = (),
    exclude: frozenset[str] = frozenset(),
) -> GoalProposal | None:
    # Callable[..., Any] deliberately lets this test express the next API
    # contract while the RED implementation still has the old rng parameter.
    dynamic: Callable[..., Any] = propose_goal
    result = dynamic(skills, episodes, usage=usage, exclude=exclude)
    assert result is None or isinstance(result, GoalProposal)
    return result


def test_ranked_memory_never_invents_work_from_empty_stores(tmp_path: Path) -> None:
    assert (
        _propose(
            SkillRegistry(tmp_path / "skills"),
            EpisodicStore(tmp_path / "episodes"),
        )
        is None
    )


def test_skill_repair_outranks_failed_episode_and_validation(tmp_path: Path) -> None:
    skills = SkillRegistry(tmp_path / "skills")
    skills.save(_skill("chrome.broken", uses=3, wins=0))
    skills.save(_skill("chrome.unproven", uses=1, wins=1))
    episodes = EpisodicStore(tmp_path / "episodes")
    episodes.record(_failed_episode("retry the failed export"))

    proposal = _propose(skills, episodes)

    assert proposal is not None
    assert proposal.source_type == "skill_repair"
    assert proposal.source_id == "chrome.broken"


def test_failed_episode_outranks_skill_validation(tmp_path: Path) -> None:
    skills = SkillRegistry(tmp_path / "skills")
    skills.save(_skill("chrome.unproven", uses=1, wins=1))
    episodes = EpisodicStore(tmp_path / "episodes")
    failed = _failed_episode("retry the failed export")
    episodes.record(failed)

    proposal = _propose(skills, episodes)

    assert proposal is not None
    assert proposal.source_type == "episode_retry"
    assert proposal.source_id == failed.episode_id


def test_same_state_selects_same_highest_utility_candidate(tmp_path: Path) -> None:
    skills = SkillRegistry(tmp_path / "skills")
    skills.save(_skill("chrome.three", uses=3, wins=0))
    skills.save(_skill("chrome.five", uses=5, wins=0))
    episodes = EpisodicStore(tmp_path / "episodes")

    first = _propose(skills, episodes)
    second = _propose(skills, episodes)

    assert first is not None and second is not None
    assert first.source_id == "chrome.five"
    assert second == first


def test_same_source_uses_historical_cost_to_rank_candidates(tmp_path: Path) -> None:
    skills = SkillRegistry(tmp_path / "skills")
    episodes = EpisodicStore(tmp_path / "episodes")
    cheap = _failed_episode("cheap retry")
    expensive = _failed_episode("expensive retry")
    episodes.record(cheap)
    episodes.record(expensive)
    usage = (
        _usage("cheap-run", cheap.description, 0.10),
        _usage("expensive-run", expensive.description, 2.00),
    )

    proposal = _propose(skills, episodes, usage=usage)

    assert proposal is not None
    assert proposal.source_type == "episode_retry"
    assert proposal.source_id == cheap.episode_id
    assert proposal.expected_cost == 0.10


def test_exclusion_happens_before_ranking_and_keeps_valid_peer(tmp_path: Path) -> None:
    skills = SkillRegistry(tmp_path / "skills")
    skills.save(_skill("chrome.blocked", uses=5, wins=0))
    skills.save(_skill("chrome.next", uses=3, wins=0))
    episodes = EpisodicStore(tmp_path / "episodes")

    proposal = _propose(
        skills,
        episodes,
        exclude=frozenset({"do the thing for chrome.blocked"}),
    )

    assert proposal is not None
    assert proposal.source_type == "skill_repair"
    assert proposal.source_id == "chrome.next"
