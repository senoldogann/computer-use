"""Running without being asked.

An agent choosing its own work is a different thing from one executing someone
else's, because nobody is watching when it chooses wrong. These tests cover the
two properties that make it defensible: it waits for the machine to be free,
and it proposes goals from memory rather than from imagination.
"""

from __future__ import annotations

import random
from pathlib import Path

from computeruse.autonomous import (
    GoalProposal,
    MachineActivity,
    SessionLimits,
    machine_is_idle,
    propose_goal,
    run_autonomously,
    wait_for_idle,
)
from computeruse.memory.episodic import EpisodicStore, episode_from_trace
from computeruse.orchestrator.schemas import MouseClick
from computeruse.skills.registry import SkillRegistry
from computeruse.skills.schemas import SkillDefinition


def _activity(x: float, app: str = "Finder") -> MachineActivity:
    """A sample from a driver too old to report the system idle clock."""
    return MachineActivity(cursor=(x, 0.0), frontmost=app, idle_seconds=None)


def test_a_still_machine_is_idle_and_a_moving_one_is_not() -> None:
    still = tuple(_activity(10) for _ in range(4))
    assert machine_is_idle(still, required=4, threshold=60) is True

    moved = (*[_activity(10) for _ in range(3)], _activity(11))
    assert machine_is_idle(moved, required=4, threshold=60) is False


def test_switching_apps_counts_as_using_the_machine() -> None:
    """The cursor alone misses someone reading; the frontmost window alone
    misses someone working inside one. Both have to hold still."""
    switched = (_activity(10, "Finder"), _activity(10, "Finder"), _activity(10, "Chrome"))
    assert machine_is_idle(switched, required=3, threshold=60) is False


def test_too_few_observations_is_not_idle() -> None:
    """Absence of evidence is not evidence of absence, and acting on one
    sample would mean acting during a pause for thought."""
    assert machine_is_idle((_activity(10),), required=4, threshold=60) is False


def test_waiting_can_be_interrupted_without_waiting_out_the_timer() -> None:
    """Someone reaching for the kill switch mid-wait should be heard."""
    polls = {"n": 0}

    def observe() -> MachineActivity:
        polls["n"] += 1
        return _activity(polls["n"])  # never settles

    became_idle = wait_for_idle(
        observe,
        idle_seconds=60,
        poll_seconds=1,
        stop=lambda: polls["n"] >= 3,
        sleep=lambda _s: None,
    )
    assert became_idle is False
    assert polls["n"] == 3


def test_a_blind_probe_is_never_read_as_idle() -> None:
    """A failing probe means we cannot see the machine, which is the one thing
    that must not be mistaken for "nobody is there"."""

    def observe() -> MachineActivity:
        raise RuntimeError("driver gone")

    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 5

    assert (
        wait_for_idle(
            observe, idle_seconds=10, poll_seconds=1, stop=stop, sleep=lambda _s: None
        )
        is False
    )


# --- choosing work -----------------------------------------------------------


def _skill(skill_id: str, uses: int, wins: int) -> SkillDefinition:
    return SkillDefinition(
        skill_id=skill_id,
        description=f"do the thing for {skill_id}",
        app="Google Chrome",
        steps=("a step",),
        signature=skill_id,
        uses=uses,
        wins=wins,
    )


def test_nothing_in_memory_means_nothing_to_do(tmp_path: Path) -> None:
    """An agent with nothing grounded to do should do nothing, not invent a
    task. This is the whole difference between self-directed and unmoored."""
    proposal = propose_goal(
        SkillRegistry(tmp_path / "skills"),
        EpisodicStore(tmp_path / "episodes"),
        rng=random.Random(0),
    )
    assert proposal is None


def test_a_broken_skill_is_chosen_first(tmp_path: Path) -> None:
    """It is the most concrete unfinished thing memory holds: a specific claim
    about how to do something that has never once been true."""
    skills = SkillRegistry(tmp_path / "skills")
    skills.save(_skill("chrome.broken", uses=3, wins=0))
    skills.save(_skill("chrome.fine", uses=3, wins=3))
    proposal = propose_goal(
        skills, EpisodicStore(tmp_path / "episodes"), rng=random.Random(0)
    )
    assert proposal is not None
    assert "chrome.broken" in proposal.reason
    assert proposal.app == "Google Chrome"


def test_a_failed_episode_is_chosen_before_re_running_a_success(
    tmp_path: Path,
) -> None:
    """A task that was attempted and lost beats one already known to work."""
    skills = SkillRegistry(tmp_path / "skills")
    skills.save(_skill("chrome.unproven", uses=1, wins=1))
    episodes = EpisodicStore(tmp_path / "episodes")
    episodes.record(
        episode_from_trace(
            app="Finder",
            description="empty the downloads folder listing",
            steps=(MouseClick(type="mouse_click", x=1, y=1),),
            step_descriptions=("click",),
            outcome="failure",
            retrospective="never found the folder",
        )
    )
    proposal = propose_goal(skills, episodes, rng=random.Random(0))
    assert proposal is not None
    assert proposal.goal == "empty the downloads folder listing"
    assert "failed" in proposal.reason


def test_an_unproven_skill_is_the_last_resort(tmp_path: Path) -> None:
    """Weakest of the three, but not worthless: it is how a skill used once
    earns a second data point."""
    skills = SkillRegistry(tmp_path / "skills")
    skills.save(_skill("chrome.unproven", uses=1, wins=1))
    proposal = propose_goal(
        skills, EpisodicStore(tmp_path / "episodes"), rng=random.Random(0)
    )
    assert proposal is not None
    assert isinstance(proposal, GoalProposal)
    assert "chrome.unproven" in proposal.reason


# --- the session ------------------------------------------------------------


def test_the_machine_is_waited_for_before_work_is_chosen() -> None:
    """Proposing first would mean deciding from a memory the user may be about
    to change, then acting minutes later on that stale decision."""
    order: list[str] = []

    run_autonomously(
        SessionLimits(max_runs=1, idle_seconds=2, rest_seconds=0),
        observe=lambda: order.append("observe") or _activity(5),
        propose=lambda: order.append("propose")
        or GoalProposal(goal="g", app=None, reason="r"),
        execute=lambda _p: order.append("execute"),
        stop=lambda: False,
        sleep=lambda _s: None,
    )
    assert order.index("observe") < order.index("propose") < order.index("execute")


def test_a_failing_run_ends_that_goal_not_the_session() -> None:
    """The point of running unattended is to accumulate attempts; the first
    goal being impossible is no reason to abandon the rest."""
    attempts: list[str] = []

    def execute(proposal: GoalProposal) -> None:
        attempts.append(proposal.goal)
        raise RuntimeError("that one was impossible")

    done = run_autonomously(
        SessionLimits(max_runs=3, idle_seconds=2, rest_seconds=0),
        observe=lambda: _activity(5),
        propose=lambda: GoalProposal(goal=f"g{len(attempts)}", app=None, reason="r"),
        execute=execute,
        stop=lambda: False,
        sleep=lambda _s: None,
    )
    assert done == 3
    assert attempts == ["g0", "g1", "g2"]


def test_a_session_stops_when_memory_runs_out_of_work() -> None:
    executed: list[str] = []
    done = run_autonomously(
        SessionLimits(max_runs=5, idle_seconds=2, rest_seconds=0),
        observe=lambda: _activity(5),
        propose=lambda: None,
        execute=lambda p: executed.append(p.goal),
        stop=lambda: False,
        sleep=lambda _s: None,
    )
    assert done == 0 and executed == []


def test_the_run_count_is_a_hard_ceiling() -> None:
    """An unattended process without a bound is not autonomy, it is a leak."""
    executed: list[str] = []
    done = run_autonomously(
        SessionLimits(max_runs=2, idle_seconds=2, rest_seconds=0),
        observe=lambda: _activity(5),
        propose=lambda: GoalProposal(goal="g", app=None, reason="r"),
        execute=lambda p: executed.append(p.goal),
        stop=lambda: False,
        sleep=lambda _s: None,
    )
    assert done == 2 and len(executed) == 2


def test_a_user_returning_to_the_machine_ends_the_session() -> None:
    """Never idle again means never acting again — the agent yields rather
    than competing for a cursor its user is holding."""
    executed: list[str] = []
    ticks = {"n": 0}

    def observe() -> MachineActivity:
        ticks["n"] += 1
        return _activity(ticks["n"])  # always moving

    done = run_autonomously(
        SessionLimits(max_runs=3, idle_seconds=2, rest_seconds=0),
        observe=observe,
        propose=lambda: GoalProposal(goal="g", app=None, reason="r"),
        execute=lambda p: executed.append(p.goal),
        stop=lambda: ticks["n"] > 10,
        sleep=lambda _s: None,
    )
    assert done == 0 and executed == []


def test_the_system_idle_clock_beats_the_proxy() -> None:
    """One reading of the real thing outweighs any number of samples of
    something that merely correlates with it.

    The cursor and window are a proxy that misses the case that matters most:
    someone typing without moving the mouse reads as absent, and an agent that
    starts clicking then is typing into their window rather than the target's.
    """
    typing = MachineActivity(cursor=(0.0, 0.0), frontmost="Notes", idle_seconds=2.0)
    assert machine_is_idle((typing,), required=4, threshold=60) is False

    away = MachineActivity(cursor=(0.0, 0.0), frontmost="Notes", idle_seconds=300.0)
    # One sample suffices: the clock already answers the whole question.
    assert machine_is_idle((away,), required=4, threshold=60) is True


def test_a_driver_without_the_clock_still_uses_the_proxy() -> None:
    """An older driver degrades to the heuristic rather than reporting the
    machine free, which would be the dangerous direction to fail in."""
    still = tuple(_activity(10) for _ in range(4))
    assert machine_is_idle(still, required=4, threshold=60) is True
    assert machine_is_idle(still[:1], required=4, threshold=60) is False
