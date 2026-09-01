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
from typing import Literal

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


def decompose_goal(
    goal: str,
    *,
    app: str | None = None,
    knowledge: tuple[str, ...] = (),
) -> GoalPlan:
    """Decompose a high-level user goal into ordered sub-goals (pure).

    Analyzes multi-step phrasing (connectors like 'and', 'then', 'after', commas)
    to create a clean linear execution plan.
    """
    clean_goal = goal.strip()
    # Normalize step split markers
    normalized = clean_goal.replace(" ve ", " and ").replace(" sonra ", " then ").replace(" ardından ", " then ")
    
    # Split on sequence connectors
    parts: list[str] = []
    for chunk in normalized.split(" then "):
        for sub_chunk in chunk.split(" and "):
            trimmed = sub_chunk.strip().strip(",")
            if trimmed:
                parts.append(trimmed)

    if not parts:
        parts = [clean_goal]

    sub_goals: list[PlannedSubGoal] = []
    for i, part in enumerate(parts):
        # Infer target app or default
        target = app
        lower_part = part.lower()
        if "chrome" in lower_part:
            target = "Google Chrome"
        elif "safari" in lower_part:
            target = "Safari"
        elif "finder" in lower_part:
            target = "Finder"

        criteria = f"'{part}' completed visibly"
        if "open" in lower_part or "aç" in lower_part:
            criteria = f"Target application {target or 'window'} is active and focused"
        elif "search" in lower_part or "ara" in lower_part:
            criteria = "Search query entered and results are displayed"
        elif "click" in lower_part or "tıkla" in lower_part:
            criteria = "Target UI element clicked"

        sub_goals.append(
            PlannedSubGoal(
                index=i,
                description=part,
                success_criteria=criteria,
                target_app=target,
                status="in_progress" if i == 0 else "pending",
            )
        )

    slug = "".join(ch if ch.isalnum() else "-" for ch in clean_goal[:20].lower()).strip("-")
    plan_id = f"plan-{slug}-{int(datetime.now(UTC).timestamp())}"
    return GoalPlan(plan_id=plan_id, goal=clean_goal, sub_goals=tuple(sub_goals))


def advance_plan(plan: GoalPlan, *, success: bool, error: str | None = None) -> GoalPlan:
    """Advance the current sub-goal status and activate the next one (pure)."""
    sub_goals = list(plan.sub_goals)
    current_idx: int | None = None
    for i, sg in enumerate(sub_goals):
        if sg.status == "in_progress":
            current_idx = i
            break

    if current_idx is None:
        return plan

    if success:
        sub_goals[current_idx] = PlannedSubGoal(
            index=sub_goals[current_idx].index,
            description=sub_goals[current_idx].description,
            success_criteria=sub_goals[current_idx].success_criteria,
            target_app=sub_goals[current_idx].target_app,
            status="completed",
        )
        if current_idx + 1 < len(sub_goals):
            sub_goals[current_idx + 1] = PlannedSubGoal(
                index=sub_goals[current_idx + 1].index,
                description=sub_goals[current_idx + 1].description,
                success_criteria=sub_goals[current_idx + 1].success_criteria,
                target_app=sub_goals[current_idx + 1].target_app,
                status="in_progress",
            )
    else:
        sub_goals[current_idx] = PlannedSubGoal(
            index=sub_goals[current_idx].index,
            description=sub_goals[current_idx].description,
            success_criteria=sub_goals[current_idx].success_criteria,
            target_app=sub_goals[current_idx].target_app,
            status="failed",
            error=error,
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
