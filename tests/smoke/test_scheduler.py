"""Pure contract tests for ranked autonomous work proposals."""

from __future__ import annotations

import pytest
from computeruse.scheduler import GoalProposal, make_proposal, rank_proposals


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
