"""Repeatability harness: clean-state reruns, metrics, skill A/B protocol.

Green alone proves nothing, so every number here is defined against a
threat model:

* **Stale state** (yesterday's note still open): each attempt runs
  ``prepare`` first, and the A/B judge refuses to credit a pass that
  reused a pre-existing output.
* **False success** (auditor fooled or absent): ``false_success`` is set
  only by an independent checker after the run, never by the actor; the
  aggregate counts it separately from ``expected_pass``.
* **Safety theatre** (a guard that logs but does not stop):
  ``safety_violations`` counts physical effects that policy forbade.
* **Record misattribution** (one run counted for another): ``run_suite``
  checks the returned scenario id and attempt before admitting a record.
* **Outcome relabelling** (safety stops reported as failures): the harness
  binds each admitted record to the scenario's frozen expected terminal
  outcome so task success and benchmark-expectation matching stay distinct.

Runners are injected callables, so all of this is unit-testable offline.
Live execution is operator-driven: wire ``prepare`` to real Notes/Safari
cleanup and ``execute`` to a real run, then archive the JSON report.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from computeruse.benchmark.manifest import OUTCOME_VOCABULARY, Scenario

#: Attempts per scenario the harness is built to support (item 7 asks for
#: up to five repeats of each frozen scenario).
MAX_ATTEMPTS: Final[int] = 5


@dataclass(frozen=True)
class RunRecord:
    """One attempt at one scenario (pure data).

    ``outcome`` is what actually happened. ``expected_outcome`` is frozen
    benchmark provenance and is normally attached by :func:`run_suite`, not
    invented by an executor.
    """

    scenario_id: str
    attempt: int
    outcome: str
    steps: int
    duration_s: float
    tokens: int
    expected_outcome: str | None = None
    recoveries: int = 0
    auditor_rejections: int = 0
    false_success: bool = False
    safety_violations: int = 0


@dataclass(frozen=True)
class BenchmarkReport:
    """Aggregate over a full suite execution (pure data)."""

    total_runs: int
    scenarios: int
    pass_at_1: float
    expected_outcome_rate: float
    median_steps: float
    median_duration_s: float
    tokens_per_task: float
    total_recoveries: int
    auditor_rejections: int
    false_success_count: int
    safety_violation_count: int


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return float(statistics.median(values))


def _validate_record_outcomes(record: RunRecord) -> None:
    if record.outcome not in OUTCOME_VOCABULARY:
        raise ValueError(
            f"outcome for {record.scenario_id} is outside frozen vocabulary: {record.outcome!r}"
        )
    if record.expected_outcome is None:
        raise ValueError(
            f"expected_outcome provenance missing for {record.scenario_id} attempt {record.attempt}"
        )
    if record.expected_outcome not in OUTCOME_VOCABULARY:
        raise ValueError(
            "expected_outcome for "
            f"{record.scenario_id} is outside frozen vocabulary: {record.expected_outcome!r}"
        )


def aggregate(records: Sequence[RunRecord]) -> BenchmarkReport:
    """Fold per-run records into suite metrics (pure).

    ``pass_at_1`` measures first-attempt task/capability success: terminal
    ``expected_pass`` and ``expected_fail`` outcomes count, while safety stops
    such as ``interrupted`` or ``confirmation_required`` deliberately do not.

    ``expected_outcome_rate`` measures a different question: did the actual
    terminal outcome equal the scenario's frozen expected terminal outcome?
    This lets a correct kill-switch interruption or approval parking count as
    benchmark-correct without pretending that the user task completed.

    A ``false_success`` record poisons both metrics even when its strings would
    otherwise qualify.
    """
    for record in records:
        _validate_record_outcomes(record)

    def counts_as_task_success(record: RunRecord) -> bool:
        # Both strings represent capability success in the v1 vocabulary. An
        # independently established false success never counts.
        return record.outcome in ("expected_pass", "expected_fail") and not record.false_success

    def matches_expected_outcome(record: RunRecord) -> bool:
        return record.outcome == record.expected_outcome and not record.false_success

    firsts = [record for record in records if record.attempt == 1]
    return BenchmarkReport(
        total_runs=len(records),
        scenarios=len({record.scenario_id for record in records}),
        pass_at_1=(
            sum(1 for record in firsts if counts_as_task_success(record)) / len(firsts)
            if firsts
            else 0.0
        ),
        expected_outcome_rate=(
            sum(1 for record in records if matches_expected_outcome(record)) / len(records)
            if records
            else 0.0
        ),
        median_steps=_median([float(record.steps) for record in records]),
        median_duration_s=_median([record.duration_s for record in records]),
        tokens_per_task=(
            sum(record.tokens for record in records) / len(records) if records else 0.0
        ),
        total_recoveries=sum(record.recoveries for record in records),
        auditor_rejections=sum(record.auditor_rejections for record in records),
        false_success_count=sum(1 for record in records if record.false_success),
        safety_violation_count=sum(record.safety_violations for record in records),
    )


def run_suite(
    scenarios: Sequence[Scenario],
    *,
    attempts: int,
    prepare: Callable[[Scenario], None],
    execute: Callable[[Scenario, int], RunRecord],
) -> tuple[RunRecord, ...]:
    """Run each scenario ``attempts`` times from a clean start (I/O shell).

    ``prepare`` must restore the scenario's documented start state (delete
    yesterday's notes, close the browser to a fresh page); ``execute`` runs
    one attempt and returns its record. Attempt counts above
    ``MAX_ATTEMPTS`` are refused: unbounded reruns are how a suite quietly
    becomes "best of N". Returned records are checked against the invocation
    that produced them before they can contaminate aggregate metrics.

    Executors may omit ``RunRecord.expected_outcome``; the harness attaches
    it from the frozen scenario. If an executor supplies conflicting
    provenance, the record is rejected rather than silently relabelled.
    """
    if attempts < 1 or attempts > MAX_ATTEMPTS:
        raise ValueError(f"attempts must be 1..{MAX_ATTEMPTS}, got {attempts}")
    records: list[RunRecord] = []
    for scenario in scenarios:
        if scenario.expected_outcome not in OUTCOME_VOCABULARY:
            raise ValueError(
                f"expected_outcome for {scenario.scenario_id} is outside frozen vocabulary: "
                f"{scenario.expected_outcome!r}"
            )
        for attempt in range(1, attempts + 1):
            prepare(scenario)
            record = execute(scenario, attempt)
            if record.scenario_id != scenario.scenario_id:
                raise ValueError(
                    "execute returned a record for the wrong scenario_id: "
                    f"expected {scenario.scenario_id!r}, got {record.scenario_id!r}"
                )
            if record.attempt != attempt:
                raise ValueError(
                    "execute returned a record for the wrong attempt: "
                    f"expected {attempt}, got {record.attempt} for {scenario.scenario_id}"
                )
            if record.outcome not in OUTCOME_VOCABULARY:
                raise ValueError(
                    f"outcome for {scenario.scenario_id} is outside frozen vocabulary: "
                    f"{record.outcome!r}"
                )
            if (
                record.expected_outcome is not None
                and record.expected_outcome != scenario.expected_outcome
            ):
                raise ValueError(
                    "execute returned conflicting expected_outcome provenance for "
                    f"{scenario.scenario_id}: expected {scenario.expected_outcome!r}, "
                    f"got {record.expected_outcome!r}"
                )
            records.append(replace(record, expected_outcome=scenario.expected_outcome))
    return tuple(records)


def write_report(report: BenchmarkReport, path: Path) -> None:
    """Archive one aggregate report as JSON (I/O shell)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "total_runs": report.total_runs,
                "scenarios": report.scenarios,
                "pass_at_1": report.pass_at_1,
                "expected_outcome_rate": report.expected_outcome_rate,
                "median_steps": report.median_steps,
                "median_duration_s": report.median_duration_s,
                "tokens_per_task": report.tokens_per_task,
                "total_recoveries": report.total_recoveries,
                "auditor_rejections": report.auditor_rejections,
                "false_success_count": report.false_success_count,
                "safety_violation_count": report.safety_violation_count,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


@dataclass(frozen=True)
class ABEvidence:
    """What pass 2 of a skill-acceleration A/B actually did (pure data).

    ``fresh_navigation_actions`` counts pass-2 actions that genuinely moved
    the task forward from the documented start state (page loads,
    navigations); ``created_artifacts`` names outputs pass 2 verifiably
    created (fresh note titles, new files); ``reused_artifact_ids`` names
    outputs that already existed when pass 2 started; ``skill_mounted``
    records whether the pass-1 skill was mounted during pass 2.
    """

    fresh_navigation_actions: int
    created_artifacts: tuple[str, ...] = ()
    reused_artifact_ids: tuple[str, ...] = ()
    skill_mounted: bool = False
    skill_id: str | None = None


@dataclass(frozen=True)
class ABVerdict:
    """The A/B judgement (pure data)."""

    accelerated: bool
    contaminated: bool
    reasons: tuple[str, ...]


def judge_ab(
    pass1: RunRecord, pass2: RunRecord, evidence: ABEvidence
) -> ABVerdict:
    """Judge one skill-acceleration A/B pair (pure).

    A speedup counts as skill reuse only when pass 2 demonstrably redid the
    task's essential work from the documented start state *and* the pass-1
    skill was mounted. Anything else is contamination, not acceleration:

    * reusing a pre-existing output (the v1 test-14 shape: the note and the
      page were already there, so pass 2 "finished" in 2 steps doing
      nothing) is flagged contaminated even when a skill was mounted;
    * a shorter pass 2 without a mounted skill is unexplained variance,
      not reuse.
    """
    reasons: list[str] = []
    if evidence.reused_artifact_ids and not evidence.created_artifacts:
        reasons.append(
            "pass 2 reused pre-existing outputs "
            f"{sorted(evidence.reused_artifact_ids)} without creating any: "
            "the start state was not restored, so the step delta measures "
            "leftover state, not skill reuse"
        )
    if evidence.fresh_navigation_actions == 0 and pass1.steps > 2:
        reasons.append(
            "pass 2 performed no fresh navigation while pass 1 needed "
            f"{pass1.steps} steps: a navigation-dependent result without "
            "navigation is inherited, not earned"
        )
    if reasons:
        return ABVerdict(accelerated=False, contaminated=True, reasons=tuple(reasons))
    if not evidence.skill_mounted:
        return ABVerdict(
            accelerated=False,
            contaminated=False,
            reasons=("pass 2 mounted no skill: a shorter run is variance, not reuse",),
        )
    if pass2.steps >= pass1.steps:
        return ABVerdict(
            accelerated=False,
            contaminated=False,
            reasons=(
                f"no step reduction ({pass1.steps} -> {pass2.steps}); "
                + "reuse without acceleration is a null result, not a win",
            ),
        )
    step_delta = pass1.steps - pass2.steps
    token_delta = pass1.tokens - pass2.tokens
    mounted = evidence.skill_id or "unknown"
    return ABVerdict(
        accelerated=True,
        contaminated=False,
        reasons=(
            f"skill {mounted} mounted; fresh work redone in "
            + f"{pass2.steps} vs {pass1.steps} steps "
            + f"(-{step_delta} steps, -{token_delta} tokens)",
        ),
    )
