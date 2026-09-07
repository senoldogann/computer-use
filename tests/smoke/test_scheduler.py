"""Pure contract tests for ranked autonomous work proposals."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from computeruse.orchestrator.report import UsageRecord
from computeruse.scheduler import (
    GoalProposal,
    estimate_expected_cost,
    make_proposal,
    rank_proposals,
)


def _usage(run_id: str, goal: str, cost: float) -> UsageRecord:
    return UsageRecord(
        run_id=run_id,
        goal=goal,
        app="Finder",
        outcome="success",
        steps=1,
        total_tokens=100,
        cost_usd=cost,
        elapsed_seconds=1.0,
        recorded_at=datetime(2026, 9, 7, tzinfo=UTC),
    )


def test_goal_proposal_carries_auditable_provenance() -> None:
    proposal = GoalProposal(
        goal="retry failed export",
        app="Finder",
        source_type="episode_retry",
        source_id="episode-42",
        utility_score=207.5,
        confidence=0.75,
        expected_cost=0.12,
        reason="episode episode-42 failed",
    )

    assert proposal.goal == "retry failed export"
    assert proposal.app == "Finder"
    assert proposal.source_type == "episode_retry"
    assert proposal.source_id == "episode-42"
    assert proposal.utility_score == 207.5
    assert proposal.confidence == 0.75
    assert proposal.expected_cost == 0.12
    assert proposal.reason == "episode episode-42 failed"


def test_legacy_inbox_constructor_recovers_typed_provenance() -> None:
    proposal = GoalProposal(
        goal="operator task",
        app="Finder",
        reason="task file 'night shift.md' claimed from watched folder /tmp/inbox",
    )

    assert proposal.source_type == "operator_inbox"
    assert proposal.source_id == "night shift.md"
    assert proposal.confidence == 1.0
    assert proposal.expected_cost is None
    assert proposal.utility_score >= 500.0


def test_legacy_mission_constructor_recovers_typed_provenance() -> None:
    proposal = GoalProposal(
        goal="finish export",
        app="Finder",
        reason=(
            "mission mission-42 was started and never finished "
            "(2 attempt(s) so far)"
        ),
    )

    assert proposal.source_type == "mission_resume"
    assert proposal.source_id == "mission-42"
    assert proposal.confidence == pytest.approx(0.6)
    assert proposal.expected_cost is None
    assert 400.0 <= proposal.utility_score < 500.0


def test_unknown_legacy_constructor_fails_closed() -> None:
    with pytest.raises(ValueError, match="explicit provenance"):
        GoalProposal(goal="x", app=None, reason="some unstructured source")


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_make_proposal_rejects_confidence_outside_unit_interval(
    confidence: float,
) -> None:
    with pytest.raises(ValueError, match="confidence"):
        make_proposal(
            goal="x",
            app=None,
            source_type="episode_retry",
            source_id="episode-1",
            confidence=confidence,
            expected_cost=None,
            reason="fixture",
        )


@pytest.mark.parametrize(
    ("goal", "source_id", "message"),
    [
        ("", "episode-1", "goal"),
        ("x", "", "source_id"),
    ],
)
def test_make_proposal_rejects_empty_identity(
    goal: str, source_id: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        make_proposal(
            goal=goal,
            app=None,
            source_type="episode_retry",
            source_id=source_id,
            confidence=0.75,
            expected_cost=None,
            reason="fixture",
        )


def test_source_priority_band_cannot_be_overturned_by_cost_or_confidence() -> None:
    expensive_operator = make_proposal(
        goal="operator task",
        app=None,
        source_type="operator_inbox",
        source_id="task.md",
        confidence=0.0,
        expected_cost=999.0,
        reason="operator supplied it",
    )
    cheap_mission = make_proposal(
        goal="resume mission",
        app=None,
        source_type="mission_resume",
        source_id="mission-1",
        confidence=1.0,
        expected_cost=0.0,
        reason="unfinished mission",
    )

    ranked = rank_proposals((cheap_mission, expensive_operator))

    assert ranked[0] is expensive_operator


def test_equal_scores_have_a_stable_provenance_tie_break() -> None:
    later = make_proposal(
        goal="same class later",
        app=None,
        source_type="episode_retry",
        source_id="episode-b",
        confidence=0.75,
        expected_cost=None,
        reason="fixture",
    )
    earlier = make_proposal(
        goal="same class earlier",
        app=None,
        source_type="episode_retry",
        source_id="episode-a",
        confidence=0.75,
        expected_cost=None,
        reason="fixture",
    )

    first = rank_proposals((later, earlier))
    second = rank_proposals((earlier, later))

    assert [proposal.source_id for proposal in first] == ["episode-a", "episode-b"]
    assert second == first


def test_expected_cost_averages_exact_normalized_goal_history() -> None:
    records = (
        _usage("run-a", "export report", 0.20),
        _usage("run-b", " export   report ", 0.40),
        _usage("run-c", "different goal", 9.0),
    )

    assert estimate_expected_cost("export report", records) == pytest.approx(0.30)


def test_expected_cost_is_unknown_without_matching_history() -> None:
    records = (_usage("run-a", "different goal", 0.25),)

    assert estimate_expected_cost("never seen", records) is None


def test_zero_dollar_history_is_valid_cost_data() -> None:
    records = (_usage("run-a", "local task", 0.0),)

    assert estimate_expected_cost("local task", records) == 0.0
