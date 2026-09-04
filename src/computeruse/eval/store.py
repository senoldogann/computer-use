"""Benchmark records: eval scores on disk, beside — never inside — history.

Engel 2: :class:`EpisodicStore.record` refuses to overwrite, because
clobbering history destroys learnings. A score is not history — it is a
snapshot of one battery run, re-runnable by design — so it needs its own
store rather than a weakened episode store. The shape follows the
``missions/`` and ``approvals/`` precedent: one JSON file per record under
``<store>/benchmarks/``, unreadable files skipped with a warning so one
corrupt score never hides the rest.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, Field

from computeruse.eval.score import TaskResult

LOGGER: Final = logging.getLogger(__name__)


class BenchmarkRecord(BaseModel):
    """One battery run's score, frozen at record time (pure data)."""

    benchmark_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    battery_version: str = Field(min_length=1)
    #: The eval run's own id, so its cost can be joined later the same way
    #: episodes join to usage. ``None`` only for records written by hand.
    run_id: str | None = Field(default=None)
    started_at: datetime
    finished_at: datetime
    results: tuple[TaskResult, ...] = Field(min_length=1)

    @property
    def passed_count(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def pass_rate(self) -> float:
        if not self.results:
            return 0.0
        return self.passed_count / len(self.results)


def new_benchmark_id(now: datetime) -> str:
    """A filesystem-safe, sortable id: ``eval.<utc-timestamp>`` (pure).

    Timestamps sort lexically, so a directory listing is chronological —
    the same convention missions use for the same reason.
    """
    return f"eval.{now.strftime('%Y%m%dt%H%M%S%f').lower()}"


class BenchmarkStore:
    """Imperative shell: benchmark snapshots on disk (Law 6 connector)."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def directory(self) -> Path:
        return self._directory

    def save(self, record: BenchmarkRecord) -> Path:
        """Persist one score, overwriting a previous same-id snapshot.

        Overwrite is the deliberate difference from :class:`EpisodicStore`:
        re-running the battery with a fixed id refreshes the snapshot rather
        than failing. History lives in episodes; this store holds readings.
        """
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self._directory / f"{record.benchmark_id}.json"
        target.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return target

    def records(self) -> tuple[BenchmarkRecord, ...]:
        """Every score on disk, oldest first; corrupt files skipped loudly."""
        if not self._directory.is_dir():
            return ()
        found: list[BenchmarkRecord] = []
        for path in sorted(self._directory.glob("*.json")):
            try:
                found.append(
                    BenchmarkRecord.model_validate(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                )
            except (OSError, ValueError) as exc:
                LOGGER.warning("unreadable benchmark %s: %s", path, exc)
        return tuple(found)

    def load(self, benchmark_id: str) -> BenchmarkRecord:
        path = self._directory / f"{benchmark_id}.json"
        if not path.is_file():
            raise KeyError(f"no benchmark {benchmark_id!r} in {self._directory}")
        return BenchmarkRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
