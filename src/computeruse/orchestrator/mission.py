"""Work that survives the run that started it (Law 4 continuity).

The planner already decomposes a goal into ordered sub-goals and marks each one
done as the loop clears it, and ``SessionCheckpoint`` already writes that
progress to disk on every transition. The CLI's own help said those checkpoints
existed "for resumability". Nothing ever read one: ``SessionCheckpoint.load``
was called only from tests, so a run killed at sub-goal four of six left a
perfect record of its progress that no later run consulted, and the next
attempt started again from sub-goal one.

A :class:`Mission` is the durable half of that story — the work item, as
opposed to the run that happens to be executing it. It carries the plan, so it
carries the progress; it carries a status that distinguishes the two ways work
stops without finishing:

* **failed** — the agent tried and could not. Retrying costs a run and might
  work, so it is bounded by an attempt ceiling.
* **blocked** — the agent could not proceed *without a human*, having parked an
  approval request. Retrying is pointless until someone answers, and burning
  attempts on it would turn a question into a failure.

That distinction is the reason this module exists rather than a boolean. An
unattended session that treats "needs your approval" as "failed" throws the
work away and proposes it again tomorrow, walking into the same wall with no
memory of having been there.

Per Law 6 every transition is a pure function over pure data; :class:`MissionStore`
is the connector.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, Field

from computeruse.atomic import write_atomic
from computeruse.orchestrator.planner import (
    GoalPlan,
    goal_from_sub_goals,
    outstanding_sub_goals,
)
from computeruse.slug import ascii_slug

LOGGER: Final = logging.getLogger(__name__)

MissionStatus = Literal["pending", "in_progress", "blocked", "completed", "failed"]

#: How many times a mission may be attempted before it stops being proposed.
#: Bounded for the same reason the recovery ladder is: a goal that has failed
#: three separate runs is not going to succeed on the fourth by luck, and an
#: unattended session with an unbounded retry has one goal, forever.
DEFAULT_MAX_ATTEMPTS: Final[int] = 3

#: How much of the goal survives into a mission id.
GOAL_SLUG_MAX_CHARS: Final[int] = 40


class Mission(BaseModel):
    """One durable unit of work, and where it got to (pure data)."""

    mission_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    goal: str = Field(min_length=1)
    app: str | None
    #: The plan as of the last transition. ``None`` for a mission created
    #: without planning enabled — it is still a resumable work item, it just
    #: resumes at the whole goal rather than at a sub-goal.
    plan: GoalPlan | None
    status: MissionStatus
    #: Why a blocked mission is blocked, in a human's words.
    blocked_reason: str | None = None
    #: The parked approval request holding it up, when there is one.
    approval_id: str | None = None
    attempts: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime


def new_mission(
    *, goal: str, app: str | None, plan: GoalPlan | None, now: datetime
) -> Mission:
    """A mission that has not been attempted yet (pure)."""
    stamp = now.strftime("%Y%m%dt%H%M%S")
    label = ascii_slug(goal, max_chars=GOAL_SLUG_MAX_CHARS)
    return Mission(
        mission_id=f"{stamp}-{label}" if label else stamp,
        goal=goal,
        app=app,
        plan=plan,
        status="pending",
        attempts=0,
        created_at=now,
        updated_at=now,
    )


def mission_started(mission: Mission, now: datetime) -> Mission:
    """Mark a mission as being worked on, spending one attempt (pure).

    The attempt is counted here rather than at the end because a run that never
    reports back — killed, crashed, power lost — must still cost one. Counting
    on completion would let a mission that hangs the machine be retried forever.
    """
    return mission.model_copy(
        update={
            "status": "in_progress",
            "attempts": mission.attempts + 1,
            "blocked_reason": None,
            "approval_id": None,
            "updated_at": now,
        }
    )


def mission_finished(
    mission: Mission, *, plan: GoalPlan | None, succeeded: bool, now: datetime
) -> Mission:
    """Close a mission the agent finished or genuinely failed (pure)."""
    return mission.model_copy(
        update={
            "status": "completed" if succeeded else "failed",
            "plan": plan if plan is not None else mission.plan,
            "updated_at": now,
        }
    )


def mission_blocked(
    mission: Mission,
    *,
    plan: GoalPlan | None,
    reason: str,
    approval_id: str | None,
    now: datetime,
) -> Mission:
    """Park a mission that cannot proceed without a human (pure).

    The attempt spent getting here is *refunded*: waiting on a person is not a
    failed attempt, and charging for it would retire a mission after three
    questions nobody had answered yet.
    """
    return mission.model_copy(
        update={
            "status": "blocked",
            "plan": plan if plan is not None else mission.plan,
            "blocked_reason": reason,
            "approval_id": approval_id,
            "attempts": max(0, mission.attempts - 1),
            "updated_at": now,
        }
    )


def mission_unblocked(mission: Mission, now: datetime) -> Mission:
    """Return a blocked mission to the queue once its question is answered (pure)."""
    return mission.model_copy(
        update={
            "status": "pending",
            "blocked_reason": None,
            "approval_id": None,
            "updated_at": now,
        }
    )


def resumable(
    missions: tuple[Mission, ...], *, max_attempts: int
) -> tuple[Mission, ...]:
    """Missions a session may pick up, oldest first (pure).

    ``blocked`` is deliberately absent: a mission waiting on a human is not
    work the agent can advance, and offering it again would produce a second
    identical approval request. It re-enters through :func:`mission_unblocked`
    when the request is answered.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")
    open_work = [
        mission
        for mission in missions
        if mission.status in ("pending", "in_progress", "failed")
        and mission.attempts < max_attempts
    ]
    return tuple(sorted(open_work, key=lambda mission: mission.created_at))


def blocked_missions(missions: tuple[Mission, ...]) -> tuple[Mission, ...]:
    """Missions parked on a human, oldest first (pure)."""
    parked = [mission for mission in missions if mission.status == "blocked"]
    return tuple(sorted(parked, key=lambda mission: mission.created_at))


def remaining_goal(mission: Mission) -> str:
    """What is actually left to do (pure).

    This is the point of resuming rather than restarting. A mission whose plan
    has three of five sub-goals done should be handed the remaining two, not
    the original goal — re-running completed steps is not merely wasteful on a
    physical host, it is *destructive*: "send the email, then archive the
    thread" resumed from the top sends the email twice.

    Falls back to the whole goal when there is no plan or nothing is finished,
    which is the honest answer in both cases.
    """
    if mission.plan is None:
        return mission.goal
    outstanding = outstanding_sub_goals(mission.plan)
    if not outstanding or len(outstanding) == len(mission.plan.sub_goals):
        # Finished, or not started. Either way the mission's own goal is the
        # honest answer, and it keeps the user's wording — commas and all —
        # instead of handing back a reconstruction of it.
        return mission.goal
    return goal_from_sub_goals(outstanding)


class MissionStore:
    """Missions on disk, one JSON file each (Law 6.1 connector)."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def directory(self) -> Path:
        return self._directory

    def save(self, mission: Mission) -> Path:
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self._directory / f"{mission.mission_id}.json"
        write_atomic(target, mission.model_dump_json(indent=2) + "\n")
        return target

    def missions(self) -> tuple[Mission, ...]:
        """Every mission on disk; unreadable files are skipped with a warning.

        Same reasoning as the approval queue: one corrupt record must not hide
        every other piece of parked work behind it.
        """
        if not self._directory.is_dir():
            return ()
        found: list[Mission] = []
        for path in sorted(self._directory.glob("*.json")):
            try:
                found.append(
                    Mission.model_validate(json.loads(path.read_text(encoding="utf-8")))
                )
            except (OSError, ValueError) as exc:
                LOGGER.warning("unreadable mission %s: %s", path, exc)
        return tuple(found)

    def load(self, mission_id: str) -> Mission:
        path = self._directory / f"{mission_id}.json"
        if not path.is_file():
            raise KeyError(f"no mission {mission_id!r} in {self._directory}")
        return Mission.model_validate(json.loads(path.read_text(encoding="utf-8")))
