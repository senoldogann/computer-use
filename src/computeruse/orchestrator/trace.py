"""Per-step run trace — what the agent decided, did, and observed (ADR-1 shell).

A run that goes wrong on step 17 of 30 leaves almost nothing to look at: the
terminal shows the log lines that scrolled past, the episode records the
actions that succeeded, and the one thing a person actually needs — the
decision, the coordinate, the verdict and the error, side by side, for the step
that broke — is gone. This module is that record.

One JSON object per step, appended to ``<trace-dir>/<run_id>/steps.jsonl``, and
optionally the exact screenshot the model saw for that step next to it. The
file is opened and closed per write on purpose: the runs worth tracing are the
ones that end by exception, and a buffered stream is empty precisely then.

:class:`StepTrace` is pure data and :func:`step_trace_json` is a pure
serialiser; :class:`RunTracer` is the I/O connector (Law 6.1).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

LOGGER: Final = logging.getLogger(__name__)

#: Name of the append-only per-step record inside a run's trace directory.
STEPS_FILENAME: Final[str] = "steps.jsonl"


@dataclass(frozen=True)
class StepTrace:
    """One step of one run, as it happened (pure data).

    ``screenshot_b64`` is carried but never serialised into the JSONL: it is
    the frame the model saw, offered to the tracer so it can write a PNG beside
    the record. Keeping it on the record — rather than making the runner decide
    whether screenshots are wanted — puts that policy in exactly one place.
    """

    run_id: str
    step: int
    app: str
    #: The focused-window summary the model was shown, when a probe answered.
    window: str | None
    thought: str
    sub_goal: str
    #: The validated action payload, exactly as it was dispatched.
    action: dict[str, object]
    route: str
    #: The verification verdict, when the action declared a postcondition.
    verdict: str | None
    #: Why the step failed, when it did. ``None`` on a step that succeeded.
    error: str | None
    screenshot_b64: str | None = None


def step_trace_json(record: StepTrace, *, screenshot: str | None) -> str:
    """Serialise one step as a single JSON line (pure).

    ``screenshot`` is the *filename* the tracer wrote, not the image — a trace
    file has to stay greppable, and a base64 frame per line would make it tens
    of megabytes for a thirty-step run.
    """
    payload: dict[str, object] = {
        "run_id": record.run_id,
        "step": record.step,
        "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "app": record.app,
        "window": record.window,
        "thought": record.thought,
        "sub_goal": record.sub_goal,
        "action": record.action,
        "route": record.route,
        "verdict": record.verdict,
        "error": record.error,
        "screenshot": screenshot,
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


#: Marks a line on stdout as a structured event rather than human log text.
#: Chosen to be something no prose or traceback produces, so a reader can split
#: the two streams with a prefix test and never misparse a log line.
EVENT_PREFIX: Final[str] = "@@CU "


def event_line(record: StepTrace) -> str:
    """One step rendered as a tagged, machine-readable stdout line (pure).

    The UI panel reads the agent's stdout, and until now it could only show
    what the log happened to say in prose — so the plan, the model's reasoning,
    the verification verdict and the recovery rung were all present in the
    process and invisible in the window watching it. The tracer already builds
    exactly that record for its file; emitting the same object on stdout gives
    the panel the structured stream without a second transport, an IPC channel,
    or a requirement that tracing to disk be switched on at all.

    The screenshot is deliberately absent: this line is read by a UI that
    already has the frame on screen, and a base64 image per step would make the
    stream unreadable for the humans and tools that also tail it.
    """
    return EVENT_PREFIX + step_trace_json(record, screenshot=None)


def new_run_id() -> str:
    """A sortable, unique identity for one run.

    Timestamp first so a trace directory lists chronologically, random suffix
    so two runs started in the same second cannot collide.
    """
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


class RunTracer:
    """Append-only trace of one run on disk (Law 6.1: I/O connector).

    Best-effort by contract: a trace is diagnostics, and a full disk or a
    read-only path must not end a run that is otherwise working. Write failures
    are logged once per run and then demoted, never raised.
    """

    def __init__(self, directory: Path, *, run_id: str, save_screenshots: bool) -> None:
        self._run_id = run_id
        self._directory = directory / run_id
        self._save_screenshots = save_screenshots
        self._steps_path = self._directory / STEPS_FILENAME
        self._warned = False
        self._directory.mkdir(parents=True, exist_ok=True)

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def directory(self) -> Path:
        """Where this run's artifacts are written."""
        return self._directory

    def record(self, step: StepTrace) -> None:
        """Append one step (and, when asked, the frame it was decided from)."""
        screenshot = self._write_screenshot(step)
        line = step_trace_json(step, screenshot=screenshot)
        try:
            with self._steps_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            self._warn("could not append to the run trace", exc)

    def _write_screenshot(self, step: StepTrace) -> str | None:
        """Save the step's frame as a PNG; return its filename, or None."""
        if not self._save_screenshots or not step.screenshot_b64:
            return None
        filename = f"step-{step.step:03d}.png"
        try:
            (self._directory / filename).write_bytes(
                base64.b64decode(step.screenshot_b64, validate=True)
            )
        except (OSError, binascii.Error) as exc:
            self._warn("could not write the step screenshot", exc)
            return None
        return filename

    def _warn(self, what: str, exc: Exception) -> None:
        """Complain loudly once, quietly thereafter (a trace is not the run)."""
        if self._warned:
            LOGGER.debug("%s (%s): %s", what, self._steps_path, exc)
            return
        self._warned = True
        LOGGER.warning("%s (%s): %s", what, self._steps_path, exc)
