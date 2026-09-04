"""Episodic memory schemas (Law 4.1).

Every agent run that reaches a terminal outcome leaves an :class:`Episode`
record — the trajectory plus whether it succeeded and why, in a structured,
disk-round-trippable form. This is the raw material both the distiller (Law 3)
and future hindsight analysis consume; it is intentionally independent of the
implementation details of *how* those downstream consumers work.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from computeruse.orchestrator.schemas import Action

EpisodeOutcome = Literal["success", "failure"]


class Episode(BaseModel):
    """One complete run's episodic trace (frozen at record time)."""

    episode_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    app: str
    description: str
    steps: tuple[Action, ...] = Field(description="Ordered actions actually executed.")
    step_descriptions: tuple[str, ...] = Field(
        default=(), description="Per-step intent descriptions."
    )
    outcome: EpisodeOutcome
    # A short retrospective: why it succeeded or what went wrong (Law 4.1's
    # "failure retrospectives"). Optional for compactness on success paths.
    retrospective: str | None = Field(default=None)
    # Identity of the *flow*, shared with the distiller so memory feeds skill
    # distillation. Recomputable from (app, steps); stored to keep indexing O(1).
    signature: str
    # Which run produced this episode, matching UsageRecord.run_id so a score
    # can join what happened to what it cost. Optional on purpose: episodes
    # recorded before this field existed are still on disk, and they must keep
    # validating — they join to no usage instead of breaking the read path.
    run_id: str | None = Field(default=None)
    recorded_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="UTC timestamp, frozen at creation.",
    )