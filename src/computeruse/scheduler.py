"""Pure ranking contract for self-directed work proposals.

The autonomous runner may learn about work from several places, but the thing
it executes must always say where it came from. This module owns that small,
pure contract: provenance, a bounded utility score, and deterministic ordering.
It performs no I/O and grants no permission to execute anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

ProposalSource = Literal[
    "operator_inbox",
    "mission_resume",
    "skill_repair",
    "episode_retry",
    "skill_validation",
]

SOURCE_BASE_UTILITY: Final[dict[ProposalSource, float]] = {
    "operator_inbox": 500.0,
    "mission_resume": 400.0,
    "skill_repair": 300.0,
    "episode_retry": 200.0,
    "skill_validation": 100.0,
}


@dataclass(frozen=True)
class GoalProposal:
    """One grounded unit of autonomous work and its auditable provenance."""

    goal: str
    app: str | None
    source_type: ProposalSource
    source_id: str
    utility_score: float
    confidence: float
    expected_cost: float | None
    reason: str


def proposal_score(
    source_type: ProposalSource, confidence: float, expected_cost: float | None
) -> float:
    """Score a proposal without allowing adjustments to invert source priority."""
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be between 0 and 1, got {confidence}")
    confidence_bonus = confidence * 10.0
    cost_penalty = min(max(expected_cost or 0.0, 0.0), 9.0)
    return SOURCE_BASE_UTILITY[source_type] + confidence_bonus - cost_penalty


def make_proposal(
    *,
    goal: str,
    app: str | None,
    source_type: ProposalSource,
    source_id: str,
    confidence: float,
    expected_cost: float | None,
    reason: str,
) -> GoalProposal:
    """Validate proposal identity and derive its utility score."""
    if not goal.strip():
        raise ValueError("goal must not be empty")
    if not source_id.strip():
        raise ValueError("source_id must not be empty")
    score = proposal_score(source_type, confidence, expected_cost)
    return GoalProposal(
        goal=goal,
        app=app,
        source_type=source_type,
        source_id=source_id,
        utility_score=score,
        confidence=confidence,
        expected_cost=expected_cost,
        reason=reason,
    )


def rank_proposals(
    proposals: tuple[GoalProposal, ...],
) -> tuple[GoalProposal, ...]:
    """Rank identically stored state identically, with provenance as tie-break."""
    return tuple(
        sorted(
            proposals,
            key=lambda proposal: (
                -proposal.utility_score,
                proposal.source_type,
                proposal.source_id,
                proposal.goal,
            ),
        )
    )
