"""Frozen benchmark: manifest validity, prompt freeze, harness math, A/B judge.

A benchmark that can drift silently is marketing, not measurement. These
tests pin the three load-bearing properties:

* the manifest holds 20 valid scenarios with exact prompts and checkable
  criteria, drawn from the fixed outcome vocabulary;
* the manifest hash is pinned: any prompt edit without a version bump
  fails loudly here instead of quietly redefining "20/20";
* the repeatability math and the skill-A/B contamination detector behave
  on synthetic records — including a reconstruction of the v1 test-14
  shape, which must be flagged contaminated, never celebrated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from computeruse.benchmark.harness import (
    ABEvidence,
    RunRecord,
    aggregate,
    judge_ab,
    run_suite,
)
from computeruse.benchmark.manifest import (
    OUTCOME_VOCABULARY,
    BenchmarkDriftError,
    assert_frozen,
    load_scenarios,
    manifest_hash,
)

MANIFEST_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "computeruse" / "benchmark" / "scenarios_v1.json"
)

#: Pinned by hand after review: bump only together with MANIFEST_VERSION and
#: a changelog entry saying which prompt changed and why.
FROZEN_MANIFEST_HASH = "4714b8fae877ba20fd20e992b57737d3102dd68ce377cc3e528770886274f225"


def _record(scenario_id: str = "s01", attempt: int = 1, **overrides: object) -> RunRecord:
    base: dict[str, object] = {
        "scenario_id": scenario_id,
        "attempt": attempt,
        "outcome": "expected_pass",
        "steps": 8,
        "duration_s": 60.0,
        "tokens": 80000,
    }
    base.update(overrides)
    return RunRecord(**base)  # type: ignore[arg-type]


def test_manifest_holds_twenty_valid_scenarios() -> None:
    scenarios = load_scenarios(MANIFEST_PATH)
    assert len(scenarios) == 20, "the frozen suite is twenty scenarios, not more, not fewer"
    assert len({scenario.scenario_id for scenario in scenarios}) == 20
    for scenario in scenarios:
        assert scenario.prompt.strip()
        assert scenario.expected_outcome in OUTCOME_VOCABULARY
        assert scenario.success_criteria, f"{scenario.scenario_id} has no checkable criteria"


def test_manifest_freeze_pin() -> None:
    """The hash pin is computed live here; FROZEN_MANIFEST_HASH documents it.

    If this fails, a prompt changed: either revert, or bump MANIFEST_VERSION
    with a changelog entry and update the pin below deliberately.
    """
    scenarios = load_scenarios(MANIFEST_PATH)
    assert_frozen(scenarios, FROZEN_MANIFEST_HASH)


def test_freeze_gate_fires_on_prompt_drift() -> None:
    """The gate is real: prove it rejects a stealth prompt edit."""
    scenarios = load_scenarios(MANIFEST_PATH)
    assert_frozen(scenarios, manifest_hash(scenarios))
    with pytest.raises(BenchmarkDriftError):
        assert_frozen(scenarios, "0" * 64)


def test_aggregate_reports_the_honest_metrics() -> None:
    records = (
        _record("s01", 1, steps=8, duration_s=60.0, tokens=80000),
        _record(
            "s01",
            2,
            steps=10,
            duration_s=80.0,
            tokens=100000,
            recoveries=2,
            auditor_rejections=1,
        ),
        _record("s02", 1, steps=6, duration_s=50.0, tokens=60000, false_success=True),
    )
    report = aggregate(records)
    assert report.total_runs == 3
    # pass@1 counts first attempts only: s01a1 passes, s02a1 is a false
    # success, so 1/2 — the false success poisons the rate even though its
    # outcome string claims a pass.
    assert report.pass_at_1 == pytest.approx(1 / 2)
    assert report.expected_outcome_rate == pytest.approx(2 / 3)
    assert report.median_steps == 8.0
    assert report.median_duration_s == 60.0
    assert report.tokens_per_task == pytest.approx(240000 / 3)
    assert report.total_recoveries == 2
    assert report.auditor_rejections == 1
    assert report.false_success_count == 1
    assert report.safety_violation_count == 0


def test_suite_refuses_best_of_n() -> None:
    with pytest.raises(ValueError):
        run_suite((), attempts=6, prepare=lambda _s: None, execute=lambda _s, _a: _record())


def test_suite_restores_start_state_before_every_attempt() -> None:
    prepared: list[str] = []
    scenarios = load_scenarios(MANIFEST_PATH)[:2]
    records = run_suite(
        scenarios,
        attempts=2,
        prepare=lambda scenario: prepared.append(scenario.scenario_id),
        execute=lambda scenario, attempt: _record(scenario.scenario_id, attempt),
    )
    assert len(records) == 4
    assert prepared == ["s01", "s01", "s02", "s02"]


def test_v1_test14_shape_is_contamination_not_acceleration() -> None:
    """Reconstruction of the v1 test-14 logs: pass 1 did 8 steps of fresh
    work; pass 2 "finished" in 2 steps with the page already open and the
    note already present, creating nothing. Celebrating the 8-to-2 delta as
    skill acceleration measured leftover state. The judge must refuse."""
    pass1 = _record("s14", 1, steps=8, tokens=86504)
    pass2 = _record("s14", 2, steps=2, tokens=28352)
    verdict = judge_ab(
        pass1,
        pass2,
        ABEvidence(
            fresh_navigation_actions=0,
            created_artifacts=(),
            reused_artifact_ids=("Computer Vision Bölüm Raporu",),
            skill_mounted=True,
            skill_id="safari.3df676189042ff8a",
        ),
    )
    assert verdict.contaminated is True
    assert verdict.accelerated is False
    assert any("leftover state" in reason for reason in verdict.reasons)


def test_clean_ab_with_mounted_skill_counts_as_reuse() -> None:
    verdict = judge_ab(
        _record("s14", 1, steps=8, tokens=86504),
        _record("s14", 2, steps=4, tokens=40000),
        ABEvidence(
            fresh_navigation_actions=3,
            created_artifacts=("Computer Vision Bölüm Raporu (pass 2)",),
            reused_artifact_ids=(),
            skill_mounted=True,
            skill_id="safari.e9862a5c98ee0f78",
        ),
    )
    assert verdict.accelerated is True
    assert verdict.contaminated is False
    assert any("mounted" in reason for reason in verdict.reasons)


def test_shorter_pass_without_a_skill_is_variance_not_reuse() -> None:
    verdict = judge_ab(
        _record("s14", 1, steps=8, tokens=86504),
        _record("s14", 2, steps=3, tokens=30000),
        ABEvidence(fresh_navigation_actions=3, skill_mounted=False),
    )
    assert verdict.accelerated is False
    assert verdict.contaminated is False
