"""Regression tests for honest benchmark outcome scoring."""

from __future__ import annotations

import pytest

from computeruse.benchmark.harness import RunRecord, aggregate, run_suite
from computeruse.benchmark.manifest import Scenario


def _raw_record(
    *,
    scenario_id: str = "s01",
    attempt: int = 1,
    outcome: str = "expected_pass",
    **extra: object,
) -> RunRecord:
    return RunRecord(
        scenario_id=scenario_id,
        attempt=attempt,
        outcome=outcome,
        steps=4,
        duration_s=10.0,
        tokens=1000,
        **extra,  # type: ignore[arg-type]
    )


def _scenario(scenario_id: str, expected_outcome: str) -> Scenario:
    return Scenario(
        scenario_id=scenario_id,
        name=f"Scenario {scenario_id}",
        category="Scoring regression",
        difficulty="1/10",
        prompt="exercise the scoring contract",
        start_state="clean",
        expected_outcome=expected_outcome,
        success_criteria=("terminal outcome is independently verified",),
    )


def test_expected_outcome_rate_counts_correct_safety_terminal_states() -> None:
    records = (
        _raw_record(
            scenario_id="s15",
            outcome="interrupted",
            expected_outcome="interrupted",
        ),
        _raw_record(
            scenario_id="s18",
            outcome="confirmation_required",
            expected_outcome="confirmation_required",
        ),
    )

    report = aggregate(records)

    # Correct safety termination is benchmark-correct, but it is deliberately
    # not task-completion success. These two metrics must not collapse into one.
    assert report.expected_outcome_rate == 1.0
    assert report.pass_at_1 == 0.0


def test_expected_outcome_rate_detects_terminal_mismatch() -> None:
    records = (
        _raw_record(
            scenario_id="s15",
            outcome="expected_pass",
            expected_outcome="interrupted",
        ),
    )

    report = aggregate(records)

    assert report.expected_outcome_rate == 0.0
    assert report.pass_at_1 == 1.0


def test_false_success_poisoning_applies_to_expected_outcome_match() -> None:
    record = _raw_record(
        outcome="expected_pass",
        expected_outcome="expected_pass",
        false_success=True,
    )

    report = aggregate((record,))

    assert report.pass_at_1 == 0.0
    assert report.expected_outcome_rate == 0.0


def test_aggregate_refuses_records_without_expected_outcome_provenance() -> None:
    record = _raw_record(outcome="expected_pass")

    with pytest.raises(ValueError, match="expected_outcome"):
        aggregate((record,))


def test_run_suite_attaches_manifest_expected_outcome() -> None:
    scenario = _scenario("s15", "interrupted")

    records = run_suite(
        (scenario,),
        attempts=1,
        prepare=lambda _scenario: None,
        execute=lambda current, attempt: _raw_record(
            scenario_id=current.scenario_id,
            attempt=attempt,
            outcome="interrupted",
        ),
    )

    assert records[0].expected_outcome == "interrupted"
    assert aggregate(records).expected_outcome_rate == 1.0


def test_run_suite_rejects_conflicting_executor_expectation() -> None:
    scenario = _scenario("s18", "confirmation_required")

    with pytest.raises(ValueError, match="expected_outcome"):
        run_suite(
            (scenario,),
            attempts=1,
            prepare=lambda _scenario: None,
            execute=lambda current, attempt: _raw_record(
                scenario_id=current.scenario_id,
                attempt=attempt,
                outcome="confirmation_required",
                expected_outcome="expected_pass",
            ),
        )


def test_aggregate_rejects_outcomes_outside_frozen_vocabulary() -> None:
    record = _raw_record(
        outcome="totally_custom_success",
        expected_outcome="expected_pass",
    )

    with pytest.raises(ValueError, match="outcome"):
        aggregate((record,))
