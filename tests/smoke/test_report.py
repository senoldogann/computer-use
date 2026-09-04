"""The morning report: what happened while nobody was watching.

Not a comfort feature. A capability grant is only safe to write if its use is
visible afterwards, and nobody writes the second grant without having read
what the first one did — so the report is the precondition for the delegation,
not a nicety on top of it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from computeruse.memory.schemas import Episode
from computeruse.orchestrator.mission import (
    mission_blocked,
    mission_started,
    new_mission,
)
from computeruse.orchestrator.report import (
    UsageRecord,
    UsageStore,
    period_ending,
    render,
    summarize,
)
from computeruse.orchestrator.schemas import AgentTurn, MouseClick
from computeruse.security.approvals import approval_request_for
from computeruse.security.grants import new_grant, spent

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
LAST_NIGHT = NOW - timedelta(hours=6)
LAST_WEEK = NOW - timedelta(days=7)


def _episode(
    *, description: str, outcome: str, at: datetime, retrospective: str | None = None
) -> Episode:
    return Episode(
        episode_id=f"safari.{int(at.timestamp())}{outcome[0]}",
        app="Safari",
        description=description,
        steps=(MouseClick(type="mouse_click", x=1, y=1),),
        outcome=outcome,  # type: ignore[arg-type]
        retrospective=retrospective,
        signature="deadbeef",
        recorded_at=at,
    )


def _usage(*, at: datetime, tokens: int, cost: float) -> UsageRecord:
    return UsageRecord(
        run_id=f"run-{int(at.timestamp())}",
        goal="tidy the exports",
        app="Safari",
        outcome="success",
        steps=4,
        total_tokens=tokens,
        cost_usd=cost,
        elapsed_seconds=42.0,
        recorded_at=at,
    )


def _parked(sub_goal: str = "send the invoice"):
    turn = AgentTurn(
        thought="t", sub_goal=sub_goal, action=MouseClick(type="mouse_click", x=9, y=9)
    )
    return approval_request_for(
        turn,
        goal="invoice the client",
        mission_id="m-1",
        target_label="Send",
        risk="destructive",
        now=LAST_WEEK,
    )


def _blocked_mission():
    started = mission_started(
        new_mission(goal="invoice the client", app="Mail", plan=None, now=LAST_WEEK),
        LAST_WEEK,
    )
    return mission_blocked(
        started,
        plan=None,
        reason="you have no grant for Send",
        approval_id="req-1",
        now=LAST_WEEK,
    )


def _report(**overrides: object):
    base: dict[str, object] = {
        "episodes": (),
        "usage": (),
        "missions": (),
        "approvals": (),
        "grants": (),
        "period": period_ending(NOW, hours=24),
    }
    base.update(overrides)
    return summarize(**base)  # type: ignore[arg-type]


# --- what belongs in the window ---------------------------------------------


def test_activity_is_filtered_to_the_window() -> None:
    """"Last night" means last night."""
    report = _report(
        episodes=(
            _episode(description="recent", outcome="success", at=LAST_NIGHT),
            _episode(description="ancient", outcome="success", at=LAST_WEEK),
        )
    )
    assert [e.description for e in report.episodes] == ["recent"]


def test_open_items_are_never_filtered_out() -> None:
    """A question parked three days ago is more urgent, not less.

    Hiding it because it fell outside the window would make the report
    actively misleading about what the agent is waiting for.
    """
    report = _report(approvals=(_parked(),), missions=(_blocked_mission(),))
    assert len(report.waiting) == 1
    assert len(report.blocked) == 1
    assert report.waiting[0].created_at < report.period.since


def test_only_authority_that_was_actually_used_is_reported() -> None:
    """A standing permission nobody used is not news."""
    used = spent(
        new_grant(
            verb="delete",
            app="Finder",
            target_pattern="*",
            max_invocations=3,
            expires_at=NOW + timedelta(hours=1),
            note="weekly cleanup",
            now=LAST_NIGHT,
        )
    )
    untouched = new_grant(
        verb="send",
        app="Mail",
        target_pattern="*",
        max_invocations=1,
        expires_at=NOW + timedelta(hours=1),
        note="never fired",
        now=LAST_NIGHT,
    )
    report = _report(grants=(used, untouched))
    assert [g.verb for g in report.grants_used] == ["delete"]


def test_counts_split_what_finished_from_what_did_not() -> None:
    report = _report(
        episodes=(
            _episode(description="a", outcome="success", at=LAST_NIGHT),
            _episode(description="b", outcome="failure", at=LAST_NIGHT),
            _episode(description="c", outcome="failure", at=LAST_NIGHT),
        )
    )
    assert (report.succeeded, report.failed) == (1, 2)


def test_spend_sums_only_the_records_in_the_window() -> None:
    report = _report(
        usage=(
            _usage(at=LAST_NIGHT, tokens=1200, cost=0.42),
            _usage(at=LAST_NIGHT, tokens=800, cost=0.18),
            _usage(at=LAST_WEEK, tokens=99999, cost=99.0),
        )
    )
    assert report.total_tokens == 2000
    assert report.total_cost_usd == pytest.approx(0.60)


def test_a_period_must_have_a_positive_length() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        period_ending(NOW, hours=0)


# --- how it reads -----------------------------------------------------------


def test_a_quiet_period_says_so_in_one_line() -> None:
    text = render(_report())
    assert "nothing ran, and nothing is waiting on you." in text
    assert "ran —" not in text


def test_what_needs_the_reader_comes_before_what_the_agent_handled() -> None:
    """Ordered by what will not resolve itself, not chronologically."""
    text = render(
        _report(
            episodes=(_episode(description="a run", outcome="success", at=LAST_NIGHT),),
            approvals=(_parked(),),
            missions=(_blocked_mission(),),
        )
    )
    assert text.index("waiting on you") < text.index("paused —") < text.index("ran —")


def test_a_failed_run_carries_its_retrospective() -> None:
    """The line a person needs is why it stopped, not that it stopped."""
    text = render(
        _report(
            episodes=(
                _episode(
                    description="research the news",
                    outcome="failure",
                    at=LAST_NIGHT,
                    retrospective="recovery exhausted (coordinate): off-screen target",
                ),
            )
        )
    )
    assert "recovery exhausted" in text


def test_a_successful_run_does_not_carry_one() -> None:
    text = render(
        _report(
            episodes=(
                _episode(
                    description="tidy up",
                    outcome="success",
                    at=LAST_NIGHT,
                    retrospective="went fine",
                ),
            )
        )
    )
    assert "went fine" not in text


def test_no_usage_record_is_reported_as_unknown_not_as_zero() -> None:
    """A report that prints "$0.00" for a run whose usage was never written is
    lying about the number people care about most."""
    text = render(_report(episodes=(_episode(description="a", outcome="success", at=LAST_NIGHT),)))
    assert "not recorded" in text
    assert "$0.00" not in text


def test_recorded_zero_spend_is_shown_as_zero() -> None:
    """A demo run genuinely costs nothing, and saying so is not the same as
    having no record."""
    text = render(
        _report(
            episodes=(_episode(description="a", outcome="success", at=LAST_NIGHT),),
            usage=(_usage(at=LAST_NIGHT, tokens=0, cost=0.0),),
        )
    )
    assert "$0.00" in text
    assert "not recorded" not in text


def test_a_long_goal_is_trimmed_to_one_line() -> None:
    text = render(
        _report(
            episodes=(
                _episode(description="x " * 200, outcome="success", at=LAST_NIGHT),
            )
        )
    )
    assert max(len(line) for line in text.splitlines()) < 100
    assert "…" in text


# --- the store --------------------------------------------------------------


def test_usage_records_round_trip(tmp_path: Path) -> None:
    store = UsageStore(tmp_path)
    record = _usage(at=LAST_NIGHT, tokens=1200, cost=0.42)
    store.record(record)
    assert store.records() == (record,)


def test_one_corrupt_usage_record_does_not_hide_the_others(tmp_path: Path) -> None:
    store = UsageStore(tmp_path)
    good = _usage(at=LAST_NIGHT, tokens=10, cost=0.01)
    store.record(good)
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert store.records() == (good,)


def test_an_empty_usage_store_is_not_an_error(tmp_path: Path) -> None:
    assert UsageStore(tmp_path / "never-created").records() == ()
