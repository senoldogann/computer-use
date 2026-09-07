"""Episodic and preference memory schemas (Law 4).

Every agent run that reaches a terminal outcome leaves an :class:`Episode`
record — the trajectory plus whether it succeeded and why, in a structured,
disk-round-trippable form. The same module owns the durable typed user-model
record used by Law 4.2; storage/reconciliation live in their focused modules.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from computeruse.orchestrator.schemas import Action

EpisodeOutcome = Literal["success", "failure"]
PreferenceDomain = Literal[
    "ui",
    "workflow",
    "communication",
    "app",
    "scheduling",
    "formatting",
    "general",
]
PreferenceSource = Literal["explicit", "repeated_behavior", "successful_correction"]


class Episode(BaseModel):
    """One complete run's episodic trace (frozen at record time)."""

    episode_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    app: str
    description: str
    steps: tuple[Action, ...] = Field(description="Ordered actions actually executed.")
    step_descriptions: tuple[str, ...] = Field(
        default=(), description="Per-step intent descriptions."
    )
    # Accessibility identity of the element each step acted on, aligned with
    # ``steps``. Part of the flow signature, so it has to be persisted: an
    # episode that dropped it would recompute a *different* signature than the
    # one it was stored with, and de-dup joins the two tiers on that value.
    # Defaulted for the same reason ``run_id`` is — records written before this
    # field existed must keep validating, and they hash exactly as they did.
    step_targets: tuple[str, ...] = Field(
        default=(), description="Per-step accessibility target identities."
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
    # Whether this run ended on a finish the completion auditor never
    # accepted (force-accepted by the stalemate guard). Optional so old
    # records keep validating — absence then means "not forced", which is
    # exactly what those runs were. The distill gate reads this, never the
    # binary outcome alone.
    forced_completion: bool = False
    recorded_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="UTC timestamp, frozen at creation.",
    )


class PreferenceRecord(BaseModel):
    """One durable, provenance-bearing user preference (Law 4.2)."""

    preference_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    domain: PreferenceDomain
    key: str = Field(min_length=1)
    value: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_count: int = Field(ge=1)
    source: PreferenceSource
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    first_seen: datetime
    last_seen: datetime
    supersedes: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
