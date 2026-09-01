"""Smoke tests for Phase 3: Hierarchical Goal Planner and Session Checkpointing."""

from __future__ import annotations

from pathlib import Path

from computeruse.orchestrator.planner import (
    SessionCheckpoint,
    advance_plan,
    decompose_goal,
    plan_summary_for_prompt,
)


def test_decompose_complex_goal() -> None:
    """A multi-step goal with 'and' and 'then' connectors is decomposed into ordered sub-goals."""
    goal = "Open Google Chrome and search for latest AI news then click the first article"
    plan = decompose_goal(goal, app="Google Chrome")

    assert len(plan.sub_goals) == 3
    assert plan.sub_goals[0].description == "Open Google Chrome"
    assert plan.sub_goals[0].status == "in_progress"
    assert plan.sub_goals[1].description == "search for latest AI news"
    assert plan.sub_goals[1].status == "pending"
    assert plan.sub_goals[2].description == "click the first article"
    assert plan.sub_goals[2].status == "pending"

    # Verify formatted prompt summary
    prompt_str = plan_summary_for_prompt(plan)
    assert "0/3 sub-goals completed" in prompt_str
    assert "[1] Open Google Chrome" in prompt_str


def test_advance_plan_lifecycle() -> None:
    """Advancing a plan transitions in_progress to completed and activates the next sub-goal."""
    plan = decompose_goal("Open Safari and go to github.com")
    assert plan.sub_goals[0].status == "in_progress"
    assert plan.sub_goals[1].status == "pending"

    # Step 1 succeeds
    plan = advance_plan(plan, success=True)
    assert plan.sub_goals[0].status == "completed"
    assert plan.sub_goals[1].status == "in_progress"
    assert plan.is_completed is False

    # Step 2 succeeds
    plan = advance_plan(plan, success=True)
    assert plan.sub_goals[1].status == "completed"
    assert plan.is_completed is True


def test_session_checkpoint_save_and_load(tmp_path: Path) -> None:
    """A session checkpoint serializes the active plan and can be restored identically."""
    plan = decompose_goal("Open Terminal and run cargo test")
    checkpoint = SessionCheckpoint(
        session_id="session-123",
        plan=plan,
        completed_steps_count=2,
    )
    saved_path = checkpoint.save(tmp_path / "sessions")
    assert saved_path.is_file()

    loaded = SessionCheckpoint.load(saved_path)
    assert loaded.session_id == "session-123"
    assert loaded.plan.goal == "Open Terminal and run cargo test"
    assert len(loaded.plan.sub_goals) == 2
    assert loaded.completed_steps_count == 2
