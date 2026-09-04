"""Running the battery: the thin dirty shell around pure checks.

Everything that can be pure already is — each task's ``check`` takes
nothing, touches nothing, and returns a :class:`CheckOutcome`. What remains
is execution itself: calling twelve functions, and deciding what a raise
means. The answer is that a raising task fails *itself*: the battery is an
instrument, and an instrument that goes blind because one probe broke
measures nothing at all.

No clock and no I/O here either, so this stays testable without fixtures:
the CLI stamps the record's timestamps and writes it to the sidecar store.
"""

from __future__ import annotations

from datetime import datetime

from computeruse.eval.score import TaskResult
from computeruse.eval.store import BenchmarkRecord
from computeruse.eval.tasks import BatteryTask


def run_all(tasks: tuple[BatteryTask, ...]) -> tuple[TaskResult, ...]:
    """Execute every task in order, containing each task's failure (pure).

    A check that raises records a failed result carrying the exception's
    type and message — the same "carry the real reason" rule the failure
    taxonomy follows — and the battery continues with the next task.
    """
    results: list[TaskResult] = []
    for task in tasks:
        try:
            outcome = task.check()
        except Exception as exc:  # noqa: BLE001 - one rotten probe must not blind the battery
            outcome_detail = f"check raised {type(exc).__name__}: {exc}"
            results.append(
                TaskResult(
                    task_id=task.task_id,
                    category=task.category,
                    title=task.title,
                    passed=False,
                    score=0.0,
                    steps=0,
                    tokens=0,
                    cost_usd=0.0,
                    stuck_loops=0,
                    first_try=False,
                    detail=outcome_detail,
                )
            )
            continue
        results.append(
            TaskResult(
                task_id=task.task_id,
                category=task.category,
                title=task.title,
                passed=outcome.passed,
                score=outcome.score,
                steps=outcome.steps,
                # Offline by construction: no model call, no loop, one
                # attempt. Carried so the record shape survives live tasks.
                tokens=0,
                cost_usd=0.0,
                stuck_loops=0,
                first_try=outcome.passed,
                detail=outcome.detail,
            )
        )
    return tuple(results)


def build_record(
    *,
    benchmark_id: str,
    battery_version: str,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    results: tuple[TaskResult, ...],
) -> BenchmarkRecord:
    """Freeze a battery run into its storable record (pure constructor)."""
    return BenchmarkRecord(
        benchmark_id=benchmark_id,
        battery_version=battery_version,
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        results=results,
    )
