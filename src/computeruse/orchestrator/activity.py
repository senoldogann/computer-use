"""Versioned live activity event contract (spec §8).

The ``@@CU`` transport is the right foundation; what it lacked was a
schema. Every UI-facing event now carries one envelope: a schema version
so the panel can evolve without guessing, a monotonic per-run sequence so
the WebView renders deterministically even when stdout/stderr delivery
timing differs, and stable identity (event/run/session ids) so log lines,
trace files and bug reports join on the same run.

Two shapes leave this module, both as one ``@@CU`` line:

* new lifecycle events (``run_completed``, ``memory_written``, …), built by
  :class:`ActivityEmitter` with a nested ``payload``;
* legacy records (``step``, ``plan``, ``stats``), translated by
  :func:`translate_legacy_record`, which adds the envelope around the
  original fields *verbatim* — the panel reads the same keys it always
  has, so migration needs no UI change. Unknown future types pass through
  the panel untouched by contract (it ignores what it does not know),
  which is what makes the vocabulary below safe to grow.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal

from computeruse.orchestrator.trace import EVENT_PREFIX

LOGGER: Final = logging.getLogger(__name__)

#: Contract version stamped on every event. Bump when the envelope shape
#: changes; the panel branches on it instead of sniffing fields.
ACTIVITY_SCHEMA_VERSION: Final[int] = 1

#: The stable event vocabulary (spec §8.2). Declared whole so producers
#: and the panel share one list; only a subset is emitted yet — an event
#: type with no producer is a promise, not a lie, as long as nothing
#: claims it fired. Wire more producers before adding names here.
ActivityEventType = Literal[
    "session_started",
    "session_stopped",
    "goal_proposed",
    "goal_selected",
    "plan_created",
    "subgoal_started",
    "observation_ready",
    "decision_ready",
    "action_started",
    "action_completed",
    "tool_started",
    "tool_completed",
    "verification_started",
    "verification_passed",
    "verification_failed",
    "recovery_started",
    "recovery_completed",
    "safety_decision",
    "approval_state",
    "memory_retrieved",
    "memory_written",
    "skill_mounted",
    "skill_updated",
    "usage_updated",
    "run_completed",
    "run_failed",
]

ActivitySeverity = Literal["debug", "info", "warning", "error"]


def _empty_payload() -> dict[str, object]:
    """Typed empty payload factory (strict mode rejects bare ``dict``)."""
    return {}


@dataclass(frozen=True)
class ActivityEvent:
    """One UI-facing fact with its envelope (pure data).

    Identity is required, not defaulted: an event without an id cannot be
    deduplicated or referenced, and a silent empty string would launder
    that absence downstream. The emitter mints ids; direct constructors
    (tests, translators) pass their own.
    """

    type: str
    run_id: str
    sequence: int
    event_id: str
    session_id: str = ""
    severity: ActivitySeverity = "info"
    payload: dict[str, object] = field(default_factory=_empty_payload)
    timestamp: str = ""

    def to_line(self) -> str:
        """Render as one ``@@CU`` transport line (pure).

        Legacy-compatible by construction: the envelope rides on top and
        the payload's own keys stay top-level beside it, so a panel that
        only knows ``type``/``plan``/``action`` keeps working. On key
        collision the envelope wins — a forged or stale ``sequence`` must
        never break the panel's ordering.
        """
        stamp = self.timestamp or datetime.now(UTC).isoformat(timespec="milliseconds")
        record: dict[str, object] = {
            **self.payload,
            "schema_version": ACTIVITY_SCHEMA_VERSION,
            "type": self.type,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "timestamp": stamp,
            "severity": self.severity,
        }
        return EVENT_PREFIX + json.dumps(record, ensure_ascii=False, default=str)


def translate_legacy_record(
    record: Mapping[str, object],
    *,
    run_id: str,
    session_id: str,
    sequence: int,
) -> str:
    """Envelope a pre-contract ``step``/``plan``/``stats`` dict (pure).

    Fields pass through untouched (minus ``type``, which the envelope
    re-states); the panel keeps reading exactly what it read before, plus
    version/sequence/identity it can start relying on. Raises on a record
    without a ``type`` — an untyped line is not a legacy record, it is a
    bug, and enveloping it would launder the bug into the timeline.
    """
    event_type = record.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("legacy records must carry a string 'type' to translate")
    payload: dict[str, object] = {
        k: v for k, v in record.items() if k != "type"
    }
    return ActivityEvent(
        type=event_type,
        run_id=run_id,
        sequence=sequence,
        event_id=f"evt_{uuid.uuid4().hex[:12]}",
        session_id=session_id,
        payload=payload,
    ).to_line()


class ActivityEmitter:
    """Per-run sequenced event source (Law 6: I/O shell).

    One emitter per run owns the monotonic sequence the spec requires;
    separate runs (separate emitters) never share a counter, so
    interleaved output from concurrent processes cannot forge an order.
    The sink defaults to stdout, matching the existing announce path;
    tests inject a list append.
    """

    def __init__(
        self,
        *,
        run_id: str,
        session_id: str = "",
        sink: Callable[[str], None] | None = None,
    ) -> None:
        self._run_id = run_id
        self._session_id = session_id
        self._sink = sink
        self._sequence = 0

    @property
    def sequence(self) -> int:
        """Next sequence number to be assigned (observability for tests)."""
        return self._sequence

    def emit(
        self,
        event_type: str,
        payload: Mapping[str, object] | None = None,
        *,
        severity: ActivitySeverity = "info",
    ) -> ActivityEvent:
        """Build, transport and return one event (shell).

        Emission is best-effort like the trace sink: a broken pipe must
        never end a run that is otherwise working. The returned event
        carries the assigned sequence even when transport failed.
        """
        event = ActivityEvent(
            type=event_type,
            run_id=self._run_id,
            sequence=self._sequence,
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            session_id=self._session_id,
            severity=severity,
            payload=dict(payload) if payload is not None else {},
        )
        self._sequence += 1
        self._transport(event.to_line())
        return event

    def _transport(self, line: str) -> None:
        try:
            if self._sink is not None:
                self._sink(line)
            else:
                print(line, flush=True)
        except Exception as exc:  # noqa: BLE001 - diagnostics must not kill runs
            LOGGER.warning("activity emit failed: %s", exc)
