"""Per-step run tracing: what a person gets to read after a run goes wrong.

The property that matters is not "successful runs are logged" — it is that the
step which *ended* a run is in the file, with the decision, the coordinate, the
verdict and the error next to each other.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from computeruse.agent import Agent, AgentConfig
from computeruse.orchestrator.loop import MaxStepsError, OodaRunner, WorkingState
from computeruse.orchestrator.schemas import AgentTurn, Finish, MouseClick
from computeruse.orchestrator.trace import (
    EVENT_PREFIX,
    RunTracer,
    StepTrace,
    event_line,
    new_run_id,
    step_trace_json,
)
from computeruse.security.autonomy import AutonomyLevel
from tests.smoke.conftest import SIMULATED_SETTLE, SOCKET_PATH

_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg=="
)


def _record(**overrides: object) -> StepTrace:
    fields: dict[str, object] = {
        "run_id": "run-1",
        "step": 0,
        "app": "Safari",
        "window": "Safari — GitHub",
        "thought": "the button is visible",
        "sub_goal": "press it",
        "action": {"type": "mouse_click", "x": 10, "y": 20},
        "route": "physical",
        "verdict": "confirmed",
        "error": None,
    }
    fields.update(overrides)
    return StepTrace(**fields)  # type: ignore[arg-type]


def _turn(action: object) -> AgentTurn:
    return AgentTurn.model_validate(
        {"thought": "t", "sub_goal": "s", "action": action}
    )


def test_step_line_carries_the_decision_and_its_verdict() -> None:
    """One line is enough to reconstruct a step without any other file."""
    payload = json.loads(step_trace_json(_record(), screenshot=None))
    assert payload["step"] == 0
    assert payload["action"] == {"type": "mouse_click", "x": 10, "y": 20}
    assert payload["verdict"] == "confirmed"
    assert payload["error"] is None
    assert payload["window"] == "Safari — GitHub"
    assert "time" in payload


def test_the_frame_is_referenced_by_filename_never_inlined() -> None:
    """A base64 frame per line would make a 30-step trace unreadable."""
    line = step_trace_json(_record(screenshot_b64=_TINY_PNG_B64), screenshot="step-000.png")
    assert _TINY_PNG_B64 not in line
    assert json.loads(line)["screenshot"] == "step-000.png"


def test_tracer_appends_one_line_per_step(tmp_path: Path) -> None:
    run_id = new_run_id()
    tracer = RunTracer(tmp_path, run_id=run_id, save_screenshots=False)
    tracer.record(_record(step=0))
    tracer.record(_record(step=1, error="boom", verdict=None))
    lines = (tracer.directory / "steps.jsonl").read_text().splitlines()
    assert [json.loads(line)["step"] for line in lines] == [0, 1]
    assert json.loads(lines[1])["error"] == "boom"
    assert tracer.directory.name == run_id
    assert not list(tracer.directory.glob("*.png")), "screenshots are opt-in"


def test_tracer_saves_the_frame_when_asked(tmp_path: Path) -> None:
    tracer = RunTracer(tmp_path, run_id="r", save_screenshots=True)
    tracer.record(_record(step=7, screenshot_b64=_TINY_PNG_B64))
    png = tracer.directory / "step-007.png"
    assert png.read_bytes().startswith(b"\x89PNG")
    line = json.loads((tracer.directory / "steps.jsonl").read_text().strip())
    assert line["screenshot"] == "step-007.png"


def test_a_broken_trace_sink_never_ends_the_run() -> None:
    """Diagnostics must not be able to kill the run they exist to explain."""

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=5, y=5))
        return _turn(Finish(type="finish", status="success", summary="done"))

    def boom(_record: StepTrace) -> None:
        raise OSError("disk full")

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _a: None,
        trace=boom,
        max_steps=5,
    )
    assert runner.run(goal="x").completed_steps  # the run finished regardless


def test_every_step_is_traced_including_the_one_that_failed() -> None:
    """The failing step is the point of the file, so it must be in it."""
    records: list[StepTrace] = []

    def provider(state: WorkingState) -> AgentTurn:
        return _turn(MouseClick(type="mouse_click", x=10 + state.step_index, y=10))

    def refuse(_action: object) -> None:
        raise RuntimeError("driver gone")

    runner = OodaRunner(
        provider=provider,
        execute_physical=refuse,
        trace=records.append,
        run_id="run-x",
        app="Safari",
        max_steps=2,
    )
    with pytest.raises(MaxStepsError):
        runner.run(goal="x")
    assert [r.step for r in records] == [0, 1]
    assert all(r.run_id == "run-x" for r in records)
    assert all(r.error is not None and "driver gone" in r.error for r in records)
    assert all(r.verdict is None for r in records)


def test_agent_writes_a_trace_directory_named_by_the_run(tmp_path: Path) -> None:
    """End to end: --trace-dir produces a readable record of the whole run."""

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=120, y=90))
        return _turn(Finish(type="finish", status="success", summary="ok"))

    trace_dir = tmp_path / "traces"
    config = AgentConfig(
        goal="press the button",
        app="Safari",
        provider=provider,
        socket_path=str(SOCKET_PATH),
        store_dir=tmp_path / "store",
        autonomy_level=AutonomyLevel.FULL,
        enable_visual_verification=False,
        enable_vision=False,
        max_steps=5,
        trace_dir=trace_dir,
        **SIMULATED_SETTLE,
    )
    result = Agent(config).run()
    assert result.run_id
    steps_file = trace_dir / result.run_id / "steps.jsonl"
    records = [json.loads(line) for line in steps_file.read_text().splitlines()]
    assert [r["route"] for r in records] == ["physical", "finish"]
    assert records[0]["action"] == {
        "type": "mouse_click",
        "x": 120,
        "y": 90,
        "button": "left",
        "click_count": 1,
    }
    assert all(r["run_id"] == result.run_id for r in records)
    assert records[1]["error"] is None


def test_every_step_is_announced_on_stdout(capsys, tmp_path) -> None:
    """The UI reads stdout, so structure has to reach it there.

    The panel used to recover the model's reasoning by matching log prose with
    string offsets ("ooda thought:" then substring(13)), which breaks silently
    the moment a message is reworded. The same facts are already assembled for
    the trace file; announcing them costs one line per step and needs no second
    transport.
    """
    record = StepTrace(
        run_id="20260101T000000Z-abcdef12",
        step=3,
        app="Google Chrome",
        window="Chrome — Hacker News",
        thought="the comments link is at (401,221)",
        sub_goal="open the comments page",
        action={"type": "mouse_click", "x": 401, "y": 221},
        route="physical",
        verdict="confirmed",
        error=None,
    )
    line = event_line(record)
    assert line.startswith(EVENT_PREFIX)
    payload = json.loads(line[len(EVENT_PREFIX) :])
    assert payload["step"] == 3
    assert payload["thought"] == "the comments link is at (401,221)"
    assert payload["action"]["type"] == "mouse_click"
    assert payload["verdict"] == "confirmed"
    # The frame is never inlined: a base64 image per step would make the stream
    # unreadable for the humans and tools that also tail it.
    assert payload["screenshot"] is None


def test_the_event_prefix_cannot_be_confused_with_log_prose() -> None:
    """A reader splits the two streams with a prefix test, so it must not
    collide with anything the log or a traceback emits."""
    for prose in (
        "ooda thought: something",
        "st : tok_total=1 elapsed=1s",
        'Traceback (most recent call last):',
        "  File \"x.py\", line 1, in <module>",
        "goal        : do a thing",
    ):
        assert not prose.startswith(EVENT_PREFIX)
