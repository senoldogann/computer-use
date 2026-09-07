"""P0-1: a force-accepted finish must never become a skill.

The stalemate guard (``MAX_FINISH_REJECTIONS``) ends a run whose finish
claims the auditor keeps rejecting, so the actor and the auditor cannot
trade turns forever. That ending used to travel as a plain
``outcome="success"``: the caller's distill gate read exactly that, so a
flow the auditor never verified was distilled into the skill store as a
recipe — and the user was told the run succeeded when the machine's own
screen had denied it. An unverified flow is not a skill, and an unverified
success is a false-green.

These tests pin both halves of the fix: the stalemate ending is recorded
as a failure carrying an honest retrospective (never the model's claimed
success), and a full agent run through the real (simulated) driver
completes while distilling nothing.
"""

from __future__ import annotations

from pathlib import Path

from computeruse.agent import Agent, AgentConfig
from computeruse.memory.schemas import Episode
from computeruse.orchestrator.evidence import CompletionVerdict
from computeruse.orchestrator.loop import (
    STALEMATE_RETROSPECTIVE,
    AxProbeResult,
    OodaRunner,
    WorkingState,
)
from computeruse.orchestrator.schemas import AgentTurn, Finish, MouseClick, Wait
from computeruse.security.autonomy import AutonomyLevel
from computeruse.skills.distiller import Trajectory
from computeruse.skills.registry import SkillRegistry
from computeruse.vision import ScreenCapture
from tests.smoke.conftest import SIMULATED_SETTLE, SOCKET_PATH

_ONE_BY_ONE = ScreenCapture(
    display_id=0, width=1, height=1, scale=1.0, data=b"\x00\x00\x00\x00"
)


def _turn(action: object) -> AgentTurn:
    return AgentTurn.model_validate({"thought": "", "sub_goal": "", "action": action})


def _deny_everything(_state: WorkingState, _claim: str) -> CompletionVerdict:
    return CompletionVerdict(satisfied=False, evidence="the goal is nowhere on screen")


def test_forced_finish_reaches_on_complete_flagged() -> None:
    """Two rejections plus insistence ends the run — as an honest failure.

    The wait step exists so there is a trajectory to hand over (a run that
    never acted fires no callback at all); the finish insistence is the
    actor disagreeing with the auditor on every turn after it. The stalemate
    guard lets the run terminate, but never as the success the auditor
    denied: the outcome handed to ``on_complete`` is ``failure`` with the
    model's claim replaced by the honest retrospective.
    """

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(Wait(type="wait", duration_ms=5, reason="settle"))
        return _turn(Finish(type="finish", status="success", summary="done"))

    received: list[tuple[str, bool, str | None]] = []
    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=lambda: _ONE_BY_ONE,
        completion_check=_deny_everything,
        on_complete=lambda _t, o, r, _s, forced: received.append((o, forced, r)),
        max_steps=10,
    )
    runner.run(goal="do the thing")
    # Failure outcome + forced flag + the honest diagnostic retrospective —
    # never the model's own "done" summary riding to the user as a success.
    assert received == [("failure", True, STALEMATE_RETROSPECTIVE)]


def test_verified_finish_stays_unflagged() -> None:
    """The flag means forced — an auditor-accepted finish must not carry it."""

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(Wait(type="wait", duration_ms=5, reason="settle"))
        return _turn(Finish(type="finish", status="success", summary="done"))

    received: list[bool] = []
    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=lambda: _ONE_BY_ONE,
        completion_check=lambda _s, _c: CompletionVerdict(
            satisfied=True, evidence="the thing is on screen"
        ),
        on_complete=lambda _t, _o, _r, _s, forced: received.append(forced),
        max_steps=10,
    )
    runner.run(goal="do the thing")
    assert received == [False]


def test_forced_success_distills_nothing(tmp_path: Path) -> None:
    """The P0-1 chain, end to end: rejected twice, forced on the third claim.

    Two real clicks run first — the distiller's minimum for a skill — so
    without the gate this exact run would poison the store. The run still
    completes (the stalemate guard did its job), but the skill index stays
    empty and the episode says it was forced.
    """

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=100, y=100))
        if state.step_index == 1:
            return _turn(MouseClick(type="mouse_click", x=200, y=200))
        return _turn(Finish(type="finish", status="success", summary="done"))

    config = AgentConfig(
        goal="open the menu",
        app="Safari",
        provider=provider,
        socket_path=str(SOCKET_PATH),
        store_dir=tmp_path / "store",
        autonomy_level=AutonomyLevel.GUARDED,
        completion_check=_deny_everything,
        enable_visual_verification=False,
        max_steps=10,
        **SIMULATED_SETTLE,
    )
    result = Agent(config).run()
    assert [s.type for s in result.trajectory] == ["mouse_click", "mouse_click"]
    assert result.distilled is None
    assert SkillRegistry(tmp_path / "store" / "skills").index() == []
    assert result.skills == ()
    assert len(result.episodes) == 1
    episode = result.episodes[0]
    assert episode.outcome == "failure"
    assert episode.forced_completion is True
    # The user-facing side is failure everywhere, and the stored episode now
    # agrees: a forced finish was never verified, so it records as the failure
    # the auditor implied, not the success the model claimed.
    assert result.succeeded is False
    assert result.outcome == "failure"


def test_legacy_episode_without_the_flag_reads_as_not_forced() -> None:
    """History recorded before the flag existed keeps validating (schema vow)."""
    episode = Episode.model_validate(
        {
            "episode_id": "safari.old",
            "app": "Safari",
            "description": "an old run",
            "steps": [],
            "outcome": "success",
            "signature": "s",
            "recorded_at": "2026-09-01T00:00:00+00:00",
        }
    )
    assert episode.forced_completion is False


def test_forced_flag_survives_the_full_distill_chain() -> None:
    """The trajectory type the callback receives is unchanged — only flagged.

    Guards the plumbing against a refactor that "simplifies" the fifth
    argument away: the flag must reach the caller on the exact trajectory
    the distiller would otherwise consume.
    """
    seen: list[tuple[Trajectory, bool]] = []
    runner = OodaRunner(
        provider=lambda state: (
            _turn(Wait(type="wait", duration_ms=5, reason="settle"))
            if state.step_index == 0
            else _turn(Finish(type="finish", status="success", summary="done"))
        ),
        execute_physical=lambda _action: None,
        sensor=lambda: _ONE_BY_ONE,
        completion_check=_deny_everything,
        on_complete=lambda t, _o, _r, _s, forced: seen.append((t, forced)),
        max_steps=10,
    )
    runner.run(goal="do the thing")
    assert len(seen) == 1
    trajectory, forced = seen[0]
    assert forced is True
    assert [s.type for s in trajectory.steps] == ["wait"]


def test_an_unreachable_auditor_marks_the_finish_unverified() -> None:
    """A provider outage must not turn every claimed success into a skill.

    The auditor may never kill a run, so a transport failure lets the finish
    stand. But it was not verified, and the distill gate reads exactly this
    flag: leaving it unset meant an unreachable auditor silently restored the
    memory poisoning P0-1 closed. Measured before the fix: forced_finish=false
    while the auditor raised on every call.
    """

    def provider(state: WorkingState) -> AgentTurn:
        return AgentTurn(
            thought="t",
            sub_goal="s",
            action=Finish(type="finish", status="success", summary="claimed"),
        )

    def unreachable(state: WorkingState, summary: str) -> CompletionVerdict:
        raise RuntimeError("audit transport unavailable")

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        ax_probe=lambda: AxProbeResult(summaries=()),
        completion_check=unreachable,
        max_steps=3,
    )
    runner.run("do the thing")

    assert runner._forced_finish is True


def test_recovery_after_rejections_is_accepted_as_verified() -> None:
    """The agent is rejected twice, repairs the state, and the 3rd attempt is verified."""
    attempts = 0

    def auditor(_state: WorkingState, _claim: str) -> CompletionVerdict:
        nonlocal attempts
        attempts += 1
        if attempts >= 3:
            return CompletionVerdict(satisfied=True, evidence="the goal is now verified")
        return CompletionVerdict(satisfied=False, evidence="not yet verified")

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index in (0, 2, 4):
            return _turn(Wait(type="wait", duration_ms=5, reason="settle"))
        return _turn(Finish(type="finish", status="success", summary="done"))

    received: list[tuple[str, bool, str | None]] = []
    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=lambda: _ONE_BY_ONE,
        completion_check=auditor,
        on_complete=lambda _t, o, r, _s, forced: received.append((o, forced, r)),
        max_steps=10,
    )
    runner.run(goal="do the thing")
    assert received == [("success", False, "done")]
