"""Pure ranking contract for self-directed work proposals.

The autonomous runner may learn about work from several places, but the thing
it executes must always say where it came from. This module owns that small,
pure contract: provenance, a bounded utility score, and deterministic ordering.
It performs no I/O and grants no permission to execute anything.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Final, Literal, overload

from computeruse.orchestrator.report import UsageRecord

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

_MISSION_ATTEMPTS: Final = re.compile(r"\((\d+) attempt\(s\) so far\)$")


def _legacy_cli_provenance(reason: str) -> tuple[ProposalSource, str, float]:
    """Recover provenance from the two pre-scheduler CLI proposal forms.

    This is deliberately narrow. The CLI historically instantiated
    ``GoalProposal`` directly for exactly two sources and already carried the
    source identity in stable, machine-authored reason strings. Supporting
    those two forms lets the scheduler land without rewriting the large CLI
    entrypoint in the same PR. Any other provenance-free construction fails
    closed instead of being mislabeled.
    """
    task_prefix = "task file "
    task_suffix = " claimed from watched folder "
    if reason.startswith(task_prefix) and task_suffix in reason:
        rendered_name = reason[len(task_prefix) :].split(task_suffix, 1)[0]
        try:
            source_name = ast.literal_eval(rendered_name)
        except (SyntaxError, ValueError) as exc:
            raise ValueError("cannot recover inbox source_id from legacy reason") from exc
        if not isinstance(source_name, str) or not source_name.strip():
            raise ValueError("cannot recover inbox source_id from legacy reason")
        return "operator_inbox", source_name, 1.0

    mission_prefix = "mission "
    mission_suffix = " was started and never finished "
    if reason.startswith(mission_prefix) and mission_suffix in reason:
        mission_id = reason[len(mission_prefix) :].split(mission_suffix, 1)[0].strip()
        if not mission_id:
            raise ValueError("cannot recover mission source_id from legacy reason")
        attempts_match = _MISSION_ATTEMPTS.search(reason)
        attempts = int(attempts_match.group(1)) if attempts_match is not None else 0
        confidence = max(0.5, 1.0 - 0.2 * attempts)
        return "mission_resume", mission_id, confidence

    raise ValueError(
        "GoalProposal requires explicit provenance; unsupported legacy construction"
    )


@dataclass(frozen=True, init=False)
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

    @overload
    def __init__(
        self,
        *,
        goal: str,
        app: str | None,
        source_type: ProposalSource,
        source_id: str,
        utility_score: float,
        confidence: float,
        expected_cost: float | None,
        reason: str,
    ) -> None: ...

    @overload
    def __init__(self, *, goal: str, app: str | None, reason: str) -> None: ...

    def __init__(
        self,
        *,
        goal: str,
        app: str | None,
        reason: str,
        source_type: ProposalSource | None = None,
        source_id: str | None = None,
        utility_score: float | None = None,
        confidence: float | None = None,
        expected_cost: float | None = None,
    ) -> None:
        """Build an explicit proposal or bridge the two legacy CLI call sites."""
        if not goal.strip():
            raise ValueError("goal must not be empty")
        if source_type is None:
            if source_id is not None or utility_score is not None or confidence is not None:
                raise ValueError("partial proposal provenance is not allowed")
            source_type, source_id, confidence = _legacy_cli_provenance(reason)
            utility_score = proposal_score(source_type, confidence, expected_cost)
        else:
            if source_id is None or not source_id.strip():
                raise ValueError("source_id must not be empty")
            if confidence is None:
                raise ValueError("confidence is required with explicit provenance")
            calculated = proposal_score(source_type, confidence, expected_cost)
            if utility_score is None:
                utility_score = calculated

        assert source_id is not None
        assert utility_score is not None
        assert confidence is not None
        object.__setattr__(self, "goal", goal)
        object.__setattr__(self, "app", app)
        object.__setattr__(self, "source_type", source_type)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "utility_score", utility_score)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "expected_cost", expected_cost)
        object.__setattr__(self, "reason", reason)


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


def _normalized_goal(goal: str) -> str:
    """Collapse insignificant whitespace for exact historical goal matching."""
    return " ".join(goal.split())


def estimate_expected_cost(
    goal: str, usage: tuple[UsageRecord, ...]
) -> float | None:
    """Mean recorded dollar cost for the same normalized goal, when known."""
    target = _normalized_goal(goal)
    matches = [
        record.cost_usd
        for record in usage
        if _normalized_goal(record.goal) == target
    ]
    if not matches:
        return None
    return sum(matches) / len(matches)
