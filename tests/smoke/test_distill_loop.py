"""OODA DISTILL wiring (Law 3 + Law 4) — the self-evolving step.

The runner records the *typed* trajectory of successfully executed actions and
fires ``on_complete`` on every terminal ``finish``, handing the caller the
trajectory plus the run outcome. The caller owns persistence (an episode for
Law 4, a skill for Law 3); the loop stays decoupled from the stores. These
tests pin the loop side of that contract and one full chain — a recorded
episode feeds ``known_signatures`` so the same flow is never re-distilled.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from computeruse.memory.episodic import (
    EpisodicStore,
    episode_from_trace,
    signature_from_trace,
)
from computeruse.memory.schemas import EpisodeOutcome
from computeruse.orchestrator.loop import (
    KillSwitchTripped,
    MaxStepsError,
    OodaRunner,
    WorkingState,
)
from computeruse.orchestrator.schemas import AgentTurn, Finish, MouseClick, Wait
from computeruse.skills.distiller import DistillResult, Trajectory, distill


def _turn(action: object) -> AgentTurn:
    return AgentTurn.model_validate({"thought": "", "sub_goal": "", "action": action})


def _click(x: int, y: int) -> MouseClick:
    return MouseClick(type="mouse_click", x=x, y=y)


def test_runner_fires_on_complete_with_executed_trajectory() -> None:
    received: list[tuple[Trajectory, EpisodeOutcome, str | None]] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(_click(10, 10))
        if state.step_index == 1:
            return _turn(Wait(type="wait", duration_ms=5, reason="settle"))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _a: None,
        app="Safari",
        on_complete=lambda t, o, r: received.append((t, o, r)),
        max_steps=10,
    )
    runner.run(goal="open the menu")
    assert received, "on_complete must fire on a terminal finish"
    trajectory, outcome, retrospective = received[0]
    assert outcome == "success"
    assert retrospective == "done"
    # The finish action itself is excluded from the flow.
    assert [s.type for s in trajectory.steps] == ["mouse_click", "wait"]
    assert trajectory.app == "Safari"
    assert trajectory.description == "open the menu"
    assert runner.executed_trajectory == trajectory.steps


def test_failed_finish_reports_failure_outcome() -> None:
    received: list[EpisodeOutcome] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(_click(10, 10))
        return _turn(Finish(type="finish", status="failed", summary="element not found"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _a: None,
        on_complete=lambda _t, o, _r: received.append(o),
        max_steps=5,
    )
    runner.run(goal="click")
    assert received == ["failure"]


def test_failed_step_never_enters_trajectory() -> None:
    """A step that raised mid-execution is neither completed nor distilled."""

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(_click(10, 10))
        return _turn(Finish(type="finish", status="success", summary="done"))

    def boom(_action: object) -> None:
        raise RuntimeError("driver gone")

    runner = OodaRunner(provider=provider, execute_physical=boom, max_steps=5)
    final = runner.run(goal="x")
    assert final.last_error is not None
    assert runner.executed_trajectory == (), "failed steps must not be distilled"


def test_aborted_run_is_remembered_as_a_failure() -> None:
    """A truncated run still raises — and still leaves a trace behind it.

    The work a run did before hitting its budget is the most useful thing to
    remember about it: without an episode, the next attempt at the same goal
    starts with no idea that the last one got three steps in and stalled.
    """
    received: list[tuple[EpisodeOutcome, str | None]] = []

    def provider(state: WorkingState) -> AgentTurn:
        # Alternate coordinates so the stuck-loop guard never fires;
        # this test is about max_steps truncation, not repetition.
        x = 10 + state.step_index
        return _turn(_click(x, x))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _a: None,
        on_complete=lambda _t, o, r: received.append((o, r)),
        max_steps=3,
    )
    # The bounded-termination contract: the truncation raises a typed error
    # (no silent stop) — and the episode is written before it propagates.
    with pytest.raises(MaxStepsError):
        runner.run(goal="spin")
    assert len(received) == 1
    outcome, retrospective = received[0]
    assert outcome == "failure"
    assert retrospective is not None and "step budget" in retrospective


def test_kill_switch_takeover_is_remembered_as_a_failure() -> None:
    """A takeover after real work is a failed episode, not a silent discard."""
    received: list[tuple[EpisodeOutcome, str | None]] = []
    armed: list[bool] = []

    class TripsOnceAStepIsOnTheRecord:
        """Trips only after a click has actually been recorded as executed."""

        def tripped(self) -> bool:
            return bool(armed)

    def provider(state: WorkingState) -> AgentTurn:
        if state.completed_steps:
            armed.append(True)
        return _turn(_click(10 + state.step_index, 10))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _a: None,
        kill_switch=TripsOnceAStepIsOnTheRecord(),  # type: ignore[arg-type]
        on_complete=lambda _t, o, r: received.append((o, r)),
        max_steps=5,
    )
    with pytest.raises(KillSwitchTripped):
        runner.run(goal="x")
    assert len(received) == 1
    outcome, retrospective = received[0]
    assert outcome == "failure"
    assert retrospective is not None and "reclaimed control" in retrospective


def test_a_run_that_never_acted_leaves_nothing() -> None:
    """No executed action means no trajectory: the exception is the whole story."""
    calls: list[bool] = []

    class AlwaysTripped:
        def tripped(self) -> bool:
            return True

    runner = OodaRunner(
        provider=lambda _s: _turn(_click(10, 10)),
        execute_physical=lambda _a: None,
        kill_switch=AlwaysTripped(),  # type: ignore[arg-type]
        on_complete=lambda _t, _o, _r: calls.append(True),
        max_steps=5,
    )
    with pytest.raises(KillSwitchTripped):
        runner.run(goal="x")
    assert calls == []


def test_full_chain_episode_feeds_skill_dedup(tmp_path) -> None:
    """One loop: a successful run is remembered AND distilled; the second run
    of the same flow is recognized by its episode signature and never
    re-distilled (Law 3.3, wired through Law 4 memory)."""
    store = EpisodicStore(tmp_path / "memory")
    results: list[DistillResult] = []

    def on_complete(
        trajectory: Trajectory, outcome: EpisodeOutcome, retrospective: str | None
    ) -> None:
        # Distill against known history first, then remember — so the fresh
        # run is novel, and any future identical run is a duplicate.
        results.append(distill(trajectory, store.known_signatures()))
        store.record(
            episode_from_trace(
                app=trajectory.app,
                description=trajectory.description,
                steps=trajectory.steps,
                step_descriptions=trajectory.step_descriptions,
                outcome=outcome,
                retrospective=retrospective,
                episode_id=f"run-{len(store.episodes())}",
            )
        )

    def make_provider() -> Callable[[WorkingState], AgentTurn]:
        def provider(state: WorkingState) -> AgentTurn:
            if state.step_index == 0:
                return _turn(_click(10, 10))
            if state.step_index == 1:
                return _turn(_click(20, 20))
            return _turn(Finish(type="finish", status="success", summary="done"))

        return provider

    runner = OodaRunner(
        provider=make_provider(),
        execute_physical=lambda _a: None,
        app="Safari",
        on_complete=on_complete,
        max_steps=10,
    )
    runner.run(goal="first")
    runner.run(goal="second")

    assert [r.kind for r in results] == ["skill", "duplicate"]
    assert results[0].signature == results[1].signature
    assert results[0].definition is not None
    episodes = store.episodes()
    assert len(episodes) == 2
    assert episodes[0].signature == results[0].signature


def test_episode_from_trace_computes_signature_and_id() -> None:
    steps = (_click(10, 10), _click(20, 20))
    episode = episode_from_trace(
        app="Safari",
        description="open the menu",
        steps=steps,
        outcome="failure",
        retrospective="coordinates were stale",
    )
    assert episode.signature == signature_from_trace("Safari", steps)
    assert episode.steps == steps
    assert episode.outcome == "failure"
    assert episode.retrospective == "coordinates were stale"
    assert episode.episode_id.startswith("safari.")
