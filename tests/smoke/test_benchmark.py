"""Frozen benchmark: manifest validity, semantic freeze, harness math, A/B judge.

A benchmark that can drift silently is marketing, not measurement. These
tests pin the load-bearing properties:

* the manifest holds 20 valid scenarios with exact, checkable semantics;
* the manifest hash covers the complete scenario contract, not just prompts;
* malformed/version-mismatched manifests fail closed;
* repeatability records cannot be attributed to the wrong scenario/attempt;
* the repeatability math and skill-A/B contamination detector behave on
  synthetic records.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from computeruse.benchmark.harness import (
    ABEvidence,
    RunRecord,
    aggregate,
    judge_ab,
    run_suite,
)
from computeruse.benchmark.manifest import (
    MANIFEST_VERSION,
    OUTCOME_VOCABULARY,
    BenchmarkDriftError,
    assert_frozen,
    load_scenarios,
    manifest_hash,
)

MANIFEST_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "computeruse" / "benchmark" / "scenarios_v1.json"
)

#: Pinned by hand after review. Updating the hash is allowed only when the
#: complete frozen manifest semantics were intentionally reviewed. A change to
#: the hashing implementation itself may update the pin without changing
#: MANIFEST_VERSION only when scenario data remains byte-for-byte unchanged.
FROZEN_MANIFEST_HASH = "bbae3e286fc32e6ba5a1fc261665b0c3d53e12384c971a7602f99d9c02f7fc05"


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


def _manifest_payload() -> dict[str, object]:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return cast(dict[str, object], raw)


def _write_manifest(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "scenarios.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _scenario_entries(payload: dict[str, object]) -> list[object]:
    entries = payload.get("scenarios")
    assert isinstance(entries, list)
    return cast(list[object], entries)


def _first_scenario(payload: dict[str, object]) -> dict[str, object]:
    entries = _scenario_entries(payload)
    assert entries and isinstance(entries[0], dict)
    return cast(dict[str, object], entries[0])


def test_manifest_holds_twenty_valid_scenarios() -> None:
    scenarios = load_scenarios(MANIFEST_PATH)
    assert len(scenarios) == 20, "the frozen suite is twenty scenarios, not more, not fewer"
    assert len({scenario.scenario_id for scenario in scenarios}) == 20
    for scenario in scenarios:
        assert scenario.name.strip()
        assert scenario.category.strip()
        assert scenario.difficulty.strip()
        assert scenario.prompt.strip()
        assert scenario.start_state.strip()
        assert scenario.expected_outcome in OUTCOME_VOCABULARY
        assert scenario.success_criteria, f"{scenario.scenario_id} has no checkable criteria"


def test_manifest_freeze_pin() -> None:
    """The pin covers complete benchmark semantics, not merely prompt text."""
    scenarios = load_scenarios(MANIFEST_PATH)
    actual = manifest_hash(scenarios)
    assert actual == FROZEN_MANIFEST_HASH, actual
    assert_frozen(scenarios, FROZEN_MANIFEST_HASH)


def test_freeze_gate_fires_on_hash_mismatch() -> None:
    scenarios = load_scenarios(MANIFEST_PATH)
    assert_frozen(scenarios, manifest_hash(scenarios))
    with pytest.raises(BenchmarkDriftError):
        assert_frozen(scenarios, "0" * 64)


def test_manifest_hash_covers_complete_scenario_semantics() -> None:
    """Moving the goalposts must invalidate the frozen benchmark pin."""
    scenarios = load_scenarios(MANIFEST_PATH)
    first = scenarios[0]
    original = manifest_hash(scenarios)
    variants = (
        replace(first, name=first.name + " changed"),
        replace(first, category=first.category + " changed"),
        replace(first, difficulty="1/10"),
        replace(first, prompt=first.prompt + " changed"),
        replace(first, start_state=first.start_state + " changed"),
        replace(first, expected_outcome="blocked"),
        replace(first, success_criteria=("much easier criterion",)),
        replace(first, live_only=not first.live_only),
        replace(first, notes=first.notes + " changed"),
    )
    for variant in variants:
        changed = (variant, *scenarios[1:])
        assert manifest_hash(changed) != original, variant


def test_manifest_version_must_match_code_contract(tmp_path: Path) -> None:
    payload = _manifest_payload()
    payload["manifest_version"] = f"{MANIFEST_VERSION}-stealth-edit"
    with pytest.raises(BenchmarkDriftError, match="manifest_version"):
        load_scenarios(_write_manifest(tmp_path, payload))


def test_manifest_rejects_non_object_scenario_entries(tmp_path: Path) -> None:
    payload = _manifest_payload()
    _scenario_entries(payload).append("silently-skipped-today")
    with pytest.raises(BenchmarkDriftError, match="scenario entry"):
        load_scenarios(_write_manifest(tmp_path, payload))


@pytest.mark.parametrize("field", ["name", "category", "difficulty", "start_state"])
def test_manifest_requires_execution_metadata(tmp_path: Path, field: str) -> None:
    payload = _manifest_payload()
    _first_scenario(payload)[field] = ""
    with pytest.raises(BenchmarkDriftError, match=field):
        load_scenarios(_write_manifest(tmp_path, payload))


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
    # success, so 1/2. The false success poisons the rate even though its
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


def test_suite_rejects_record_for_wrong_scenario() -> None:
    scenario = load_scenarios(MANIFEST_PATH)[0]
    with pytest.raises(ValueError, match="scenario_id"):
        run_suite(
            (scenario,),
            attempts=1,
            prepare=lambda _scenario: None,
            execute=lambda _scenario, attempt: _record("wrong-scenario", attempt),
        )


def test_suite_rejects_record_for_wrong_attempt() -> None:
    scenario = load_scenarios(MANIFEST_PATH)[0]
    with pytest.raises(ValueError, match="attempt"):
        run_suite(
            (scenario,),
            attempts=1,
            prepare=lambda _scenario: None,
            execute=lambda current, attempt: _record(current.scenario_id, attempt + 1),
        )


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
