"""TDD regressions for the verified skill fast-path (#75)."""

from __future__ import annotations

from datetime import UTC, datetime

from computeruse.skills.fast_path import FastPathProvider, resolve_ax_target

from computeruse.orchestrator.loop import WorkingState
from computeruse.orchestrator.schemas import (
    ActivateApp,
    AgentTurn,
    Finish,
    MouseClick,
    PressHotkey,
    Wait,
)
from computeruse.skills.distiller import Trajectory, distill
from computeruse.skills.registry import (
    SkillRegistry,
    environment_fingerprint,
    fast_path_eligible,
)
from computeruse.skills.schemas import (
    FastPathActivateApp,
    FastPathAxPress,
    FastPathHotkey,
    FastPathWait,
    SkillDefinition,
    summary_of,
)


def _definition(**updates: object) -> SkillDefinition:
    base: dict[str, object] = {
        "skill_id": "safari.save-document",
        "description": "Save the document",
        "app": "Safari",
        "uses": 3,
        "wins": 3,
        "consecutive_successes": 3,
        "last_environment": environment_fingerprint("Safari", "Save the document"),
        "steps": ("click save", "wait"),
        "signature": "abcdef0123456789",
        "fast_path": (
            FastPathAxPress(type="ax_press", target='Button "Save"'),
            FastPathWait(type="wait", duration_ms=20, reason="settle"),
        ),
    }
    base.update(updates)
    return SkillDefinition.model_validate(base)


def test_fast_path_eligibility_requires_exact_environment_and_track_record() -> None:
    summary = summary_of(_definition())

    assert fast_path_eligible(summary, app="Safari", goal="Save the document") is True
    assert fast_path_eligible(summary, app="Safari", goal="Delete the document") is False
    assert (
        fast_path_eligible(
            summary.model_copy(update={"consecutive_successes": 1}),
            app="Safari",
            goal="Save the document",
        )
        is False
    )
    assert (
        fast_path_eligible(
            summary.model_copy(update={"wins": 2}),
            app="Safari",
            goal="Save the document",
        )
        is False
    )


def test_environment_fingerprint_separates_named_sites() -> None:
    github = environment_fingerprint("Google Chrome", "Open GitHub notifications")
    reddit = environment_fingerprint("Google Chrome", "Open Reddit notifications")

    assert github != reddit
    assert github == environment_fingerprint("Google Chrome", "Open GitHub notifications")


def test_registry_records_consecutive_success_and_failure_context(tmp_path) -> None:
    registry = SkillRegistry(tmp_path)
    registry.save(_definition(uses=0, wins=0, consecutive_successes=0, last_environment=None))
    observed = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)

    registry.record_outcome(
        "safari.save-document",
        succeeded=True,
        environment="Safari|site:none",
        observed_at=observed,
    )
    after_win = registry.load("safari.save-document")
    assert (after_win.uses, after_win.wins, after_win.consecutive_successes) == (1, 1, 1)
    assert after_win.last_successful_at == observed
    assert after_win.last_environment == "Safari|site:none"

    registry.record_outcome(
        "safari.save-document",
        succeeded=False,
        failure_reason="verification failed after target moved",
    )
    after_failure = registry.load("safari.save-document")
    assert (after_failure.uses, after_failure.wins, after_failure.consecutive_successes) == (2, 1, 0)
    assert after_failure.last_failure_reason == "verification failed after target moved"


def test_distiller_never_persists_click_coordinates_in_fast_path() -> None:
    trajectory = Trajectory(
        app="Safari",
        description="Save the document",
        steps=(
            MouseClick(type="mouse_click", x=912, y=641),
            Wait(type="wait", duration_ms=20, reason="settle"),
        ),
        step_descriptions=("Press Save", "Wait for save"),
        step_targets=('Button "Save"', ""),
    )

    result = distill(trajectory, set())

    assert result.definition is not None
    assert result.definition.fast_path == (
        FastPathAxPress(type="ax_press", target='Button "Save"'),
        FastPathWait(type="wait", duration_ms=20, reason="settle"),
    )
    rendered = result.definition.model_dump_json()
    assert '"x":912' not in rendered
    assert '"y":641' not in rendered


def test_distiller_refuses_fast_path_when_a_click_has_no_semantic_target() -> None:
    trajectory = Trajectory(
        app="Safari",
        description="Save the document",
        steps=(
            MouseClick(type="mouse_click", x=912, y=641),
            Wait(type="wait", duration_ms=20, reason="settle"),
        ),
        step_targets=("", ""),
    )

    result = distill(trajectory, set())

    assert result.definition is not None
    assert result.definition.fast_path == ()


def test_distiller_preserves_only_supported_coordinate_free_actions() -> None:
    trajectory = Trajectory(
        app="Safari",
        description="Open and settle",
        steps=(
            ActivateApp(type="activate_app", app="Safari"),
            PressHotkey(type="press_hotkey", modifiers=["command"], key="s"),
            Wait(type="wait", duration_ms=50, reason="settle"),
        ),
    )

    result = distill(trajectory, set())

    assert result.definition is not None
    assert result.definition.fast_path == (
        FastPathActivateApp(type="activate_app", app="Safari"),
        FastPathHotkey(type="press_hotkey", modifiers=("command",), key="s"),
        FastPathWait(type="wait", duration_ms=50, reason="settle"),
    )


def test_resolve_ax_target_requires_one_exact_semantic_identity() -> None:
    ui = (
        'Button "Cancel" at (100,200) 80x24',
        'Button "Save" at (220,200) 80x24',
    )

    assert resolve_ax_target('Button "Save"', ui) == MouseClick(
        type="mouse_click", x=260, y=212
    )
    assert resolve_ax_target('Button "Missing"', ui) is None
    assert (
        resolve_ax_target(
            'Button "Save"',
            ui + ('Button "Save" at (400,200) 80x24',),
        )
        is None
    )


def test_provider_completes_semantic_fast_path_without_fallback_calls() -> None:
    fallback_calls = 0

    def fallback(_state: WorkingState) -> AgentTurn:
        nonlocal fallback_calls
        fallback_calls += 1
        return AgentTurn(
            thought="fallback",
            sub_goal="fallback",
            action=Finish(type="finish", status="failed", summary="fallback called"),
        )

    provider = FastPathProvider(
        instructions=(FastPathAxPress(type="ax_press", target='Button "Save"'),),
        fallback=fallback,
    )
    state = WorkingState(
        goal="Save the document",
        ui_elements=('Button "Save" at (220,200) 80x24',),
    )

    first = provider(state)
    second = provider(state.model_copy(update={"step_index": 1}))

    assert first.action == MouseClick(type="mouse_click", x=260, y=212)
    assert isinstance(second.action, Finish)
    assert second.action.status == "success"
    assert fallback_calls == 0
    assert provider.provider_calls == 0
    assert provider.fast_path_turns == 2


def test_provider_disables_fast_path_on_mismatch_or_execution_error() -> None:
    fallback_calls = 0

    def fallback(_state: WorkingState) -> AgentTurn:
        nonlocal fallback_calls
        fallback_calls += 1
        return AgentTurn(
            thought="recover normally",
            sub_goal="recover",
            action=Wait(type="wait", duration_ms=0, reason="fallback"),
        )

    missing = FastPathProvider(
        instructions=(FastPathAxPress(type="ax_press", target='Button "Save"'),),
        fallback=fallback,
    )
    missing(WorkingState(goal="Save the document", ui_elements=()))
    assert fallback_calls == 1
    assert missing.provider_calls == 1

    errored = FastPathProvider(
        instructions=(FastPathWait(type="wait", duration_ms=10, reason="settle"),),
        fallback=fallback,
    )
    errored(WorkingState(goal="Save the document", last_error="verification failed"))
    errored(WorkingState(goal="Save the document"))
    assert fallback_calls == 3
    assert errored.provider_calls == 2
    assert errored.fast_path_turns == 0
