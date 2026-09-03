"""Approval requests an unattended run parks instead of dying on (Law 5.1).

Level 3 is "unattended execution", and the permission guard still asks a human
about destructive actions — correctly, and this module does not change that.
What it changes is what happens when the human is not there.

Before this, an unattended run that reached a destructive action had two
endings, both bad:

* **With a terminal attached** it called the CLI's confirmation handler, which
  reads a line from stdin. Nobody was going to type one, so the session hung —
  holding the machine, with the agent frozen mid-task, until someone came back.
* **Without one** the guard raised, the run ended, and nothing anywhere
  recorded *what* it had wanted to do or why it stopped. The session moved to
  its next goal, so the work was not merely paused, it was lost — and the same
  goal proposed again tomorrow would walk into the same wall with no memory of
  having been there.

An approval request is the third ending: write down exactly what was proposed,
who proposed it and what it was for, park the mission, and move on. The human
reviews the queue when they return, and a run can be resumed from where it
stopped rather than restarted from nothing.

The queue is deliberately *not* a permission system. Nothing here decides that
an action may run; a pending request is a question, an approved one is a
human's recorded answer, and the guard remains the only thing that lets an
action through. Per Law 6 the transformations are pure and :class:`ApprovalQueue`
is the connector that puts them on disk.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, Field

from computeruse.orchestrator.schemas import AgentTurn
from computeruse.slug import ascii_slug

LOGGER: Final = logging.getLogger(__name__)

ApprovalDecision = Literal["pending", "approved", "denied"]

#: How much of a sub-goal survives into a request id. Long enough to tell two
#: parked actions apart when reading the queue, short enough to stay a filename.
SUB_GOAL_SLUG_MAX_CHARS: Final[int] = 40


class ApprovalRequest(BaseModel):
    """One action an unattended run could not take on its own (pure data).

    Carries enough for a person to answer the question *without* replaying the
    run: what the agent wanted to do, what it was trying to achieve, which
    control it was aimed at, and how the guard classified it. A queue entry
    that only said "an action needs approval" would be unanswerable, and an
    unanswerable question is the same as no question.
    """

    request_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    #: The mission this belongs to, when the run had one. ``None`` for a
    #: one-off run: the request is still worth recording, it just has no
    #: parked work to resume.
    mission_id: str | None
    goal: str
    sub_goal: str
    action_type: str
    #: The action exactly as it would have been actuated, so an approval
    #: cannot silently apply to a *different* action later.
    action: dict[str, object]
    #: Accessibility title of the control under the pointer, when there was
    #: one. This — not the model's prose — is what the guard classified.
    target_label: str | None
    risk: str
    created_at: datetime
    decision: ApprovalDecision = "pending"
    decided_at: datetime | None = None


def approval_request_for(
    turn: AgentTurn,
    *,
    goal: str,
    mission_id: str | None,
    target_label: str | None,
    risk: str,
    now: datetime,
) -> ApprovalRequest:
    """Build the request an unattended run parks (pure).

    The id carries the timestamp first so a directory listing is chronological,
    then the sub-goal, so a queue of five parked actions reads as five
    recognisable tasks rather than five hashes.
    """
    stamp = now.strftime("%Y%m%dt%H%M%S")
    label = ascii_slug(
        turn.sub_goal or turn.action.type, max_chars=SUB_GOAL_SLUG_MAX_CHARS
    )
    request_id = f"{stamp}-{label}" if label else stamp
    return ApprovalRequest(
        request_id=request_id,
        mission_id=mission_id,
        goal=goal,
        sub_goal=turn.sub_goal,
        action_type=turn.action.type,
        action=turn.action.model_dump(exclude_none=True),
        target_label=target_label,
        risk=risk,
        created_at=now,
    )


def pending_requests(
    requests: tuple[ApprovalRequest, ...],
) -> tuple[ApprovalRequest, ...]:
    """Those still waiting on a human, oldest first (pure)."""
    waiting = [request for request in requests if request.decision == "pending"]
    return tuple(sorted(waiting, key=lambda request: request.created_at))


def requests_for_mission(
    requests: tuple[ApprovalRequest, ...], mission_id: str
) -> tuple[ApprovalRequest, ...]:
    """Every request raised by one mission, oldest first (pure)."""
    owned = [request for request in requests if request.mission_id == mission_id]
    return tuple(sorted(owned, key=lambda request: request.created_at))


def goals_awaiting_decision(
    requests: tuple[ApprovalRequest, ...],
) -> frozenset[str]:
    """Goals that already have an unanswered question against them (pure).

    A parked run is still recorded as a failed episode, because the work it did
    before the question is worth keeping (Law 4.1) — and that record is exactly
    what an unattended session's own goal-proposer reads. Measured on a live
    two-run session: run one parked "delete the stale export"; run two saw the
    failed episode it had just written, proposed the same goal, walked to the
    same action and parked an identical second request. Its mission was
    correctly held back, so the block leaked in through the episode channel
    instead.

    Asking a person the same question twice while they have not answered the
    first is not autonomy, it is noise, so a proposer consults this set.
    """
    return frozenset(request.goal for request in pending_requests(requests))


def decided(
    request: ApprovalRequest, *, approved: bool, now: datetime
) -> ApprovalRequest:
    """A human's answer, recorded (pure).

    An already-decided request is returned unchanged. Re-deciding one would let
    a "yes" typed today re-open a question that was answered "no" yesterday,
    and the queue is a record of what was actually authorised.
    """
    if request.decision != "pending":
        return request
    return request.model_copy(
        update={
            "decision": "approved" if approved else "denied",
            "decided_at": now,
        }
    )


class ApprovalRequiredError(RuntimeError):
    """A run reached an action only a human may authorise, and none was there.

    Deliberately distinct from
    :class:`~computeruse.security.permissions.PermissionDeniedError`: that one
    means a human said no, and re-proposing the action is the agent arguing
    with its operator. This one means nobody was asked yet — the work is
    *parked*, not refused, and the mission stays resumable.
    """

    def __init__(self, *, request: ApprovalRequest) -> None:
        self.request = request
        super().__init__(
            f"action {request.action_type!r} for {request.sub_goal!r} needs a "
            f"human decision and no one is attended; parked as approval "
            f"request {request.request_id!r}"
        )


class ApprovalQueue:
    """The on-disk approval queue (Law 6.1 connector).

    One JSON file per request, in the same one-file-per-record shape the
    episodic and semantic stores use, so the whole store is one mental model
    and a person can read a parked action with ``cat``.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def directory(self) -> Path:
        return self._directory

    def submit(self, request: ApprovalRequest) -> Path:
        """Write a request to the queue and return where it landed."""
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self._directory / f"{request.request_id}.json"
        target.write_text(request.model_dump_json(indent=2) + "\n", encoding="utf-8")
        LOGGER.info(
            "approval parked: %s (%s for %r)",
            request.request_id,
            request.action_type,
            request.sub_goal,
        )
        return target

    def requests(self) -> tuple[ApprovalRequest, ...]:
        """Every request on disk; unreadable files are skipped with a warning.

        A queue that refused to list because one file was corrupt would hide
        every other parked action behind one bad record — and these are the
        records a person consults precisely when something has gone wrong.
        """
        if not self._directory.is_dir():
            return ()
        found: list[ApprovalRequest] = []
        for path in sorted(self._directory.glob("*.json")):
            try:
                found.append(
                    ApprovalRequest.model_validate(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                )
            except (OSError, ValueError) as exc:
                LOGGER.warning("unreadable approval request %s: %s", path, exc)
        return tuple(found)

    def resolve(self, request_id: str, *, approved: bool, now: datetime) -> ApprovalRequest:
        """Record a human's decision on one request.

        Raises :class:`KeyError` for an id the queue does not hold: answering a
        question nobody asked is a caller bug, and silently creating the record
        would put an "approval" on disk that no run ever requested.
        """
        path = self._directory / f"{request_id}.json"
        if not path.is_file():
            raise KeyError(f"no approval request {request_id!r} in {self._directory}")
        request = ApprovalRequest.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
        answered = decided(request, approved=approved, now=now)
        path.write_text(answered.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return answered


def now_utc() -> datetime:
    """The clock, in one place, so the pure functions never reach for it."""
    return datetime.now(UTC)
