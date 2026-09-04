"""Scoring the self-evaluation battery: pure core, no I/O (Law 6.1).

This module is the ``report.py`` sibling the plan calls for: a pure
projection (:func:`summarize`) plus pure renderings of it (:func:`render` for
a person, :func:`render_json` for a machine), over records rather than prose.
The CLI and the runner are the dirty shell around it — they run checks,
stamp the clock and write the sidecar file, and none of that reaches here.

Two deliberate schema choices:

* :class:`TaskResult` carries its own 0..1 ``score``, independent of
  ``EpisodeOutcome``'s binary success|failure. A partial pass (two of three
  blindness assertions holding) is information about *where* the agent
  regressed, and forcing it through a binary outcome would throw that away
  (Engel 3: the scorer keeps its own scale, it never bends the Episode).
* Token/cost fields exist even though today's battery is offline and always
  records zero. They keep the record shape stable for the day live-agent
  tasks join the battery — a schema that changes when the battery grows is
  a schema the history cannot be read back through.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from computeruse.memory.schemas import Episode
from computeruse.orchestrator.report import UsageRecord

#: What the battery measures. One category per failure surface the agent has
#: actually hit in the field — not an aspirational taxonomy.
EvalCategory = Literal["grounding", "permission", "recovery", "report"]

#: The vocabulary, in one place, so the CLI filter and the summary agree.
ALL_CATEGORIES: Final[tuple[str, ...]] = (
    "grounding",
    "permission",
    "recovery",
    "report",
)


class TaskResult(BaseModel):
    """One battery task's outcome, as recorded (pure data)."""

    model_config = ConfigDict(frozen=True)

    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    category: EvalCategory
    title: str = Field(min_length=1)
    passed: bool
    #: 0..1 on the scorer's own scale: the fraction of the task's
    #: sub-assertions that held. Binary tasks record exactly 0.0 or 1.0.
    score: float = Field(ge=0.0, le=1.0)
    #: Atomic assertions inside the task — the "adım sayısı" metric.
    steps: int = Field(ge=0)
    #: Model spend attributable to the task. Zero by construction while the
    #: battery is offline; carried so live tasks later need no new schema.
    tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    #: Stuck-loop trips during the task. Zero by construction offline.
    stuck_loops: int = Field(ge=0)
    #: Whether the task passed on its single attempt. The runner evaluates
    #: each check exactly once, so this equals ``passed`` by construction
    #: today — and stays honest the day a task genuinely retries.
    first_try: bool
    detail: str = Field(min_length=1)


@dataclass(frozen=True)
class CategoryBreakdown:
    """One category's slice of the summary (pure data)."""

    category: str
    total: int
    passed: int


@dataclass(frozen=True)
class EvalSummary:
    """The whole battery, already counted — never a single number (pure data).

    The plan is explicit: pass rate alone is not the answer, the breakdown
    is. A battery that reports "92%" hides whether grounding or permission
    regressed, and those two failures mean entirely different things.
    """

    battery_version: str
    run_id: str
    results: tuple[TaskResult, ...]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def pass_rate(self) -> float:
        if not self.results:
            return 0.0
        return self.passed_count / len(self.results)

    @property
    def mean_score(self) -> float:
        if not self.results:
            return 0.0
        return sum(result.score for result in self.results) / len(self.results)

    def by_category(self) -> tuple[CategoryBreakdown, ...]:
        """Per-category passed/total, in vocabulary order (pure)."""
        ordered = [cat for cat in ALL_CATEGORIES if any(r.category == cat for r in self.results)]
        ordered.extend(
            sorted({r.category for r in self.results} - set(ordered))
        )
        return tuple(
            CategoryBreakdown(
                category=category,
                total=sum(1 for r in self.results if r.category == category),
                passed=sum(1 for r in self.results if r.category == category and r.passed),
            )
            for category in ordered
        )

    @property
    def total_steps(self) -> int:
        return sum(result.steps for result in self.results)

    @property
    def total_tokens(self) -> int:
        return sum(result.tokens for result in self.results)

    @property
    def total_cost_usd(self) -> float:
        return sum(result.cost_usd for result in self.results)


def summarize(
    *,
    battery_version: str,
    run_id: str,
    results: tuple[TaskResult, ...],
) -> EvalSummary:
    """Count a battery run (pure).

    Results are sorted by task id so two runs of the same battery render
    identically regardless of execution order.
    """
    return EvalSummary(
        battery_version=battery_version,
        run_id=run_id,
        results=tuple(sorted(results, key=lambda r: r.task_id)),
    )


def render(summary: EvalSummary) -> str:
    """The battery result a person reads (pure).

    Breakdown first, failures with their detail — a red line without the
    reason is unactionable, and the reason is exactly what ``detail`` holds.
    """
    lines: list[str] = [
        (
            f"eval battery v{summary.battery_version} — "
            f"{summary.passed_count}/{summary.total} passed "
            f"({summary.pass_rate * 100:.1f}%)"
        ),
        "",
    ]
    for part in summary.by_category():
        lines.append(f"  {part.category:<11} {part.passed}/{part.total}")
    lines.append("")
    lines.append(
        f"steps {summary.total_steps} · tokens {summary.total_tokens} "
        f"(offline battery spends none) · cost ${summary.total_cost_usd:.2f}"
    )
    failed = [r for r in summary.results if not r.passed]
    if failed:
        lines.append("")
        lines.append(f"failed — {len(failed)} task(s):")
        for result in failed:
            lines.append(f"  [{result.task_id}] {result.title}")
            lines.append(f"      {result.detail}")
    return "\n".join(lines).rstrip() + "\n"


def render_json(summary: EvalSummary) -> str:
    """The battery result a machine reads: records, never prose (pure).

    Deliberately the sibling of ``report.render``'s philosophy inverted —
    RunReport holds records so callers need not parse text, and this is that
    rendering for the eval side.
    """
    payload = {
        "battery_version": summary.battery_version,
        "run_id": summary.run_id,
        "passed": summary.passed_count,
        "total": summary.total,
        "pass_rate": summary.pass_rate,
        "mean_score": summary.mean_score,
        "steps": summary.total_steps,
        "tokens": summary.total_tokens,
        "cost_usd": summary.total_cost_usd,
        "by_category": [
            {"category": part.category, "passed": part.passed, "total": part.total}
            for part in summary.by_category()
        ],
        "results": [result.model_dump(mode="json") for result in summary.results],
    }
    return json.dumps(payload, indent=2) + "\n"


def join_usage(
    episodes: tuple[Episode, ...],
    usage: tuple[UsageRecord, ...],
) -> dict[str, UsageRecord | None]:
    """Map each episode to the usage of the run that produced it (pure).

    The join key is ``Episode.run_id`` (Engel 1). Episodes recorded before
    the field existed carry ``None`` and map to no usage — they are old
    history, not broken records, and the scorer must read them as such.
    A run id with no usage record (counters lost with the terminal) also
    maps to ``None`` rather than failing the join: no record is not the
    same as no spend, and the report already says so explicitly.
    """
    by_run: dict[str, UsageRecord] = {}
    for record in usage:
        by_run.setdefault(record.run_id, record)
    return {
        episode.episode_id: (
            by_run.get(episode.run_id) if episode.run_id is not None else None
        )
        for episode in episodes
    }


def usage_for_run(
    usage: tuple[UsageRecord, ...], *, run_id: str
) -> tuple[UsageRecord, ...]:
    """Every usage record belonging to one run id (pure)."""
    return tuple(record for record in usage if record.run_id == run_id)
