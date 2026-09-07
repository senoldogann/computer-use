"""Regression tests for zero-action terminal outcome publication (#69).

A terminal ``finish`` is still a run outcome when the requested state already
exists and no actuation was necessary. That outcome must reach the product
shell without turning an empty trajectory into durable learning. Abnormal
pre-action termination remains a different contract and is covered by the
existing kill-switch regression in ``test_distill_loop.py``.
"""

from __future__ import annotations

from pathlib import Path

from computeruse.agent import Agent, AgentConfig
from computeruse.memory.preferences import PreferenceStore, PreferenceWrite
from computeruse.memory.schemas import EpisodeOutcome
from computeruse.orchestrator.loop import OodaRunner, WorkingState
from computeruse.orchestrator.schemas import AgentTurn, Finish
from computeruse.skills.distiller import Trajectory
from tests.smoke.conftest import SIMULATED_SETTLE, SOCKET_PATH


def _turn(*, status: str, summary: str) -> AgentTurn:
    return AgentTurn.model_validate(
        {
            "thought": "the requested state is already satisfied",
            "sub_goal": "publish the terminal result",
            "action": Finish(type="finish", status=status, summary=summary),
        }
    )


def test_zero_action_success_publishes_terminal_outcome_once() -> None:
    received: list[
        tuple[Trajectory, EpisodeOutcome, str | None, str | None, bool]
    ] = []

    runner = OodaRunner(
        provider=lambda _state: _turn(status="success", summary="already done"),
        execute_physical=lambda _action: None,
        app="Safari",
        on_complete=lambda trajectory, outcome, retrospective, skill_id, forced: received.append(
            (trajectory, outcome, retrospective, skill_id, forced)
        ),
        max_steps=3,
    )

    final = runner.run(goal="leave the already-correct state unchanged")

    assert final.last_error is None
    assert len(received) == 1
    trajectory, outcome, retrospective, skill_id, forced = received[0]
    assert trajectory.steps == ()
    assert outcome == "success"
    assert retrospective == "already done"
    assert skill_id is None
    assert forced is False


def test_zero_action_failed_finish_publishes_failure_outcome_once() -> None:
    received: list[EpisodeOutcome] = []

    runner = OodaRunner(
        provider=lambda _state: _turn(status="failed", summary="cannot verify"),
        execute_physical=lambda _action: None,
        on_complete=lambda _trajectory, outcome, _retrospective, _skill_id, _forced: received.append(
            outcome
        ),
        max_steps=3,
    )

    final = runner.run(goal="verify the requested state")

    assert final.last_error == "cannot verify"
    assert received == ["failure"]


def test_agent_zero_action_success_reports_success_without_learning(tmp_path: Path) -> None:
    writes: list[PreferenceWrite] = []

    config = AgentConfig(
        goal="From now on use compact summaries.",
        app="Safari",
        provider=lambda _state: _turn(status="success", summary="already done"),
        socket_path=str(SOCKET_PATH),
        store_dir=tmp_path / "store",
        enable_visual_verification=False,
        on_preference_write=writes.append,
        max_steps=3,
        **SIMULATED_SETTLE,
    )

    result = Agent(config).run()

    assert result.succeeded is True
    assert result.trajectory == ()
    assert result.distilled is None
    assert result.episodes == ()
    assert result.preferences == ()
    assert writes == []
    assert PreferenceStore(tmp_path / "store" / "preferences").records() == ()
