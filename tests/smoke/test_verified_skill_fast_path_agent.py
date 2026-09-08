"""Product-level RED regressions for verified skill fast-path integration (#75)."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from computeruse.agent import Agent, AgentConfig
from computeruse.orchestrator.evidence import CompletionVerdict
from computeruse.orchestrator.loop import WorkingState
from computeruse.orchestrator.schemas import AgentTurn, Finish, MouseClick
from computeruse.security.autonomy import AutonomyLevel
from computeruse.skills.fast_path import FastPathProvider
from computeruse.skills.registry import SkillRegistry, environment_fingerprint
from computeruse.skills.schemas import FastPathAxPress, SkillDefinition
from tests.smoke.conftest import SIMULATED_SETTLE, SOCKET_PATH


def _proven_skill(*, target: str = 'Button "Reload"') -> SkillDefinition:
    goal = "click reload"
    return SkillDefinition(
        skill_id="safari.click-reload",
        description=goal,
        app="Safari",
        uses=2,
        wins=2,
        consecutive_successes=2,
        last_environment=environment_fingerprint("Safari", goal),
        steps=("press Reload",),
        fast_path=(FastPathAxPress(type="ax_press", target=target),),
        signature="0123456789abcdef",
    )


def _reload_action(state: WorkingState) -> MouseClick:
    line = next(item for item in state.ui_elements if 'Button "Reload"' in item)
    match = re.search(r"at \((-?\d+),(-?\d+)\) (\d+)x(\d+)", line)
    assert match is not None
    x, y, width, height = (int(group) for group in match.groups())
    return MouseClick(
        type="mouse_click",
        x=x + width // 2,
        y=y + height // 2,
    )


def _config(
    tmp_path: Path,
    *,
    provider: Callable[[WorkingState], AgentTurn],
    completion_check: Callable[[WorkingState, str], CompletionVerdict],
) -> AgentConfig:
    return AgentConfig(
        goal="click reload",
        app="Safari",
        provider=provider,
        socket_path=str(SOCKET_PATH),
        store_dir=tmp_path / "store",
        autonomy_level=AutonomyLevel.GUARDED,
        enable_visual_verification=True,
        enable_vision=False,
        completion_check=completion_check,
        max_steps=10,
        **SIMULATED_SETTLE,
    )


def test_fast_path_provider_exposes_failure_after_semantic_mismatch() -> None:
    provider = FastPathProvider(
        instructions=(FastPathAxPress(type="ax_press", target='Button "Missing"'),),
        fallback=lambda _state: AgentTurn(
            thought="fallback",
            sub_goal="fallback",
            action=Finish(type="finish", status="failed", summary="fallback"),
        ),
    )

    provider(WorkingState(goal="click reload", ui_elements=()))

    assert provider.fast_path_failed is True


def test_agent_eligible_fast_path_uses_zero_fallback_calls_and_completion_audit(
    tmp_path: Path,
) -> None:
    registry = SkillRegistry(tmp_path / "store" / "skills")
    registry.save(_proven_skill())
    fallback_calls = 0
    audit_calls = 0

    def fallback(_state: WorkingState) -> AgentTurn:
        nonlocal fallback_calls
        fallback_calls += 1
        return AgentTurn(
            thought="fallback should not run",
            sub_goal="fail loudly",
            action=Finish(
                type="finish",
                status="failed",
                summary="fallback provider was called",
            ),
        )

    def audit(_state: WorkingState, claim: str) -> CompletionVerdict:
        nonlocal audit_calls
        audit_calls += 1
        assert "fast-path" in claim
        return CompletionVerdict(satisfied=True, evidence="reload workflow observed")

    result = Agent(_config(tmp_path, provider=fallback, completion_check=audit)).run()

    assert result.succeeded is True
    assert result.state.last_error is None
    assert [action.type for action in result.trajectory] == ["mouse_click"]
    assert fallback_calls == 0
    assert audit_calls == 1
    stored = registry.load("safari.click-reload")
    assert (stored.uses, stored.wins, stored.consecutive_successes) == (3, 3, 3)
    assert stored.last_environment == environment_fingerprint("Safari", "click reload")


def test_fast_path_mismatch_falls_back_but_does_not_reinforce_failed_skill(
    tmp_path: Path,
) -> None:
    registry = SkillRegistry(tmp_path / "store" / "skills")
    registry.save(_proven_skill(target='Button "Missing"'))
    fallback_calls = 0

    def fallback(state: WorkingState) -> AgentTurn:
        nonlocal fallback_calls
        fallback_calls += 1
        if state.step_index == 0:
            return AgentTurn(
                thought="recover from stale skill",
                sub_goal="press the live Reload control",
                action=_reload_action(state),
            )
        return AgentTurn(
            thought="recovered",
            sub_goal="finish",
            action=Finish(type="finish", status="success", summary="reload clicked"),
        )

    result = Agent(
        _config(
            tmp_path,
            provider=fallback,
            completion_check=lambda _state, _claim: CompletionVerdict(
                satisfied=True,
                evidence="recovered workflow observed",
            ),
        )
    ).run()

    assert result.succeeded is True
    assert fallback_calls > 0
    stored = registry.load("safari.click-reload")
    assert stored.uses == 3
    assert stored.wins == 2, "fallback recovery must not count as a fast-path win"
    assert stored.consecutive_successes == 0
    assert stored.last_failure_reason is not None
