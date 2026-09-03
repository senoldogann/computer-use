"""Mission continuity (AUT-01): work that survives the run that started it.

``SessionCheckpoint`` wrote a run's plan progress to disk on every sub-goal
transition, and the CLI's help said those files existed "for resumability".
``SessionCheckpoint.load`` was called only from tests, so a run killed at
sub-goal four of six left a perfect record nothing ever read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from computeruse.orchestrator.mission import (
    DEFAULT_MAX_ATTEMPTS,
    MissionStore,
    blocked_missions,
    mission_blocked,
    mission_finished,
    mission_started,
    mission_unblocked,
    new_mission,
    remaining_goal,
    resumable,
)
from computeruse.orchestrator.planner import GoalPlan, PlannedSubGoal

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _plan(*statuses: str) -> GoalPlan:
    """A plan whose sub-goals carry the given statuses, in order."""
    return GoalPlan(
        plan_id="plan-x",
        goal="draft the note, then send it, then archive the thread",
        sub_goals=tuple(
            PlannedSubGoal(
                index=index,
                description=description,
                success_criteria="observable",
                target_app="Mail",
                status=status,  # type: ignore[arg-type]
            )
            for index, (description, status) in enumerate(
                zip(("draft the note", "send it", "archive the thread"), statuses)
            )
        ),
    )


def _mission(goal: str = "do the thing") -> object:
    return new_mission(goal=goal, app="Mail", plan=None, now=NOW)


# --- the failed / blocked distinction ---------------------------------------


def test_a_blocked_mission_is_not_offered_again() -> None:
    """Offering it would raise a second identical approval request.

    A mission waiting on a person is not work the agent can advance, so it
    leaves the queue until the question is answered.
    """
    started = mission_started(_mission(), NOW)
    parked = mission_blocked(
        started, plan=None, reason="needs you", approval_id="req-1", now=NOW
    )
    assert resumable((parked,), max_attempts=DEFAULT_MAX_ATTEMPTS) == ()
    assert blocked_missions((parked,)) == (parked,)


def test_blocking_refunds_the_attempt() -> None:
    """Waiting on a person is not a failed attempt.

    Charging for it would retire a mission after three questions nobody had
    got around to answering.
    """
    started = mission_started(_mission(), NOW)
    assert started.attempts == 1
    parked = mission_blocked(
        started, plan=None, reason="needs you", approval_id="req-1", now=NOW
    )
    assert parked.attempts == 0


def test_answering_the_question_returns_it_to_the_queue() -> None:
    started = mission_started(_mission(), NOW)
    parked = mission_blocked(
        started, plan=None, reason="needs you", approval_id="req-1", now=NOW
    )
    freed = mission_unblocked(parked, NOW)
    assert freed.status == "pending"
    assert freed.approval_id is None
    assert resumable((freed,), max_attempts=DEFAULT_MAX_ATTEMPTS) == (freed,)


def test_a_failed_mission_is_retried_until_its_ceiling() -> None:
    """A goal that lost three separate runs will not win the fourth by luck."""
    mission = _mission()
    for _ in range(DEFAULT_MAX_ATTEMPTS):
        mission = mission_started(mission, NOW)
        mission = mission_finished(mission, plan=None, succeeded=False, now=NOW)
        # Still failed, so it stays a candidate right up to the ceiling.
    assert mission.attempts == DEFAULT_MAX_ATTEMPTS
    assert resumable((mission,), max_attempts=DEFAULT_MAX_ATTEMPTS) == ()


def test_a_completed_mission_is_never_offered_again() -> None:
    done = mission_finished(
        mission_started(_mission(), NOW), plan=None, succeeded=True, now=NOW
    )
    assert resumable((done,), max_attempts=DEFAULT_MAX_ATTEMPTS) == ()


def test_a_ceiling_below_one_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        resumable((), max_attempts=0)


def test_open_work_is_offered_oldest_first() -> None:
    first = new_mission(goal="a", app=None, plan=None, now=NOW)
    second = new_mission(goal="b", app=None, plan=None, now=NOW + timedelta(hours=1))
    assert [m.goal for m in resumable((second, first), max_attempts=3)] == ["a", "b"]


# --- resuming, not restarting -----------------------------------------------


def test_resuming_hands_over_only_what_is_left() -> None:
    """The reason resume beats restart, and it is not about speed.

    "draft the note, then send it, then archive the thread" resumed from the
    top sends the email twice. On a physical host a repeated step is not
    wasted work, it is the step happening again.
    """
    mission = new_mission(
        goal="draft the note, then send it, then archive the thread",
        app="Mail",
        plan=_plan("completed", "in_progress", "pending"),
        now=NOW,
    )
    assert remaining_goal(mission) == "send it, then archive the thread"


def test_a_mission_with_no_progress_resumes_at_the_whole_goal() -> None:
    mission = new_mission(
        goal="draft the note, then send it, then archive the thread",
        app="Mail",
        plan=_plan("in_progress", "pending", "pending"),
        now=NOW,
    )
    assert remaining_goal(mission) == mission.goal


def test_a_mission_with_no_plan_resumes_at_the_whole_goal() -> None:
    assert remaining_goal(_mission("tidy the exports")) == "tidy the exports"


def test_a_fully_completed_plan_falls_back_to_the_goal() -> None:
    """Nothing outstanding is not the same as nothing to say.

    Returning an empty string here would hand the runner a goal it cannot act
    on; the original goal is the honest answer, and the completion auditor is
    what stops it being redone.
    """
    mission = new_mission(
        goal="draft the note, then send it, then archive the thread",
        app="Mail",
        plan=_plan("completed", "completed", "completed"),
        now=NOW,
    )
    assert remaining_goal(mission) == mission.goal


# --- the store --------------------------------------------------------------


def test_missions_round_trip_on_disk(tmp_path: Path) -> None:
    store = MissionStore(tmp_path)
    mission = new_mission(
        goal="draft the note, then send it",
        app="Mail",
        plan=_plan("completed", "in_progress", "pending"),
        now=NOW,
    )
    store.save(mission)
    assert store.load(mission.mission_id) == mission
    assert store.missions() == (mission,)


def test_one_corrupt_mission_does_not_hide_the_others(tmp_path: Path) -> None:
    """These are the records a person consults when something has gone wrong."""
    store = MissionStore(tmp_path)
    good = new_mission(goal="a real one", app=None, plan=None, now=NOW)
    store.save(good)
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert store.missions() == (good,)


def test_loading_a_missing_mission_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        MissionStore(tmp_path).load("nope")


def test_a_turkish_goal_still_produces_a_valid_mission_id() -> None:
    """The store names a file after the goal, and ids are pattern-checked."""
    mission = new_mission(
        goal="Işık ayarlarını değiştir", app=None, plan=None, now=NOW
    )
    assert mission.mission_id.endswith("isik-ayarlarini-degistir")
