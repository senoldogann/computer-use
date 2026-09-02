"""Hierarchical goal planning, plan advancement, and session checkpointing.

Two properties matter more than the decomposition itself:

* **The plan never invents work.** Splitting on the conjunction "and" turned
  "search for latest AI news" into two sub-goals the user never asked for, and
  the executor then chased — and reported failure on — half a phrase. Only an
  explicit sequence marker splits a goal now.
* **The plan is strictly monotonic.** A failed sub-goal used to leave the plan
  with no ``in_progress`` head, so every later advance was a no-op while
  ``current_sub_goal`` still reported work pending: the run could not terminate
  until the step budget ran out. Every advance now either moves the head
  forward or reports the plan complete.
"""

from __future__ import annotations

from pathlib import Path

from computeruse.orchestrator.planner import (
    SessionCheckpoint,
    advance_plan,
    decompose_goal,
    plan_summary_for_prompt,
    split_sequential_goal,
)


def test_decompose_splits_on_explicit_sequence_markers() -> None:
    """'then' names a real sequence, so the plan follows it."""
    goal = "Open Google Chrome then search for latest AI news then read the first article"
    plan = decompose_goal(goal, app="Google Chrome", knowledge=())

    assert [sub_goal.description for sub_goal in plan.sub_goals] == [
        "Open Google Chrome",
        "search for latest AI news",
        "read the first article",
    ]
    assert plan.sub_goals[0].status == "in_progress"
    assert all(sub_goal.status == "pending" for sub_goal in plan.sub_goals[1:])
    assert all(sub_goal.target_app == "Google Chrome" for sub_goal in plan.sub_goals)

    prompt_str = plan_summary_for_prompt(plan)
    assert "0/3 sub-goals completed" in prompt_str
    assert "[1] Open Google Chrome" in prompt_str


def test_decompose_does_not_split_on_a_conjunction() -> None:
    """"and" joins one intent; splitting it invents a sub-goal nobody asked for."""
    plan = decompose_goal(
        "search for cats and dogs on google", app="Google Chrome", knowledge=()
    )
    assert len(plan.sub_goals) == 1
    assert plan.sub_goals[0].description == "search for cats and dogs on google"


def test_split_rejects_fragments_too_small_to_execute() -> None:
    """A split that yields a one-word fragment is not a plan; keep the goal whole."""
    assert split_sequential_goal("open chrome then go") == (
        "open chrome then go",
    )
    assert split_sequential_goal("open chrome then go to github") == (
        "open chrome",
        "go to github",
    )


def test_advance_plan_lifecycle() -> None:
    """Success moves the head forward until the plan is complete."""
    plan = decompose_goal("Open Safari then go to github.com", app="Safari", knowledge=())
    assert plan.sub_goals[0].status == "in_progress"
    assert plan.sub_goals[1].status == "pending"

    plan = advance_plan(plan, success=True, error=None)
    assert plan.sub_goals[0].status == "completed"
    assert plan.sub_goals[1].status == "in_progress"
    assert plan.is_completed is False

    plan = advance_plan(plan, success=True, error=None)
    assert plan.sub_goals[1].status == "completed"
    assert plan.is_completed is True
    assert plan.current_sub_goal is None


def test_failed_sub_goal_still_advances_the_plan() -> None:
    """A failure must not strand the plan without an in-progress head.

    Previously the failed sub-goal was marked ``failed`` and no successor was
    promoted. ``current_sub_goal`` then reported the next *pending* item while
    ``advance_plan`` found nothing ``in_progress`` to advance — so the loop
    treated every subsequent finish as "sub-goal done, keep going" and burned
    its entire step budget without ever terminating.
    """
    plan = decompose_goal(
        "Open Safari then go to github.com", app="Safari", knowledge=()
    )
    plan = advance_plan(plan, success=False, error="the app would not launch")
    assert plan.sub_goals[0].status == "failed"
    assert plan.sub_goals[0].error == "the app would not launch"
    assert plan.sub_goals[1].status == "in_progress"

    plan = advance_plan(plan, success=True, error=None)
    assert plan.current_sub_goal is None, "the plan must reach a terminal state"


def test_advance_is_idempotent_on_a_finished_plan() -> None:
    """A completed plan stays completed; advancing it cannot resurrect work."""
    plan = decompose_goal("do the thing", app=None, knowledge=())
    plan = advance_plan(plan, success=True, error=None)
    assert plan.current_sub_goal is None
    again = advance_plan(plan, success=True, error=None)
    assert again.current_sub_goal is None
    assert [sub_goal.status for sub_goal in again.sub_goals] == ["completed"]


def test_session_checkpoint_save_and_load(tmp_path: Path) -> None:
    """A session checkpoint serializes the active plan and restores it identically."""
    plan = decompose_goal(
        "Open Terminal then run cargo test", app="Terminal", knowledge=()
    )
    checkpoint = SessionCheckpoint(
        session_id="session-123",
        plan=plan,
        completed_steps_count=2,
    )
    saved_path = checkpoint.save(tmp_path / "sessions")
    assert saved_path.is_file()

    loaded = SessionCheckpoint.load(saved_path)
    assert loaded.session_id == "session-123"
    assert loaded.plan.goal == "Open Terminal then run cargo test"
    assert len(loaded.plan.sub_goals) == 2
    assert loaded.completed_steps_count == 2
