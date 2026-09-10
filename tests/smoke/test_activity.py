"""Versioned live activity event contract (spec §8).

The @@CU transport stays byte-compatible: legacy step/plan/stats records
keep every field the panel reads, and gain a versioned envelope the panel
can start relying on. New lifecycle events use the same envelope from day
one. Unknown future types must pass through the panel untouched — that
forward-compatibility is what makes the vocabulary safe to grow.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import get_args

import pytest

from computeruse.agent import Agent, AgentConfig
from computeruse.orchestrator.activity import (
    ACTIVITY_SCHEMA_VERSION,
    ActivityEmitter,
    ActivityEvent,
    ActivityEventType,
    translate_legacy_record,
)
from computeruse.orchestrator.loop import WorkingState
from computeruse.orchestrator.schemas import AgentTurn
from computeruse.orchestrator.trace import EVENT_PREFIX
from computeruse.security.autonomy import AutonomyLevel
from tests.smoke.conftest import SIMULATED_SETTLE, SOCKET_PATH


def _parse(line: str) -> dict[str, object]:
    assert line.startswith(EVENT_PREFIX)
    parsed = json.loads(line[len(EVENT_PREFIX):])
    assert isinstance(parsed, dict)
    return parsed


def test_vocabulary_covers_the_spec_set() -> None:
    names = set(get_args(ActivityEventType))
    for expected in (
        "run_completed",
        "run_failed",
        "memory_written",
        "decision_ready",
        "verification_failed",
        "approval_state",
        "skill_mounted",
        "usage_updated",
    ):
        assert expected in names


def test_envelope_shape_and_version() -> None:
    event = ActivityEvent(
        type="run_completed", run_id="r1", sequence=4, event_id="evt_test123"
    )
    record = _parse(event.to_line())
    assert record["schema_version"] == ACTIVITY_SCHEMA_VERSION == 1
    assert record["type"] == "run_completed"
    assert record["run_id"] == "r1"
    assert record["sequence"] == 4
    assert record["event_id"] == "evt_test123"
    assert isinstance(record["timestamp"], str) and record["timestamp"]
    assert record["severity"] == "info"
    assert record["session_id"] == ""


def test_sequence_is_monotonic_per_emitter() -> None:
    seen: list[str] = []
    emitter = ActivityEmitter(run_id="r1", sink=seen.append)
    first = emitter.emit("run_completed", {"outcome": "success"})
    second = emitter.emit("memory_written", {"outcome": "created"})
    assert (first.sequence, second.sequence) == (0, 1)
    assert emitter.sequence == 2
    assert [_parse(line)["sequence"] for line in seen] == [0, 1]


def test_event_ids_are_unique() -> None:
    emitter = ActivityEmitter(run_id="r1", sink=lambda line: None)
    ids = {emitter.emit("run_completed").event_id for _ in range(50)}
    assert len(ids) == 50


def test_legacy_step_record_keeps_every_panel_field() -> None:
    legacy = {
        "type": "step",
        "run_id": "r1",
        "step": 3,
        "thought": "t",
        "sub_goal": "s",
        "action": {"type": "wait", "duration_ms": 1, "reason": "r"},
        "route": "internal_wait",
        "verdict": None,
        "error": None,
    }
    record = _parse(
        translate_legacy_record(legacy, run_id="r1", session_id="", sequence=7)
    )
    for key, value in legacy.items():
        assert record[key] == value
    assert record["schema_version"] == 1
    assert record["sequence"] == 7


def test_legacy_translation_rejects_untyped_records() -> None:
    with pytest.raises(ValueError, match="string 'type'"):
        translate_legacy_record({"nope": 1}, run_id="r", session_id="", sequence=0)


def test_envelope_wins_collisions_for_ordering_integrity() -> None:
    """A stale or forged sequence inside a payload must never break the
    panel's ordering: envelope identity always wins."""
    record = _parse(
        translate_legacy_record(
            {"type": "stats", "sequence": 999, "total_tokens": 5},
            run_id="r1",
            session_id="",
            sequence=2,
        )
    )
    assert record["sequence"] == 2
    assert record["total_tokens"] == 5


def test_broken_sink_never_kills_the_run(capsys: pytest.CaptureFixture[str]) -> None:
    def bad_sink(line: str) -> None:
        raise OSError("pipe gone")

    emitter = ActivityEmitter(run_id="r1", sink=bad_sink)
    event = emitter.emit("run_completed", {"outcome": "success"})
    assert event.sequence == 0
    assert emitter.sequence == 1


def _stream_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    """Parse every @@CU line the run printed (pure test helper)."""
    out: list[dict[str, object]] = []
    for line in capsys.readouterr().out.splitlines():
        if not line.startswith(EVENT_PREFIX):
            continue
        parsed = json.loads(line[len(EVENT_PREFIX):])
        assert isinstance(parsed, dict)
        out.append(parsed)
    return out


def _agent_config(
    tmp_path: Path,
    goal: str,
    provider: Callable[[WorkingState], AgentTurn],
    *,
    max_steps: int = 10,
) -> AgentConfig:
    return AgentConfig(
        goal=goal,
        app="Safari",
        provider=provider,
        socket_path=str(SOCKET_PATH),
        store_dir=tmp_path / "store",
        autonomy_level=AutonomyLevel.GUARDED,
        enable_visual_verification=False,
        max_steps=max_steps,
        **SIMULATED_SETTLE,
    )


def _finish_turn() -> AgentTurn:
    return AgentTurn.model_validate(
        {
            "thought": "t",
            "sub_goal": "s",
            "action": {"type": "finish", "status": "success", "summary": "done"},
        }
    )


def _click_turn() -> AgentTurn:
    return AgentTurn.model_validate(
        {
            "thought": "t",
            "sub_goal": "s",
            "action": {"type": "mouse_click", "x": 1, "y": 1},
        }
    )


def test_run_completed_event_closes_the_stream(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The last structured line of a run names its ending: outcome, steps,
    app — everything a timeline needs to close the run's row."""
    def provider(state: WorkingState) -> AgentTurn:
        return _finish_turn()

    result = Agent(_agent_config(tmp_path, "Do nothing.", provider)).run()

    # Readouterr drains: a single read serves every assertion below.
    lines = _stream_lines(capsys)
    assert result.succeeded is True
    completed = [line for line in lines if line.get("type") == "run_completed"]
    assert len(completed) == 1
    event = completed[0]
    assert event["run_id"] == result.run_id
    assert event["schema_version"] == 1
    assert isinstance(event["sequence"], int)
    # Every step line shares the envelope: same run, ordered sequences.
    steps = [line for line in lines if line.get("type") == "step"]
    assert steps, "the finish turn itself is a step and must stream"
    assert all(step["run_id"] == result.run_id for step in steps)
    assert event["sequence"] > max(step["sequence"] for step in steps)


def test_run_failed_event_marks_abnormal_endings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A run that dies by exception must read as dead on the timeline, not
    as perpetually running: the event fires before the error propagates."""
    def provider(state: WorkingState) -> AgentTurn:
        return _click_turn()

    from computeruse.orchestrator.failures import UnrecoverableFailureError
    from computeruse.orchestrator.loop import MaxStepsError

    config = _agent_config(tmp_path, "Click forever.", provider, max_steps=1)
    with pytest.raises((MaxStepsError, UnrecoverableFailureError)):
        Agent(config).run()
    failed = [
        line for line in _stream_lines(capsys) if line.get("type") == "run_failed"
    ]
    assert len(failed) == 1
    assert failed[0]["severity"] == "error"
    assert "MaxStepsError" in str(failed[0].get("error", ""))


def test_memory_written_event_follows_preference_learning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A durable preference write is timeline-visible with its safe summary
    — the audit trail for future behavior changes starts here."""
    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _click_turn()
        return _finish_turn()

    result = Agent(
        _agent_config(tmp_path, "From now on use compact summaries.", provider)
    ).run()
    assert result.succeeded is True
    assert len(result.preferences) == 1
    written = [
        line for line in _stream_lines(capsys) if line.get("type") == "memory_written"
    ]
    assert len(written) == 1
    assert written[0]["run_id"] == result.run_id
    assert written[0]["outcome"] == "created"
    assert isinstance(written[0]["summary"], str) and written[0]["summary"]
