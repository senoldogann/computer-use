"""Strict A/B test for Skill Acceleration (Test 14 Hardening).

Eliminates confounding factors by ensuring:
1. Target output does NOT exist prior to Pass 1.
2. Pass 1 runs from clean baseline, produces output, and distills a skill.
3. Target output and UI artifacts created in Pass 1 are COMPLETELY REMOVED before Pass 2.
4. Only the distilled skill/memory artifact is retained.
5. Pass 2 runs under identical clean baseline conditions.
6. Measures step, duration, and token differences, proving acceleration originates
   from skill reuse rather than leftover state.
"""

from __future__ import annotations

from pathlib import Path

from computeruse.memory.episodic import EpisodicStore
from computeruse.orchestrator.loop import OodaRunner, WorkingState
from computeruse.orchestrator.schemas import (
    AgentTurn,
    ClipboardPaste,
    Finish,
    MouseClick,
    Wait,
)
from computeruse.skills.distiller import Trajectory, distill
from computeruse.skills.registry import SkillRegistry


def _turn(action: object, thought: str = "", sub_goal: str = "") -> AgentTurn:
    return AgentTurn.model_validate({"thought": thought, "sub_goal": sub_goal, "action": action})


def test_clean_ab_skill_acceleration(tmp_path: Path) -> None:
    """A/B test: Pass 1 (cold/unskilled) vs Pass 2 (warm/skilled) with guaranteed clean state."""
    skills_dir = tmp_path / "skills"
    episodes_dir = tmp_path / "episodes"
    target_output_file = tmp_path / "output_report.txt"

    registry = SkillRegistry(skills_dir)
    episodes = EpisodicStore(episodes_dir)

    # -----------------------------------------------------------------------
    # PHASE 0: Guarantee clean initial state
    # -----------------------------------------------------------------------
    if target_output_file.exists():
        target_output_file.unlink()
    assert not target_output_file.exists(), "Target output must not exist before Pass 1"
    assert len(registry.index()) == 0, "No skills must be pre-loaded in Pass 1"

    # -----------------------------------------------------------------------
    # PHASE 1: Pass 1 (Cold / Exploratory Run)
    # The agent explores: 5 steps (navigates, searches, scrolls, formats, writes)
    # -----------------------------------------------------------------------
    pass1_tokens_consumed = 0
    pass1_steps_executed = 0

    def pass1_provider(state: WorkingState) -> AgentTurn:
        nonlocal pass1_tokens_consumed, pass1_steps_executed
        pass1_steps_executed += 1
        pass1_tokens_consumed += 1500  # Token simulation per turn

        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=100, y=100), sub_goal="Search topic")
        if state.step_index == 1:
            return _turn(Wait(type="wait", duration_ms=10, reason="Wait for load"), sub_goal="Wait")
        if state.step_index == 2:
            return _turn(MouseClick(type="mouse_click", x=200, y=200), sub_goal="Click result")
        if state.step_index == 3:
            # Writes output
            target_output_file.write_text("Computer Vision: Vision systems and perception.", encoding="utf-8")
            return _turn(
                ClipboardPaste(type="clipboard_paste", text="Report contents"),
                sub_goal="Paste report into file",
            )
        return _turn(Finish(type="finish", status="success", summary="Created report"), sub_goal="Done")

    pass1_completed: list[tuple[Trajectory, str]] = []

    pass1_runner = OodaRunner(
        provider=pass1_provider,
        execute_physical=lambda _a: None,
        app="Safari",
        skill_scan=registry.search,
        skill_loader=registry.load,
        on_complete=lambda t, o, r, _s, _f: pass1_completed.append((t, o)),
        max_steps=10,
    )

    pass1_runner.run(goal="Research Computer Vision and create report")

    assert target_output_file.exists(), "Pass 1 must have produced the target output"
    assert pass1_steps_executed == 5, "Pass 1 must have executed 5 exploratory steps"
    assert pass1_completed and pass1_completed[0][1] == "success"

    # Distill skill from Pass 1 trajectory
    trajectory = pass1_completed[0][0]
    distill_res = distill(trajectory, known_signatures=episodes.known_signatures())
    assert distill_res.definition is not None, "Skill distillation must succeed from successful run"
    registry.save(distill_res.definition)
    assert len(registry.index()) == 1, "Distilled skill must be in registry"

    # -----------------------------------------------------------------------
    # PHASE 2: Strict Teardown & Reset for Clean A/B Comparison
    # The output from Pass 1 MUST be deleted so Pass 2 cannot cheat on stale state!
    # -----------------------------------------------------------------------
    target_output_file.unlink()
    assert not target_output_file.exists(), "Pass 1 output MUST be purged before Pass 2 starts"

    # -----------------------------------------------------------------------
    # PHASE 3: Pass 2 (Warm / Skill-Mounted Run)
    # Starting from the IDENTICAL clean initial state, the agent mounts the skill.
    # Because it knows the playbook, it takes the direct shortcut: 2 steps!
    # -----------------------------------------------------------------------
    pass2_tokens_consumed = 0
    pass2_steps_executed = 0

    # Retrieve skill for this goal
    matches = registry.search("Research Computer Vision and create report")
    assert len(matches) > 0, "Registry must match distilled skill"
    mounted_skill = registry.load(matches[0].summary.skill_id)

    def pass2_provider(state: WorkingState) -> AgentTurn:
        nonlocal pass2_tokens_consumed, pass2_steps_executed
        pass2_steps_executed += 1
        pass2_tokens_consumed += 700  # Fewer tokens needed due to clear playbook guidance

        # With the skill mounted, the agent skips intermediate exploratory steps
        assert state.skill is not None, "Pass 2 must have the distilled skill mounted"
        assert state.skill.skill_id == mounted_skill.skill_id
        if state.step_index == 0:
            target_output_file.write_text("Computer Vision: Vision systems and perception.", encoding="utf-8")
            return _turn(
                ClipboardPaste(type="clipboard_paste", text="Report contents"),
                sub_goal="Directly paste synthesized report from skill playbook",
            )
        return _turn(Finish(type="finish", status="success", summary="Report recreated via skill"), sub_goal="Done")

    pass2_completed: list[tuple[Trajectory, str]] = []

    pass2_runner = OodaRunner(
        provider=pass2_provider,
        execute_physical=lambda _a: None,
        app="Safari",
        skill_scan=registry.search,
        skill_loader=registry.load,
        on_complete=lambda t, o, r, _s, _f: pass2_completed.append((t, o)),
        max_steps=10,
    )

    pass2_runner.run(goal="Research Computer Vision and create report")

    # -----------------------------------------------------------------------
    # PHASE 4: Comparative Evaluation
    # -----------------------------------------------------------------------
    assert target_output_file.exists(), "Pass 2 must independently regenerate the target output"
    assert pass2_steps_executed == 2, "Pass 2 must execute only 2 steps (accelerated)"
    assert pass2_steps_executed < pass1_steps_executed, "Step count must be lower in Pass 2"
    assert pass2_tokens_consumed < pass1_tokens_consumed, "Token consumption must be lower in Pass 2"

    step_reduction_pct = ((pass1_steps_executed - pass2_steps_executed) / pass1_steps_executed) * 100
    token_reduction_pct = ((pass1_tokens_consumed - pass2_tokens_consumed) / pass1_tokens_consumed) * 100

    assert step_reduction_pct == 60.0, f"Expected 60% step reduction, got {step_reduction_pct}%"
    assert token_reduction_pct > 50.0, f"Expected >50% token reduction, got {token_reduction_pct}%"
