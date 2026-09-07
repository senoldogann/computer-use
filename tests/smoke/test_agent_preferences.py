"""Agent integration regressions for verified adaptive preference memory."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from computeruse.agent import Agent, AgentConfig
from computeruse.memory.preferences import PreferenceStore, PreferenceWrite
from computeruse.orchestrator.evidence import CompletionVerdict
from computeruse.orchestrator.loop import WorkingState
from computeruse.orchestrator.schemas import AgentTurn, Finish, MouseClick
from computeruse.security.autonomy import AutonomyLevel
from tests.smoke.conftest import SIMULATED_SETTLE, SOCKET_PATH


def _turn(action: object) -> AgentTurn:
    return AgentTurn.model_validate(
        {"thought": "test", "sub_goal": "test preference memory", "action": action}
    )


def _one_click_then_finish(
    *,
    status: str = "success",
    seen_knowledge: list[tuple[str, ...]] | None = None,
) -> Callable[[WorkingState], AgentTurn]:
    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            if seen_knowledge is not None:
                seen_knowledge.append(state.knowledge)
            return _turn(MouseClick(type="mouse_click", x=100, y=100))
        return _turn(
            Finish(
                type="finish",
                status=status,
                summary="done" if status == "success" else "failed",
            )
        )

    return provider


def _config(
    tmp_path: Path,
    *,
    goal: str,
    provider: Callable[[WorkingState], AgentTurn] | None = None,
    on_preference_write: Callable[[PreferenceWrite], None] | None = None,
    completion_check: Callable[[WorkingState, str], CompletionVerdict] | None = None,
) -> AgentConfig:
    return AgentConfig(
        goal=goal,
        app="Safari",
        provider=provider or _one_click_then_finish(),
        socket_path=str(SOCKET_PATH),
        store_dir=tmp_path / "store",
        autonomy_level=AutonomyLevel.GUARDED,
        enable_visual_verification=False,
        completion_check=completion_check,
        on_preference_write=on_preference_write,
        max_steps=10,
        **SIMULATED_SETTLE,
    )


def test_verified_run_learns_preference_and_stages_it_on_next_run(tmp_path: Path) -> None:
    first = Agent(
        _config(tmp_path, goal="From now on use compact summaries.")
    ).run()

    assert first.succeeded is True
    assert len(first.preferences) == 1
    assert first.preferences[0].value == "From now on use compact summaries"

    seen: list[tuple[str, ...]] = []
    second = Agent(
        _config(
            tmp_path,
            goal="Open the menu.",
            provider=_one_click_then_finish(seen_knowledge=seen),
        )
    ).run()

    assert second.succeeded is True
    assert seen
    assert any(
        line.startswith("[preference:general]")
        and "From now on use compact summaries" in line
        for line in seen[0]
    )
    assert second.preferences == first.preferences


def test_failed_run_does_not_learn_explicit_preference(tmp_path: Path) -> None:
    result = Agent(
        _config(
            tmp_path,
            goal="From now on use compact summaries.",
            provider=_one_click_then_finish(status="failed"),
        )
    ).run()

    assert result.succeeded is False
    assert result.preferences == ()
    assert PreferenceStore(tmp_path / "store" / "preferences").records() == ()


def test_forced_unverified_finish_does_not_learn_preference(tmp_path: Path) -> None:
    def reject(_state: WorkingState, _claim: str) -> CompletionVerdict:
        return CompletionVerdict(satisfied=False, evidence="not verified")

    result = Agent(
        _config(
            tmp_path,
            goal="From now on use compact summaries.",
            completion_check=reject,
        )
    ).run()

    assert result.succeeded is False
    assert result.preferences == ()
    assert PreferenceStore(tmp_path / "store" / "preferences").records() == ()


def test_empty_trajectory_success_does_not_learn_preference(tmp_path: Path) -> None:
    def finish_immediately(_state: WorkingState) -> AgentTurn:
        return _turn(Finish(type="finish", status="success", summary="already done"))

    writes: list[PreferenceWrite] = []
    result = Agent(
        _config(
            tmp_path,
            goal="From now on use compact summaries.",
            provider=finish_immediately,
            on_preference_write=writes.append,
        )
    ).run()

    assert result.succeeded is True
    assert result.trajectory == ()
    assert result.preferences == ()
    assert writes == []
    assert PreferenceStore(tmp_path / "store" / "preferences").records() == ()


def test_sensitive_durable_instruction_is_never_persisted(tmp_path: Path) -> None:
    secret_like = "token=" + "abcdef0123456789"
    result = Agent(
        _config(tmp_path, goal="From now on always use " + secret_like)
    ).run()

    assert result.succeeded is True
    assert result.preferences == ()
    assert PreferenceStore(tmp_path / "store" / "preferences").records() == ()


def test_preference_callback_receives_safe_write(tmp_path: Path) -> None:
    writes: list[PreferenceWrite] = []

    result = Agent(
        _config(
            tmp_path,
            goal="preference: response-style=compact summaries",
            on_preference_write=writes.append,
        )
    ).run()

    assert result.succeeded is True
    assert len(writes) == 1
    write = writes[0]
    assert write.outcome == "created"
    assert write.record is not None
    assert write.record.key == "response-style"
    assert write.record.value == "compact summaries"
    assert write.safe_summary == "created preference evidence for general/response-style"
    assert "run-" not in write.safe_summary


def test_preference_callback_failure_does_not_change_physical_outcome(
    tmp_path: Path,
) -> None:
    def broken_callback(_write: PreferenceWrite) -> None:
        raise RuntimeError("UI event sink unavailable")

    result = Agent(
        _config(
            tmp_path,
            goal="From now on use compact summaries.",
            on_preference_write=broken_callback,
        )
    ).run()

    assert result.succeeded is True
    assert [action.type for action in result.trajectory] == ["mouse_click"]
    assert len(result.preferences) == 1
