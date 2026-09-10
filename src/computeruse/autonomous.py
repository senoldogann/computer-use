"""Running without being asked.

An agent that only ever acts on instruction learns only what it is told to do.
This module lets it choose its own work — which is a different and much more
serious thing than executing someone else's, because nobody is watching when it
gets the choice wrong.

Three properties make that defensible, and none of them is a restriction bolted
on afterwards:

*It waits for the machine to be free.* Not a courtesy — a synthetic click goes
to whatever is frontmost, so an agent acting while its user types is not
sharing the computer, it is corrupting whatever they were doing.

*It proposes goals from concrete evidence, not imagination.* A failed episode
or a skill whose track record needs repair is specific, checkable work. "Do
something useful" is not, and an agent asked to invent work from nothing will
invent it from nothing.

*It stops.* Every run carries the same wall-clock, token and spend ceilings a
supervised one does, and the session itself has a run count. An unattended
process without a bound is not autonomy, it is a leak.

The permission guard is untouched. The scheduler decides what is worth trying;
it never decides whether an action may execute. FULL, Sovereign, grants,
approvals and the runtime safety floor remain separate policy layers.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Container
from dataclasses import dataclass
from typing import Final

from computeruse.memory.episodic import EpisodicStore
from computeruse.orchestrator.budget import BudgetExceededError
from computeruse.orchestrator.loop import KillSwitchTripped
from computeruse.orchestrator.report import UsageRecord
from computeruse.scheduler import (
    GoalProposal,
    estimate_expected_cost,
    make_proposal,
    rank_proposals,
)
from computeruse.skills.registry import SkillRegistry, is_demoted

LOGGER: Final = logging.getLogger(__name__)

#: How still the machine must be before the agent treats it as free. Long
#: enough that a pause for thought is not mistaken for absence.
DEFAULT_IDLE_SECONDS: Final[float] = 120.0
#: How often to look. Cheap — one focused-window probe.
IDLE_POLL_SECONDS: Final[float] = 15.0
#: How long to wait after a run before considering another, so a session of
#: unattended work does not become a hot loop on someone's laptop.
DEFAULT_REST_SECONDS: Final[float] = 300.0


@dataclass(frozen=True)
class MachineActivity:
    """One observation of whether a human is using the machine (pure data).

    ``idle_seconds`` is the direct answer when the driver can give it: the HID
    system's own clock since the last input of any kind. The cursor and window
    are the fallback for a driver that cannot, and they are a *proxy* — they
    miss someone typing without moving the mouse, which is exactly the person
    an agent must not start clicking around.
    """

    cursor: tuple[float, float]
    frontmost: str
    idle_seconds: float | None = None


def machine_is_idle(
    samples: tuple[MachineActivity, ...], *, required: int, threshold: float
) -> bool:
    """Has the machine been untouched (pure)?

    Prefers the measurement over the proxy. When the newest observation carries
    the system's own idle clock, that clock answers the question directly and
    completely — it counts keystrokes as well as movement, and one reading is
    worth any number of samples of something that merely correlates.

    Without it, both proxies have to hold still across every sample: the cursor
    alone misses someone reading, and the frontmost window alone misses someone
    working inside one. Neither catches someone typing, which is why the
    measurement is preferred wherever it exists.
    """
    if not samples:
        return False
    newest = samples[-1]
    if newest.idle_seconds is not None:
        return newest.idle_seconds >= threshold
    if len(samples) < required:
        return False
    recent = samples[-required:]
    first = recent[0]
    return all(
        sample.cursor == first.cursor and sample.frontmost == first.frontmost
        for sample in recent
    )


def propose_goal(
    skills: SkillRegistry,
    episodes: EpisodicStore,
    *,
    usage: tuple[UsageRecord, ...] = (),
    exclude: Container[str],
) -> GoalProposal | None:
    """Rank grounded memory work and return the strongest candidate.

    All eligible memory sources enter one candidate set. Source priority lives
    in the scheduler's wide utility bands, while confidence and recorded cost
    order peers inside those bands. Identical stores therefore produce the same
    proposal without an ambient random choice.

    ``exclude`` is applied before proposal construction. A parked or already
    handled goal cannot win a score and then erase another legitimate candidate
    merely because it happened to be considered first.

    Returns ``None`` when memory has nothing grounded to say. An agent with no
    evidence-backed work should do nothing, not invent a task.
    """
    candidates: list[GoalProposal] = []

    for summary in skills.index():
        if summary.description in exclude:
            continue
        expected_cost = estimate_expected_cost(summary.description, usage)
        if is_demoted(summary):
            candidates.append(
                make_proposal(
                    goal=summary.description,
                    app=summary.app,
                    source_type="skill_repair",
                    source_id=summary.skill_id,
                    confidence=min(1.0, summary.uses / 5.0),
                    expected_cost=expected_cost,
                    reason=(
                        f"skill {summary.skill_id} has failed {summary.uses} times "
                        "and never worked; retrying it tests whether the recipe "
                        "or the observed screen was wrong"
                    ),
                )
            )
        elif summary.uses <= 1:
            candidates.append(
                make_proposal(
                    goal=summary.description,
                    app=summary.app,
                    source_type="skill_validation",
                    source_id=summary.skill_id,
                    confidence=0.50 + 0.10 * min(summary.uses, 1),
                    expected_cost=expected_cost,
                    reason=(
                        f"skill {summary.skill_id} has been used {summary.uses} "
                        "time(s); another verified run adds evidence to its record"
                    ),
                )
            )

    for episode in episodes.episodes():
        if (
            episode.outcome != "failure"
            or not episode.description
            or episode.description in exclude
        ):
            continue
        candidates.append(
            make_proposal(
                goal=episode.description,
                app=episode.app,
                source_type="episode_retry",
                source_id=episode.episode_id,
                confidence=0.75,
                expected_cost=estimate_expected_cost(episode.description, usage),
                reason=(
                    f"episode {episode.episode_id} failed; retrying it is "
                    "grounded work with a recorded failure to improve"
                ),
            )
        )

    ranked = rank_proposals(tuple(candidates))
    return ranked[0] if ranked else None


def wait_for_idle(
    observe: Callable[[], MachineActivity],
    *,
    idle_seconds: float,
    poll_seconds: float = IDLE_POLL_SECONDS,
    stop: Callable[[], bool],
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Block until the machine is free, or until told to stop.

    Returns whether it became idle. ``stop`` is checked every poll rather than
    only at the end: a user who reaches for the kill switch during a two-minute
    wait should not have to wait out the timer.
    """
    required = max(2, int(idle_seconds / poll_seconds))
    samples: list[MachineActivity] = []
    while not stop():
        try:
            samples.append(observe())
        except Exception as exc:  # noqa: BLE001 - a blind probe means not idle
            LOGGER.debug("idle probe failed: %s", exc)
            samples.clear()
        if machine_is_idle(
            tuple(samples), required=required, threshold=idle_seconds
        ):
            return True
        if len(samples) > required:
            samples = samples[-required:]
        sleep(poll_seconds)
    return False


@dataclass(frozen=True)
class SessionLimits:
    """What bounds an unattended session (pure data).

    Every field is required. An unattended process without a bound is not
    autonomy, it is a leak, and defaults would let one be created by omission.
    """

    max_runs: int
    idle_seconds: float
    rest_seconds: float


def run_autonomously(
    limits: SessionLimits,
    *,
    observe: Callable[[], MachineActivity],
    propose: Callable[[], GoalProposal | None],
    execute: Callable[[GoalProposal], None],
    stop: Callable[[], bool],
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Choose work and do it, until the session's bounds are reached.

    Returns how many goals were attempted. The order is deliberate: wait for
    the machine, *then* choose. Proposing first would mean deciding what to do
    from a memory that the user may be about to change, and then acting on a
    decision made minutes ago.

    A failing run ends that goal, not the session. The point of running
    unattended is to accumulate attempts, and the first goal proposed being
    impossible is not a reason to stop trying the rest.

    Two things end everything instead. A human reclaiming the machine, and the
    session's budget running out — the ceiling belongs to the session, so the
    next goal would exceed it before taking a step.
    ``KillSwitchTripped`` used to be swallowed by the same handler that
    forgives an impossible goal, so tripping the emergency hotkey stopped one
    run and the session took the cursor back after resting — the precise
    inversion of what a kill switch promises.
    """
    attempted = 0
    while attempted < limits.max_runs and not stop():
        if not wait_for_idle(
            observe,
            idle_seconds=limits.idle_seconds,
            stop=stop,
            sleep=sleep,
        ):
            break
        proposal = propose()
        if proposal is None:
            LOGGER.info("autonomous: memory has nothing to work on; stopping")
            break
        LOGGER.info(
            "autonomous run %d/%d: %r (%s)",
            attempted + 1,
            limits.max_runs,
            proposal.goal,
            proposal.reason,
        )
        attempted += 1
        try:
            execute(proposal)
        except KillSwitchTripped:
            LOGGER.warning("autonomous: human reclaimed control; ending session")
            raise
        except BudgetExceededError:
            # The ceiling is the session's, so the next goal would exceed it
            # immediately; continuing would spend the run count on runs that
            # cannot start.
            LOGGER.warning("autonomous: budget exhausted; ending session")
            raise
        except Exception as exc:  # noqa: BLE001 - one goal failing is not the session failing
            LOGGER.warning("autonomous run failed: %s", exc)
        if attempted < limits.max_runs and not stop():
            # Rest between runs so a session of unattended work does not become
            # a hot loop on someone's laptop.
            sleep(limits.rest_seconds)
    return attempted
