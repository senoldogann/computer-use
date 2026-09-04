"""Layer 7: the battery that scores the agent instead of trusting it.

"Is the agent getting better?" has no answer without an instrument. Skill
uses/wins counters are binary, coarse and blind to *which* surface
regressed — a grounding drift and a permission false-positive both read as
"a skill failed". The eval battery is twelve fixed offline tasks over the
four surfaces the agent has actually bled on, and this module pins the
instrument itself: the counting, the rendering, the run_id join, the
sidecar store, and the fact that the battery passes against this tree.

Nothing here needs the driver socket or a model. That is the point: the
battery must hold in CI on a displayless machine, so every check runs
against pure classifiers with fixture input.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from computeruse.eval.runner import build_record, run_all
from computeruse.eval.score import (
    TaskResult,
    join_usage,
    render,
    render_json,
    summarize,
    usage_for_run,
)
from computeruse.eval.store import BenchmarkRecord, BenchmarkStore, new_benchmark_id
from computeruse.eval.tasks import (
    BATTERY_VERSION,
    TASK_BATTERY,
    BatteryTask,
    CheckOutcome,
    task_ids,
    tasks_in,
)
from computeruse.memory.episodic import EpisodicStore, episode_from_trace
from computeruse.memory.schemas import Episode
from computeruse.orchestrator.report import UsageRecord
from computeruse.orchestrator.schemas import MouseClick

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _result(task_id: str, category: str, passed: bool, score: float) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        category=category,  # type: ignore[arg-type]
        title=task_id,
        passed=passed,
        score=score,
        steps=1,
        tokens=0,
        cost_usd=0.0,
        stuck_loops=0,
        first_try=passed,
        detail="fixture",
    )


def _usage(run_id: str, tokens: int) -> UsageRecord:
    return UsageRecord(
        run_id=run_id,
        goal="g",
        app="Safari",
        outcome="success",
        steps=1,
        total_tokens=tokens,
        cost_usd=0.0,
        elapsed_seconds=1.0,
        recorded_at=NOW,
    )


# --- the instrument's counting -------------------------------------------------


def test_summarize_counts_passes_not_just_a_ratio() -> None:
    """A single pass rate hides which surface regressed; the summary keeps both."""
    summary = summarize(
        battery_version=BATTERY_VERSION,
        run_id="run-1",
        results=(
            _result("grounding.a", "grounding", True, 1.0),
            _result("grounding.b", "grounding", False, 0.5),
            _result("permission.a", "permission", True, 1.0),
        ),
    )
    assert (summary.passed_count, summary.total) == (2, 3)
    assert summary.pass_rate == pytest.approx(2 / 3)
    assert summary.mean_score == pytest.approx((1.0 + 0.5 + 1.0) / 3)
    assert summary.total_steps == 3


def test_breakdown_follows_vocabulary_order_not_input_order() -> None:
    """Two runs of one battery must render identically whatever order ran."""
    summary = summarize(
        battery_version=BATTERY_VERSION,
        run_id="run-1",
        results=(
            _result("report.a", "report", True, 1.0),
            _result("grounding.a", "grounding", False, 0.0),
        ),
    )
    assert [(p.category, p.passed, p.total) for p in summary.by_category()] == [
        ("grounding", 0, 1),
        ("report", 1, 1),
    ]


def test_render_names_failures_with_their_reason() -> None:
    """A red line without the detail is unactionable; the detail is the point."""
    summary = summarize(
        battery_version=BATTERY_VERSION,
        run_id="run-1",
        results=(_result("permission.a", "permission", False, 0.0),),
    )
    text = render(summary)
    assert "0/1 passed" in text
    assert "permission" in text
    assert "[permission.a]" in text


def test_render_stays_quiet_about_failures_when_green() -> None:
    """A green battery prints the breakdown, not an empty failure section."""
    summary = summarize(
        battery_version=BATTERY_VERSION,
        run_id="run-1",
        results=(_result("grounding.a", "grounding", True, 1.0),),
    )
    assert "failed" not in render(summary)


def test_json_rendering_holds_records_never_prose() -> None:
    """Machines read the JSON sibling; it must parse and carry the records."""
    summary = summarize(
        battery_version=BATTERY_VERSION,
        run_id="run-9",
        results=(_result("grounding.a", "grounding", True, 1.0),),
    )
    payload = json.loads(render_json(summary))
    assert payload["run_id"] == "run-9"
    assert payload["passed"] == 1
    assert payload["results"][0]["task_id"] == "grounding.a"


# --- the run_id join (Engel 1) ---------------------------------------------------


def test_episodes_join_usage_by_run_id() -> None:
    """What happened meets what it cost — the join the scorer exists for."""
    episode = Episode(
        episode_id="e1",
        app="Safari",
        description="d",
        steps=(MouseClick(type="mouse_click", x=1, y=1),),
        outcome="success",
        signature="s",
        run_id="run-7",
        recorded_at=NOW,
    )
    joined = join_usage((episode,), (_usage("run-7", 120),))
    assert joined["e1"] is not None
    assert joined["e1"].total_tokens == 120


def test_legacy_episodes_without_run_id_join_to_none_not_to_error() -> None:
    """History recorded before the join key existed still reads (Engel 1)."""
    legacy = Episode.model_validate(
        {
            "episode_id": "e0",
            "app": "Safari",
            "description": "an old run",
            "steps": [],
            "outcome": "success",
            "signature": "s",
            "recorded_at": NOW.isoformat(),
        }
    )
    assert legacy.run_id is None
    assert join_usage((legacy,), (_usage("run-7", 120),)) == {"e0": None}


def test_usage_without_a_matching_episode_is_not_an_error_either() -> None:
    """Counters lost with the terminal leave usage no episode claims."""
    episode = Episode(
        episode_id="e1",
        app="Safari",
        description="d",
        steps=(),
        outcome="success",
        signature="s",
        run_id="run-missing",
        recorded_at=NOW,
    )
    assert join_usage((episode,), (_usage("run-7", 120),)) == {"e1": None}


def test_usage_for_run_selects_one_run() -> None:
    assert [r.run_id for r in usage_for_run((_usage("a", 1), _usage("b", 2)), run_id="b")] == ["b"]


def test_episode_factory_carries_the_join_key() -> None:
    """New episodes leave the trace already joined (Engel 1, write side)."""
    episode = episode_from_trace(
        app="Safari",
        description="d",
        steps=(MouseClick(type="mouse_click", x=1, y=1),),
        outcome="success",
        run_id="run-3",
    )
    assert episode.run_id == "run-3"


# --- the sidecar store (Engel 2) -------------------------------------------------


def _record(benchmark_id: str) -> BenchmarkRecord:
    return build_record(
        benchmark_id=benchmark_id,
        battery_version=BATTERY_VERSION,
        run_id="run-1",
        started_at=NOW,
        finished_at=NOW,
        results=(_result("grounding.a", "grounding", True, 1.0),),
    )


def test_benchmark_roundtrips_through_its_store(tmp_path: Path) -> None:
    store = BenchmarkStore(tmp_path / "benchmarks")
    store.save(_record("eval.20260904t120000000000"))
    assert store.load("eval.20260904t120000000000").pass_rate == 1.0


def test_benchmark_save_refreshes_rather_than_refusing(tmp_path: Path) -> None:
    """Scores are snapshots, not history: re-running refreshes the reading.

    This is the deliberate difference from EpisodicStore, which refuses to
    clobber — history must never be overwritten, but a score re-run with a
    fixed id is a refresh, and failing it would force id bookkeeping onto
    every CI invocation for no safety gain.
    """
    store = BenchmarkStore(tmp_path / "benchmarks")
    store.save(_record("eval.fixed"))
    assert store.save(_record("eval.fixed")).name == "eval.fixed.json"
    assert len(store.records()) == 1


def test_episode_store_still_refuses_to_clobber(tmp_path: Path) -> None:
    """The sidecar exists so this refusal never has to weaken (Engel 2)."""
    episodes = EpisodicStore(tmp_path / "episodes")
    episode = episode_from_trace(
        app="Safari",
        description="d",
        steps=(MouseClick(type="mouse_click", x=1, y=1),),
        outcome="success",
        episode_id="safari.fixed",
    )
    episodes.record(episode)
    with pytest.raises(FileExistsError):
        episodes.record(episode)


def test_missing_benchmark_dir_reads_empty_not_missing(tmp_path: Path) -> None:
    assert BenchmarkStore(tmp_path / "benchmarks").records() == ()


def test_corrupt_benchmark_file_hides_nothing_else(tmp_path: Path) -> None:
    """One corrupt score must not hide every other reading behind it."""
    directory = tmp_path / "benchmarks"
    directory.mkdir()
    (directory / "eval.broken.json").write_text("{not json", encoding="utf-8")
    store = BenchmarkStore(directory)
    store.save(_record("eval.good"))
    assert [r.benchmark_id for r in store.records()] == ["eval.good"]


def test_benchmark_ids_sort_chronologically() -> None:
    assert new_benchmark_id(NOW).startswith("eval.20260904t120000")


# --- the runner ------------------------------------------------------------------


def test_the_battery_passes_against_this_tree() -> None:
    """The regression gate itself: twelve tasks, all green on this tree.

    If someone reintroduces the "drop" false positive, weakens the prose
    exemption, or drifts the mark geometry, this is the test that goes red —
    before any live run has to bleed for the same lesson twice.
    """
    results = run_all(TASK_BATTERY)
    assert len(results) == 12
    assert {r.category for r in results} == {"grounding", "permission", "recovery", "report"}
    failed = [r.task_id for r in results if not r.passed]
    assert failed == []


def test_one_rotten_task_blinds_nothing_else() -> None:
    """The instrument stays up when a single probe breaks — it measures that."""
    def _boom() -> CheckOutcome:
        raise RuntimeError("probe wiring broke")

    rotten = BatteryTask(
        task_id="grounding.rotten",
        category="grounding",
        title="a broken probe",
        rationale="fixture",
        check=_boom,
    )
    (failed, nxt) = run_all((rotten, TASK_BATTERY[0]))
    assert not failed.passed
    assert "RuntimeError" in failed.detail
    assert not failed.first_try
    assert nxt.passed


def test_category_filter_rejects_unknown_names_loudly() -> None:
    """A misspelled --eval-only must fail, not silently score a subset."""
    with pytest.raises(ValueError, match="unknown eval categories"):
        tasks_in(TASK_BATTERY, ("grounding", "telepathy"))


def test_category_filter_keeps_battery_order() -> None:
    assert [t.task_id for t in tasks_in(TASK_BATTERY, ("report",))] == [
        "report.counts",
        "report.usage-join",
    ]


def test_battery_ids_are_unique_and_ordered() -> None:
    ids = task_ids(TASK_BATTERY)
    assert len(set(ids)) == len(ids) == 12
