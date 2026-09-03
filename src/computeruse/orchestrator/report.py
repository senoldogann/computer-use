"""What the agent did while nobody was watching (Law 5 accountability).

Every other piece of this system argues for letting the agent run unattended:
it recovers from a dead driver, parks questions instead of hanging, resumes
missions it was killed in the middle of, and acts on authority delegated in
advance. None of that is worth anything if the person who owns the machine
cannot find out what happened on it.

That is not a comfort feature. It is the precondition for the rest: a
capability grant is only safe to write if its use is visible afterwards, and
nobody writes the second grant without having read what the first one did.

The state is already on disk in five stores that know nothing about each
other — episodes, missions, approvals, grants and per-run usage. This module
is the one place they are read together, as a *pure* projection
(:func:`summarize`) and a pure rendering of it (:func:`render`), so the whole
report can be tested without a clock, a store or a run.

:class:`UsageStore` is here rather than in its own module because the record it
keeps exists only to be reported: what a run cost is the one number a person
asks about first, and it was the one thing nowhere on disk — token and spend
counters were streamed to stderr and lost with the terminal scrollback.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from pydantic import BaseModel, Field

from computeruse.memory.schemas import Episode
from computeruse.orchestrator.mission import Mission, blocked_missions
from computeruse.security.approvals import ApprovalRequest, pending_requests
from computeruse.security.grants import CapabilityGrant

LOGGER: Final = logging.getLogger(__name__)

#: How much of a goal is shown on one line of the report.
GOAL_LINE_MAX_CHARS: Final[int] = 68


class UsageRecord(BaseModel):
    """What one run consumed (pure data).

    Written per run because the counters that produced it live only in the
    process: the CLI streams them to stderr for the live panel, and stderr is
    gone by morning. "What did last night cost?" is the first question anyone
    asks of an unattended agent, and it had no answer on disk.
    """

    run_id: str = Field(min_length=1)
    goal: str
    app: str
    outcome: str
    steps: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    elapsed_seconds: float = Field(ge=0.0)
    recorded_at: datetime


@dataclass(frozen=True)
class ReportPeriod:
    """The window a report covers (pure data)."""

    since: datetime
    until: datetime


@dataclass(frozen=True)
class RunReport:
    """Everything the period contains, already selected and counted (pure data).

    Deliberately holds the records themselves rather than pre-rendered text, so
    a caller that wants JSON, a panel view or a different phrasing does not
    have to parse prose back apart.
    """

    period: ReportPeriod
    episodes: tuple[Episode, ...]
    usage: tuple[UsageRecord, ...]
    blocked: tuple[Mission, ...]
    waiting: tuple[ApprovalRequest, ...]
    grants_used: tuple[CapabilityGrant, ...]

    @property
    def succeeded(self) -> int:
        return sum(1 for episode in self.episodes if episode.outcome == "success")

    @property
    def failed(self) -> int:
        return sum(1 for episode in self.episodes if episode.outcome == "failure")

    @property
    def total_tokens(self) -> int:
        return sum(record.total_tokens for record in self.usage)

    @property
    def total_cost_usd(self) -> float:
        return sum(record.cost_usd for record in self.usage)

    @property
    def is_quiet(self) -> bool:
        """Nothing ran, nothing is waiting — the period has nothing to say."""
        return not (self.episodes or self.blocked or self.waiting)


def summarize(
    *,
    episodes: tuple[Episode, ...],
    usage: tuple[UsageRecord, ...],
    missions: tuple[Mission, ...],
    approvals: tuple[ApprovalRequest, ...],
    grants: tuple[CapabilityGrant, ...],
    period: ReportPeriod,
) -> RunReport:
    """Select what belongs in the period (pure).

    Two different rules on purpose. *Activity* — what ran, what it cost — is
    filtered to the window, because "last night" means last night. *Open
    items* — blocked missions, unanswered questions — are never filtered: a
    question parked three days ago is more urgent than one parked an hour ago,
    and a report that hid it because it fell outside the window would be
    actively misleading about what the agent is waiting for.
    """
    in_window = tuple(
        episode
        for episode in episodes
        if period.since <= episode.recorded_at <= period.until
    )
    spent = tuple(
        record
        for record in usage
        if period.since <= record.recorded_at <= period.until
    )
    return RunReport(
        period=period,
        episodes=tuple(sorted(in_window, key=lambda e: e.recorded_at)),
        usage=tuple(sorted(spent, key=lambda r: r.recorded_at)),
        blocked=blocked_missions(missions),
        waiting=pending_requests(approvals),
        # Grants with a use spent: what authority was actually exercised, as
        # opposed to what merely exists. A standing permission nobody used is
        # not news; one that was used three times is.
        grants_used=tuple(grant for grant in grants if grant.used > 0),
    )


def period_ending(now: datetime, *, hours: float) -> ReportPeriod:
    """The window of the last ``hours``, ending now (pure)."""
    if hours <= 0:
        raise ValueError(f"hours must be positive, got {hours}")
    return ReportPeriod(since=now - timedelta(hours=hours), until=now)


def _ellipsis(text: str, limit: int) -> str:
    """Trim to one line's worth, marking the cut (pure)."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def render(report: RunReport) -> str:
    """The report a person reads over coffee (pure).

    Ordered by what a reader needs first, which is not chronological: what is
    waiting for *them* comes before what the agent managed on its own, because
    the first is the only part that will not resolve itself.
    """
    hours = (report.period.until - report.period.since).total_seconds() / 3600
    lines: list[str] = [
        (
            f"computeruse — the last {hours:.0f} hour(s), to "
            f"{report.period.until.isoformat(timespec='minutes')}"
        ),
        "",
    ]
    if report.is_quiet:
        lines.append("nothing ran, and nothing is waiting on you.")
        return "\n".join(lines)

    if report.waiting:
        lines.append(f"waiting on you — {len(report.waiting)} question(s)")
        for request in report.waiting:
            target = f" on {request.target_label!r}" if request.target_label else ""
            lines.append(
                f"  [{request.request_id}] {request.action_type}{target}"
            )
            lines.append(f"      to: {_ellipsis(request.sub_goal, GOAL_LINE_MAX_CHARS)}")
        lines.append("  answer with: computeruse --approve <id>   (or --deny <id>)")
        lines.append("")

    if report.blocked:
        lines.append(f"paused — {len(report.blocked)} mission(s)")
        for mission in report.blocked:
            lines.append(f"  {_ellipsis(mission.goal, GOAL_LINE_MAX_CHARS)}")
            if mission.blocked_reason:
                lines.append(f"      {_ellipsis(mission.blocked_reason, GOAL_LINE_MAX_CHARS)}")
        lines.append("")

    lines.append(
        f"ran — {len(report.episodes)} run(s): "
        f"{report.succeeded} finished, {report.failed} did not"
    )
    for episode in report.episodes:
        mark = "ok  " if episode.outcome == "success" else "lost"
        lines.append(
            f"  {mark} [{episode.app}] {_ellipsis(episode.description, GOAL_LINE_MAX_CHARS)}"
        )
        if episode.outcome != "success" and episode.retrospective:
            lines.append(f"       {_ellipsis(episode.retrospective, GOAL_LINE_MAX_CHARS)}")
    lines.append("")

    if report.usage:
        lines.append(
            f"spent — {report.total_tokens:,} tokens, ${report.total_cost_usd:.2f}"
        )
        lines.append("")
    else:
        # Said plainly rather than shown as zero: no record is not the same as
        # no spend, and a report that prints "$0.00" for a run whose usage was
        # never written is lying about the number people care about most.
        lines.append("spent — not recorded for this period")
        lines.append("")

    if report.grants_used:
        lines.append(f"authority used — {len(report.grants_used)} grant(s)")
        for grant in report.grants_used:
            lines.append(
                f"  {grant.verb} in {grant.app}: {grant.used} of "
                f"{grant.max_invocations} used — {_ellipsis(grant.note, 44)}"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


class UsageStore:
    """Per-run usage records on disk (Law 6.1 connector)."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def directory(self) -> Path:
        return self._directory

    def record(self, usage: UsageRecord) -> Path:
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self._directory / f"{usage.run_id}.json"
        target.write_text(usage.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return target

    def records(self) -> tuple[UsageRecord, ...]:
        """Every record on disk; unreadable files are skipped with a warning."""
        if not self._directory.is_dir():
            return ()
        found: list[UsageRecord] = []
        for path in sorted(self._directory.glob("*.json")):
            try:
                found.append(
                    UsageRecord.model_validate(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                )
            except (OSError, ValueError) as exc:
                LOGGER.warning("unreadable usage record %s: %s", path, exc)
        return tuple(found)
