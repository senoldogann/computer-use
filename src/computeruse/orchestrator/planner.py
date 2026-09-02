"""Hierarchical Goal Planner (Strategic Planner & Multi-Step Decomposition).

This module introduces the high-level "strategist" layer above the OODA "executor":
1. Decomposes complex user goals into structured, verifiable sub-goals with success criteria.
2. Tracks sub-goal progress across the multi-step execution lifecycle.
3. Dynamically re-plans when a sub-goal fails or requires an alternate route.
4. Manages session checkpointing for resumability across interruptions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, Field

SubGoalStatus = Literal["pending", "in_progress", "completed", "failed"]


class PlannedSubGoal(BaseModel):
    """One strategic sub-goal with concrete success criteria."""

    index: int
    description: str = Field(min_length=1, description="What needs to be done.")
    success_criteria: str = Field(description="Verifiable condition for success.")
    target_app: str | None = Field(default=None, description="App to focus for this sub-goal.")
    status: SubGoalStatus = "pending"
    error: str | None = None


class GoalPlan(BaseModel):
    """The hierarchical decomposition of a high-level goal."""

    plan_id: str
    goal: str
    sub_goals: tuple[PlannedSubGoal, ...]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def current_sub_goal(self) -> PlannedSubGoal | None:
        for sg in self.sub_goals:
            if sg.status in ("pending", "in_progress"):
                return sg
        return None

    @property
    def is_completed(self) -> bool:
        return all(sg.status == "completed" for sg in self.sub_goals)

    @property
    def progress_summary(self) -> str:
        completed = sum(1 for sg in self.sub_goals if sg.status == "completed")
        total = len(self.sub_goals)
        return f"{completed}/{total} sub-goals completed"


def plan_summary_for_prompt(plan: GoalPlan) -> str:
    """Format the strategic roadmap for prompt context injection (pure)."""
    lines: list[str] = [f"Strategic Plan ({plan.progress_summary}):"]
    for sg in plan.sub_goals:
        status_icon = "✓" if sg.status == "completed" else ("➤" if sg.status == "in_progress" else "○")
        lines.append(f"  {status_icon} [{sg.index + 1}] {sg.description} (Criteria: {sg.success_criteria})")
    return "\n".join(lines)


# Explicit sequence markers. Only these split a goal into ordered sub-goals.
# Conjunctions ("and", "ve") are deliberately absent: "search for cats and dogs"
# is one step, and splitting it produced two nonsense sub-goals ("search for
# cats" / "dogs") that the executor then chased separately. A planner that
# invents work is worse than one that plans a single step.
SEQUENCE_MARKERS: Final[tuple[str, ...]] = (
    " then ",
    " sonra ",
    " ardından ",
    " after that ",
    " afterwards ",
    " danach ",
    " ensuite ",
    " luego ",
)

# Shortest sub-goal worth executing on its own, in words. A fragment below
# this ("dogs", "it") carries no actionable intent — splitting there yields a
# sub-goal no executor could satisfy or verify.
MIN_SUB_GOAL_WORDS: Final[int] = 2


def split_sequential_goal(goal: str) -> tuple[str, ...]:
    """Split a goal on explicit sequence markers only (pure).

    Returns a single-element tuple when the goal names no sequence, which is
    the common case and the safe default: the executor decomposes tactically
    from what it sees, and a bad strategic split actively misleads it. A split
    is accepted only when *every* resulting part is substantial enough to be a
    step in its own right.
    """
    cleaned = goal.strip()
    if not cleaned:
        return ()
    lowered = cleaned.lower()
    marker = next((m for m in SEQUENCE_MARKERS if m in lowered), None)
    if marker is None:
        return (cleaned,)
    parts: list[str] = []
    remaining = cleaned
    while marker is not None:
        index = remaining.lower().index(marker)
        parts.append(remaining[:index].strip().strip(","))
        remaining = remaining[index + len(marker) :]
        lowered_rest = remaining.lower()
        marker = next((m for m in SEQUENCE_MARKERS if m in lowered_rest), None)
    parts.append(remaining.strip().strip(","))
    parts = [part for part in parts if part]
    if len(parts) < 2 or any(len(part.split()) < MIN_SUB_GOAL_WORDS for part in parts):
        return (cleaned,)
    return tuple(parts)


def decompose_goal(
    goal: str,
    *,
    app: str | None,
    knowledge: tuple[str, ...],
) -> GoalPlan:
    """Decompose a high-level user goal into ordered sub-goals (pure).

    Deliberately conservative. A strategic plan is only useful when it is
    *right*: a wrong decomposition does not merely waste a step, it redirects
    the executor toward work the user never asked for and then reports the
    invented step as failed. So the plan splits only on explicit sequence
    words, and otherwise carries the goal as a single sub-goal whose success
    criteria restate the user's own wording — the executor's own perception,
    not a template, decides what "done" looks like.

    ``knowledge`` is accepted so the signature is stable for a model-backed
    decomposer; the deterministic implementation does not consult it.
    """
    del knowledge  # Reserved for a model-backed decomposer; unused here.
    parts = split_sequential_goal(goal)
    if not parts:
        raise ValueError("goal must be a non-empty string")

    sub_goals = tuple(
        PlannedSubGoal(
            index=index,
            description=part,
            success_criteria=(
                f"The screen shows observable evidence that '{part}' is done."
            ),
            target_app=app,
            status="in_progress" if index == 0 else "pending",
        )
        for index, part in enumerate(parts)
    )
    clean_goal = goal.strip()
    slug = "".join(ch if ch.isalnum() else "-" for ch in clean_goal[:20].lower()).strip("-")
    plan_id = f"plan-{slug}-{int(datetime.now(UTC).timestamp())}"
    return GoalPlan(plan_id=plan_id, goal=clean_goal, sub_goals=sub_goals)


def _with_status(
    sub_goal: PlannedSubGoal,
    status: SubGoalStatus,
    error: str | None,
) -> PlannedSubGoal:
    """Copy a sub-goal with a new status (pure)."""
    return sub_goal.model_copy(update={"status": status, "error": error})


def advance_plan(plan: GoalPlan, *, success: bool, error: str | None) -> GoalPlan:
    """Close the current sub-goal and open the next one (pure).

    The next sub-goal is promoted to ``in_progress`` on *both* outcomes. When
    a failure left the plan with a ``failed`` head and no ``in_progress``
    successor, the very next call found nothing to advance and returned the
    plan unchanged — while ``current_sub_goal`` still reported a pending item,
    so the loop treated every subsequent ``finish`` as "sub-goal done, keep
    going" and could not terminate until the step budget ran out. Promoting on
    both paths is what makes the plan strictly monotonic: every call either
    moves the head forward or reports the plan complete.
    """
    sub_goals = list(plan.sub_goals)
    current_index = next(
        (i for i, sub_goal in enumerate(sub_goals) if sub_goal.status == "in_progress"),
        None,
    )
    if current_index is None:
        # No head to close. Promote the first pending sub-goal so a plan that
        # lost its head (e.g. after a failure) still advances rather than
        # deadlocking; with nothing pending the plan is genuinely finished.
        pending_index = next(
            (i for i, sub_goal in enumerate(sub_goals) if sub_goal.status == "pending"),
            None,
        )
        if pending_index is None:
            return plan
        sub_goals[pending_index] = _with_status(sub_goals[pending_index], "in_progress", None)
        return GoalPlan(
            plan_id=plan.plan_id,
            goal=plan.goal,
            sub_goals=tuple(sub_goals),
            created_at=plan.created_at,
            updated_at=datetime.now(UTC),
        )

    sub_goals[current_index] = _with_status(
        sub_goals[current_index],
        "completed" if success else "failed",
        None if success else error,
    )
    if current_index + 1 < len(sub_goals):
        sub_goals[current_index + 1] = _with_status(
            sub_goals[current_index + 1], "in_progress", None
        )
    return GoalPlan(
        plan_id=plan.plan_id,
        goal=plan.goal,
        sub_goals=tuple(sub_goals),
        created_at=plan.created_at,
        updated_at=datetime.now(UTC),
    )


class SessionCheckpoint(BaseModel):
    """Persistent session state for crash recovery & resumability."""

    session_id: str
    plan: GoalPlan
    completed_steps_count: int
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{self.session_id}.json"
        target.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: Path) -> SessionCheckpoint:
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))
