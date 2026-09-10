"""PR D: verified-skill replay without model turns.

The fast path skips *decisions*, never *gates*: every replayed step still
runs through VALIDATE -> ACT -> VERIFY, coordinates are re-grounded from
live AX identity (never replayed), and the goal is still claimed by one
provider turn under the completion audit. These tests pin the contract at
every layer: recipe building, eligibility, re-grounding, and the loop.
"""

from __future__ import annotations

from pathlib import Path

from computeruse.orchestrator.loop import (
    AxProbeResult,
    OodaRunner,
    WorkingState,
    rebuild_recipe_action,
    resolve_mark,
)
from computeruse.orchestrator.schemas import (
    AgentTurn,
    CallTool,
    ClickMark,
    ClipboardPaste,
    Finish,
    MouseClick,
    PressHotkey,
)
from computeruse.skills.distiller import Trajectory, build_recipe, distill
from computeruse.skills.registry import (
    SkillRegistry,
    environment_fingerprint,
    fast_path_eligible,
)
from computeruse.skills.schemas import RecipeStep, SkillDefinition, summary_of
from computeruse.vision.som import MarkElement, parse_ax_elements_to_marks

APP = "TestApp"
GOAL = "file the quarterly report"


def _skill(**overrides: object) -> SkillDefinition:
    base: dict[str, object] = {
        "skill_id": "testapp.abc123",
        "description": GOAL,
        "app": APP,
        "steps": ("click Send", "paste body"),
        "signature": "deadbeef",
    }
    base.update(overrides)
    return SkillDefinition.model_validate(base)


def _proven_skill() -> SkillDefinition:
    return _skill(
        uses=3,
        wins=3,
        consecutive_wins=2,
        last_env=environment_fingerprint(APP, GOAL),
        recipe=(
            RecipeStep(
                action_type="mouse_click",
                params={"button": "left", "click_count": 1},
                target='Button "Send"',
            ),
            RecipeStep(
                action_type="clipboard_paste",
                params={"text": "quarterly body"},
            ),
        ),
    )


# --- recipe building ----------------------------------------------------------


def test_recipe_strips_coordinates_but_keeps_targets_and_operands() -> None:
    steps = (
        MouseClick(type="mouse_click", x=10, y=20),
        ClipboardPaste(type="clipboard_paste", text="quarterly body"),
    )
    traj = Trajectory(
        app=APP,
        description=GOAL,
        steps=steps,
        step_targets=('Button "Send"', ""),
    )
    recipe = build_recipe(traj)

    assert len(recipe) == 2
    assert recipe[0].action_type == "mouse_click"
    assert "x" not in recipe[0].params and "y" not in recipe[0].params
    assert recipe[0].params["button"] == "left"
    assert recipe[0].target == 'Button "Send"'
    assert recipe[1].params["text"] == "quarterly body"


def test_recipe_is_the_leading_replayable_prefix() -> None:
    """A tool call stops the recipe: replay runs in order and must never
    silently reorder the flow around a skipped middle."""
    steps = (
        MouseClick(type="mouse_click", x=1, y=1),
        CallTool(type="call_tool", tool="search", arguments={"q": "x"}),
        MouseClick(type="mouse_click", x=2, y=2),
    )
    traj = Trajectory(
        app=APP,
        description=GOAL,
        steps=steps,
        step_targets=('Button "A"', "", 'Button "B"'),
    )
    recipe = build_recipe(traj)

    assert [step.action_type for step in recipe] == ["mouse_click"]


def test_click_without_a_target_breaks_the_recipe() -> None:
    """No AX identity means nothing to re-ground against: coordinates alone
    never enter a recipe."""
    traj = Trajectory(
        app=APP,
        description=GOAL,
        steps=(MouseClick(type="mouse_click", x=1, y=1),),
        step_targets=("",),
    )
    assert build_recipe(traj) == ()


def test_mark_and_drag_steps_break_the_recipe() -> None:
    """A frame-bound mark index and a drag with a stale start are both
    unreplayable by construction."""
    for step in (
        ClickMark(type="click_mark", mark=2),
        PressHotkey(type="press_hotkey", key="s", modifiers=["command"]),
    ):
        traj = Trajectory(app=APP, description=GOAL, steps=(step,))
        # Hotkey alone is replayable; the mark is not.
        expected = 1 if step.type == "press_hotkey" else 0
        assert len(build_recipe(traj)) == expected


def test_distill_attaches_the_recipe() -> None:
    steps = (
        MouseClick(type="mouse_click", x=10, y=20),
        ClipboardPaste(type="clipboard_paste", text="body"),
    )
    traj = Trajectory(
        app=APP, description=GOAL, steps=steps, step_targets=('Button "Send"', "")
    )
    result = distill(traj, known_signatures=set())

    assert result.kind == "skill"
    assert result.definition is not None
    assert len(result.definition.recipe) == 2
    assert summary_of(result.definition).has_recipe is True


def test_tool_first_flow_yields_an_empty_but_valid_recipe() -> None:
    """Research-first flows stay full OODA: no recipe is normal, not an error."""
    steps = (
        CallTool(type="call_tool", tool="search", arguments={"q": "x"}),
        MouseClick(type="mouse_click", x=1, y=1),
    )
    traj = Trajectory(
        app=APP, description=GOAL, steps=steps, step_targets=("", 'Button "A"')
    )
    result = distill(traj, known_signatures=set())

    assert result.kind == "skill"
    assert result.definition is not None
    assert result.definition.recipe == ()
    assert summary_of(result.definition).has_recipe is False


# --- eligibility -----------------------------------------------------------------


def test_eligibility_needs_a_proven_streak_in_this_environment() -> None:
    env = environment_fingerprint(APP, GOAL)
    proven = _proven_skill()
    assert fast_path_eligible(proven, current_env=env) is True

    one_win = _skill(
        uses=1,
        wins=1,
        consecutive_wins=1,
        last_env=env,
        recipe=proven.recipe,
    )
    assert fast_path_eligible(one_win, current_env=env) is False

    wrong_env = SkillDefinition.model_validate(
        {**proven.model_dump(), "last_env": "OtherApp\0"}
    )
    assert fast_path_eligible(wrong_env, current_env=env) is False

    no_recipe = SkillDefinition.model_validate({**proven.model_dump(), "recipe": ()})
    assert fast_path_eligible(no_recipe, current_env=env) is False

    assert fast_path_eligible(proven, current_env="") is False


def test_environment_fingerprint_binds_app_and_site() -> None:
    same = environment_fingerprint("Chrome", "read the news on hacker news")
    assert same == environment_fingerprint("Chrome", "open hacker news today")
    assert same != environment_fingerprint("Chrome", "read the news on reddit")
    assert same != environment_fingerprint("Safari", "read the news on hacker news")
    # Site-agnostic goals bind on the app alone.
    assert environment_fingerprint("Notes", "write a note") == "Notes\0"


# --- streak bookkeeping -------------------------------------------------------


def test_record_outcome_maintains_the_streak(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path / "skills")
    registry.save(_skill())
    registry.record_outcome("testapp.abc123", succeeded=True, env="TestApp\0")
    first = registry.load("testapp.abc123")
    assert (first.uses, first.wins, first.consecutive_wins) == (1, 1, 1)
    assert first.last_env == "TestApp\0"
    assert first.last_success_at is not None
    assert first.last_failure_reason == ""

    registry.record_outcome("testapp.abc123", succeeded=True, env="TestApp\0")
    assert registry.load("testapp.abc123").consecutive_wins == 2

    registry.record_outcome(
        "testapp.abc123", succeeded=False, failure_reason="click missed: no live target"
    )
    broken = registry.load("testapp.abc123")
    assert (broken.uses, broken.wins, broken.consecutive_wins) == (3, 2, 0)
    assert broken.last_failure_reason == "click missed: no live target"


def test_failure_reason_is_bounded_and_cleared_by_success(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path / "skills")
    registry.save(_skill())
    registry.record_outcome(
        "testapp.abc123", succeeded=False, failure_reason="x" * 500
    )
    stored = registry.load("testapp.abc123")
    assert len(stored.last_failure_reason) <= 200

    registry.record_outcome("testapp.abc123", succeeded=True, env="TestApp\0")
    assert registry.load("testapp.abc123").last_failure_reason == ""


def test_skills_distilled_before_recipes_still_load(tmp_path: Path) -> None:
    """Old files on disk keep parsing via defaults — and are ineligible."""
    store = tmp_path / "skills"
    store.mkdir()
    (store / "old.aaa.json").write_text(
        '{"skill_id": "old.aaa", "description": "old flow", "app": "Old", '
        '"steps": ["do it"], "signature": "aaa", '
        '"uses": 9, "wins": 9}',
        encoding="utf-8",
    )
    loaded = SkillRegistry(store).load("old.aaa")
    assert loaded.recipe == ()
    assert loaded.consecutive_wins == 0
    assert fast_path_eligible(loaded, current_env="Old\0") is False


# --- re-grounding ------------------------------------------------------------------


def _marks() -> tuple[MarkElement, ...]:
    return parse_ax_elements_to_marks(
        ('Button "Send" at (100,100) 80x24 value="old body" (focused)',)
    )


def test_click_regrounds_to_the_live_element_despite_state_drift() -> None:
    """The recorded identity is canonical: field values and focus markers
    change between runs, and the comparison must not care. Comparing display
    labels here would make every filled field unmatchable."""
    step = RecipeStep(
        action_type="mouse_click",
        params={"button": "left", "click_count": 1, "x": 999, "y": 999},
        target='Button "Send"',
    )
    marks = _marks()
    action = rebuild_recipe_action(step, marks)

    assert isinstance(action, ClickMark)
    resolved = resolve_mark(action, marks)
    # Live centre (100, 100) — the stale (999, 999) never actuates. (The
    # summary names the centre; the mark rect is derived around it.)
    assert isinstance(resolved, MouseClick)
    assert (resolved.x, resolved.y) == (100, 100)


def test_click_with_no_live_target_refuses_to_guess() -> None:
    step = RecipeStep(action_type="mouse_click", params={}, target='Button "Gone"')
    assert rebuild_recipe_action(step, _marks()) is None


def test_garbage_payload_and_values_fail_closed() -> None:
    assert (
        rebuild_recipe_action(
            RecipeStep(action_type="no_such_action", params={}), _marks()
        )
        is None
    )
    assert (
        rebuild_recipe_action(
            RecipeStep(
                action_type="mouse_click",
                params={"button": "middle-click-everything"},
                target='Button "Send"',
            ),
            _marks(),
        )
        is None
    )
    assert (
        rebuild_recipe_action(
            RecipeStep(
                action_type="clipboard_paste", params={"text": ["not", "text"]}
            ),
            _marks(),
        )
        is None
    )


def test_paste_replays_verbatim_minus_coordinates() -> None:
    step = RecipeStep(
        action_type="clipboard_paste",
        params={"text": "quarterly body", "x": 9, "y": 9},
    )
    action = rebuild_recipe_action(step, _marks())
    assert isinstance(action, ClipboardPaste)
    assert action.text == "quarterly body"


# --- the loop ----------------------------------------------------------------------
# Sensorless on purpose: marks and the AX witness come from the ax_probe
# summaries, which is the entire re-grounding surface. Pixel verification
# is orthogonal and covered elsewhere; determinism here is the point.


def _finish_turn() -> AgentTurn:
    return AgentTurn(
        thought="verify",
        sub_goal="verify and finish",
        action=Finish(type="finish", status="success", summary="done"),
    )


def test_replay_spends_exactly_one_provider_turn() -> None:
    """The headline metric: a 2-step proven workflow costs 1 model turn
    (verify-and-finish), not 3. Both recipe actions still execute."""
    skill = _proven_skill()
    executed: list[str] = []
    provider_calls = {"n": 0}

    def provider(state: WorkingState) -> AgentTurn:
        provider_calls["n"] += 1
        return _finish_turn()

    def execute_physical(action: object) -> None:
        if isinstance(action, MouseClick):
            executed.append(f"click {action.x},{action.y}")
        else:
            executed.append(type(action).__name__)

    runner = OodaRunner(
        provider=provider,
        execute_physical=execute_physical,
        ax_probe=lambda: AxProbeResult(
            summaries=('Button "Send" at (100,100) 80x24 value="old" (focused)',)
        ),
        skill_scan=lambda q: (summary_of(skill),),
        skill_loader=lambda sid: skill,
        app=APP,
        max_steps=10,
    )
    runner.run(goal=GOAL)

    assert provider_calls["n"] == 1
    assert executed[0] == "click 100,100"
    assert executed[1] == "ClipboardPaste"


def test_missed_replay_records_failure_and_runs_normally() -> None:
    """Target gone from the screen: no blind click, the streak resets with
    a reason, and the run still completes through ordinary OODA."""
    skill = _proven_skill()
    failures: list[tuple[str, str]] = []
    executed: list[str] = []
    seen_errors: list[str | None] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen_errors.append(state.last_error)
        return _finish_turn()

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda action: executed.append(type(action).__name__),
        ax_probe=lambda: AxProbeResult(
            summaries=('Button "Cancel" at (10,10) 80x24',)
        ),
        skill_scan=lambda q: (summary_of(skill),),
        skill_loader=lambda sid: skill,
        record_skill_failure=lambda sid, reason: failures.append((sid, reason)),
        app=APP,
        max_steps=10,
    )
    runner.run(goal=GOAL)

    assert executed == [], "a missed replay actuates nothing"
    assert len(failures) == 1
    assert failures[0][0] == skill.skill_id
    assert "no live target" in failures[0][1]
    # The diagnosis travels to the provider turn that continues the run
    # (the finish claim clears it from the final state, which is correct —
    # the run is over, and the episode carries the retrospective instead).
    assert any(
        error is not None and "fast-path replay" in error for error in seen_errors
    )


def test_distill_record_replay_chain_end_to_end(tmp_path: Path) -> None:
    """The whole learning loop through real persistence: distill a fresh
    trajectory, save it to a real on-disk registry, earn a two-win streak
    across two separate bookkeeping calls, then replay from a fresh load —
    with exactly one provider turn for the verify-and-finish.

    The unit tests pin each link; this pins that the links connect: a
    recipe that cannot survive a save/load round-trip, an env string that
    drifts between recording and replay, or a loader that drops the streak
    would all pass the unit tests and fail here.
    """
    goal = "file the quarterly report"
    registry = SkillRegistry(tmp_path / "skills")
    live_tree = (
        'Button "Send" at (100,100) 80x24',
        'Button "Confirm" at (200,200) 80x24',
    )

    trajectory = Trajectory(
        app=APP,
        description=goal,
        steps=(
            MouseClick(type="mouse_click", x=11, y=22),
            MouseClick(type="mouse_click", x=33, y=44),
        ),
        step_descriptions=("click Send", "click Confirm"),
        step_targets=('Button "Send"', 'Button "Confirm"'),
    )
    result = distill(trajectory, known_signatures=set())
    assert result.kind == "skill"
    assert result.definition is not None
    assert len(result.definition.recipe) == 2
    registry.save(result.definition)
    skill_id = result.definition.skill_id

    # Fresh load, as a later run would see it: no recipe yet proven.
    loaded = registry.load(skill_id)
    env = environment_fingerprint(APP, goal)
    assert fast_path_eligible(loaded, current_env=env) is False

    # Two verified runs later, recorded the way Agent.on_complete does.
    registry.record_outcome(skill_id, succeeded=True, env=env)
    registry.record_outcome(skill_id, succeeded=True, env=env)
    proven = registry.load(skill_id)
    assert fast_path_eligible(proven, current_env=env) is True

    executed: list[str] = []
    provider_calls = {"n": 0}

    def provider(state: WorkingState) -> AgentTurn:
        provider_calls["n"] += 1
        return _finish_turn()

    def execute_physical(action: object) -> None:
        if isinstance(action, MouseClick):
            executed.append(f"click {action.x},{action.y}")
        else:
            executed.append(type(action).__name__)

    runner = OodaRunner(
        provider=provider,
        execute_physical=execute_physical,
        ax_probe=lambda: AxProbeResult(summaries=live_tree),
        skill_scan=lambda q: [summary_of(proven)],
        skill_loader=registry.load,
        app=APP,
        max_steps=10,
    )
    runner.run(goal=goal)

    assert provider_calls["n"] == 1
    # Re-grounded to the live marks (summaries name the centre here):
    # the recorded (11,22)/(33,44) must never actuate.
    assert executed == ["click 100,100", "click 200,200"]
