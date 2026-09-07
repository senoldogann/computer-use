"""The deferred approval queue: the third ending for an unattended run.

Before this there were two, both bad. With a terminal attached the run called
the CLI's confirmation handler, which blocks on ``stdin.readline()`` — and
``isatty()`` is true long after the human has gone home, so the session froze
mid-task holding the machine. Without one the guard raised, the run ended, and
nothing recorded what it had wanted to do.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from computeruse.orchestrator.loop import OodaRunner, WorkingState
from computeruse.orchestrator.schemas import AgentTurn, CallTool, MouseClick
from computeruse.security.approvals import (
    ApprovalQueue,
    ApprovalRequest,
    ApprovalRequiredError,
    approval_request_for,
    decided,
    find_usable_approval,
    goals_awaiting_decision,
    pending_requests,
    requests_for_mission,
)
from computeruse.security.autonomy import PermissionDecision, classify_risk

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _turn(sub_goal: str = "delete the stale export") -> AgentTurn:
    return AgentTurn(
        thought="the flow needs this",
        sub_goal=sub_goal,
        action=MouseClick(type="mouse_click", x=120, y=90),
    )


def _request(
    *, mission_id: str | None = "m-1", sub_goal: str = "delete the stale export",
    now: datetime = NOW,
) -> object:
    turn = _turn(sub_goal)
    return approval_request_for(
        turn,
        goal="tidy the exports folder",
        mission_id=mission_id,
        target_label="Delete Permanently",
        risk=classify_risk(turn, target_label="Delete Permanently").value,
        now=now,
    )


# --- the record itself ------------------------------------------------------


def test_a_parked_request_is_answerable_without_replaying_the_run() -> None:
    """A queue entry saying only "an action needs approval" is unanswerable.

    The control's accessibility title is the part that matters: it is what the
    guard classified, and the one account of the action that is not the
    model's own prose.
    """
    request = _request()
    assert request.target_label == "Delete Permanently"
    assert request.risk == "destructive"
    assert request.action["x"] == 120
    assert request.sub_goal == "delete the stale export"
    assert request.goal == "tidy the exports folder"
    assert request.decision == "pending"


def test_the_exact_action_is_recorded_not_just_its_type() -> None:
    """So an approval cannot silently apply to a different action later."""
    turn = AgentTurn(
        thought="t",
        sub_goal="run the cleanup",
        action=CallTool(type="call_tool", tool="bash", arguments={"command": "rm -rf /tmp/x"}),
    )
    request = approval_request_for(
        turn, goal="g", mission_id=None, target_label=None,
        risk=classify_risk(turn).value, now=NOW,
    )
    assert request.action["tool"] == "bash"
    assert request.action["arguments"] == {"command": "rm -rf /tmp/x"}
    assert request.risk == "destructive"


def test_a_turkish_sub_goal_still_produces_a_valid_request_id() -> None:
    """The queue names a file after the sub-goal, and ids are pattern-checked."""
    request = _request(sub_goal="Eski dışa aktarımı sil")
    assert request.request_id.endswith("eski-disa-aktarimi-sil")


# --- decisions --------------------------------------------------------------


def test_a_decision_is_recorded_once_and_stays_recorded() -> None:
    """A "yes" typed today must not re-open a question answered "no" yesterday."""
    request = _request()
    refused = decided(request, approved=False, now=NOW)
    assert refused.decision == "denied"
    later = decided(refused, approved=True, now=NOW + timedelta(days=1))
    assert later.decision == "denied", "an answered question stays answered"


def test_pending_lists_only_what_is_still_waiting_oldest_first() -> None:
    first = _request(sub_goal="first thing", now=NOW)
    second = _request(sub_goal="second thing", now=NOW + timedelta(hours=1))
    answered = decided(_request(sub_goal="third thing", now=NOW), approved=True, now=NOW)
    waiting = pending_requests((second, answered, first))
    assert [r.sub_goal for r in waiting] == ["first thing", "second thing"]


def test_requests_can_be_traced_back_to_their_mission() -> None:
    mine = _request(mission_id="m-1", sub_goal="mine")
    theirs = _request(mission_id="m-2", sub_goal="theirs")
    orphan = _request(mission_id=None, sub_goal="orphan")
    assert [r.sub_goal for r in requests_for_mission((mine, theirs, orphan), "m-1")] == [
        "mine"
    ]


# --- the typed error --------------------------------------------------------


def test_the_parked_error_says_what_is_waiting_and_why() -> None:
    """Distinct from a denial: nobody said no, nobody was asked."""
    request = _request()
    error = ApprovalRequiredError(request=request)
    assert error.request is request
    message = str(error)
    assert "delete the stale export" in message
    assert request.request_id in message


# --- the store --------------------------------------------------------------


def test_requests_round_trip_on_disk(tmp_path: Path) -> None:
    queue = ApprovalQueue(tmp_path)
    request = _request()
    queue.submit(request)
    assert queue.requests() == (request,)


def test_resolving_writes_the_answer_back(tmp_path: Path) -> None:
    queue = ApprovalQueue(tmp_path)
    request = _request()
    queue.submit(request)
    answered = queue.resolve(request.request_id, approved=True, now=NOW)
    assert answered.decision == "approved"
    assert queue.requests()[0].decision == "approved"
    assert pending_requests(queue.requests()) == ()


def test_answering_a_question_nobody_asked_raises(tmp_path: Path) -> None:
    """Silently creating the record would put an approval on disk for a run
    that never requested one."""
    with pytest.raises(KeyError, match="no approval request"):
        ApprovalQueue(tmp_path).resolve("nope", approved=True, now=NOW)


def test_one_corrupt_request_does_not_hide_the_rest(tmp_path: Path) -> None:
    queue = ApprovalQueue(tmp_path)
    good = _request()
    queue.submit(good)
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert queue.requests() == (good,)


def test_an_empty_queue_is_not_an_error(tmp_path: Path) -> None:
    assert ApprovalQueue(tmp_path / "never-created").requests() == ()


# --- the runner integration -------------------------------------------------


def test_the_runner_parks_instead_of_hanging_or_dying() -> None:
    """The whole point, exercised through the loop.

    The handler writes the question down and raises; the runner lets that out
    typed, so the caller can tell "this needs you" from "this could not be
    done" — and the episode of the work done before the question is still
    recorded (Law 4.1).
    """
    queue_dir: list[ApprovalRequest] = []
    recorded: list[tuple[str, str | None]] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="open the folder",
                action=MouseClick(type="mouse_click", x=5, y=5),
            )
        return AgentTurn(
            thought="now the risky one",
            sub_goal="delete the stale export",
            action=MouseClick(type="mouse_click", x=120, y=90),
        )

    def guard(turn: AgentTurn, _observation: object) -> PermissionDecision:
        if "delete" in turn.sub_goal:
            return PermissionDecision.CONFIRM
        return PermissionDecision.ALLOW

    def park(turn: AgentTurn, target_label: str | None) -> bool:
        request = approval_request_for(
            turn,
            goal="tidy the exports folder",
            mission_id="m-1",
            target_label=target_label,
            risk="destructive",
            now=NOW,
        )
        queue_dir.append(request)
        raise ApprovalRequiredError(request=request)

    def on_complete(
        _trajectory: object,
        outcome: str,
        retrospective: str | None,
        _skill: object,
        _forced: object,
    ) -> None:
        recorded.append((outcome, retrospective))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        guard=guard,  # type: ignore[arg-type]
        confirm_handler=park,
        on_complete=on_complete,  # type: ignore[arg-type]
        max_steps=5,
    )
    with pytest.raises(ApprovalRequiredError) as excinfo:
        runner.run(goal="tidy the exports folder")

    assert len(queue_dir) == 1, "the question was written down exactly once"
    assert queue_dir[0].sub_goal == "delete the stale export"
    assert excinfo.value.request is queue_dir[0]
    # Law 4.1: the work done before the question is not thrown away, and the
    # retrospective calls it a pause rather than a failure so the next attempt
    # does not plan around an obstacle that is only an unanswered question.
    assert recorded, "the run before the question was never recorded"
    outcome, retrospective = recorded[0]
    assert outcome == "failure"
    assert retrospective is not None
    assert "paused for human approval" in retrospective
    assert queue_dir[0].request_id in retrospective


def test_approved_request_matches_only_the_exact_decision() -> None:
    """An approval must not silently apply to a different action."""
    turn = _turn("delete the stale export")
    base = _request(sub_goal="delete the stale export")
    approved = decided(base, approved=True, now=NOW)
    assert isinstance(approved, ApprovalRequest)
    assert find_usable_approval((approved,), turn, "Delete Permanently") is approved
    moved = AgentTurn(
        thought="the flow needs this",
        sub_goal="delete the stale export",
        action=MouseClick(type="mouse_click", x=999, y=90),
    )
    assert find_usable_approval((approved,), moved, "Delete Permanently") is None
    assert find_usable_approval((approved,), turn, "Other Button") is None
    other_goal = AgentTurn(
        thought="the flow needs this",
        sub_goal="delete something else",
        action=MouseClick(type="mouse_click", x=120, y=90),
    )
    assert find_usable_approval((approved,), other_goal, "Delete Permanently") is None


def test_only_approved_requests_are_usable() -> None:
    turn = _turn("delete the stale export")
    pending = _request(sub_goal="delete the stale export")
    assert isinstance(pending, ApprovalRequest)
    assert find_usable_approval((pending,), turn, "Delete Permanently") is None
    denied = decided(_request(sub_goal="delete the stale export"), approved=False, now=NOW)
    assert find_usable_approval((denied,), turn, "Delete Permanently") is None


def test_consumed_approval_cannot_be_reused(tmp_path: Path) -> None:
    queue = ApprovalQueue(tmp_path)
    request = _request(sub_goal="delete the stale export")
    assert isinstance(request, ApprovalRequest)
    queue.submit(request)
    queue.resolve(request.request_id, approved=True, now=NOW)
    turn = _turn("delete the stale export")
    usable = find_usable_approval(queue.requests(), turn, "Delete Permanently")
    assert usable is not None
    queue.consume(usable.request_id, now=NOW)
    assert find_usable_approval(queue.requests(), turn, "Delete Permanently") is None


def test_guarded_uses_a_matching_approval_single_use(tmp_path: Path) -> None:
    """The guard turns one CONFIRM into ALLOW and consumes the token."""
    from computeruse.agent import guarded
    from computeruse.orchestrator.loop import EMPTY_OBSERVATION
    from computeruse.security.autonomy import AutonomyLevel

    queue = ApprovalQueue(tmp_path)
    request = _request(sub_goal="delete the stale export")
    assert isinstance(request, ApprovalRequest)
    queue.submit(request)
    queue.resolve(request.request_id, approved=True, now=NOW)

    def consume_approval(turn: AgentTurn, label: str | None) -> ApprovalRequest | None:
        matched = find_usable_approval(queue.requests(), turn, label)
        if matched is None:
            return None
        return queue.consume(matched.request_id, now=NOW)

    guard = guarded(
        AutonomyLevel.FULL,
        authorize=None,
        consume_approval=consume_approval,
    )
    turn = AgentTurn(
        thought="t",
        sub_goal="delete the stale export",
        action=MouseClick(type="mouse_click", x=120, y=90),
    )
    # No AX element covers the point, so the label is None; the stored
    # request used "Delete Permanently" and must NOT match here. Submit a
    # matching None-label approval for the single-use check instead.
    none_request = approval_request_for(
        turn, goal="g", mission_id=None, target_label=None,
        risk="destructive", now=NOW,
    )
    queue.submit(none_request)
    queue.resolve(none_request.request_id, approved=True, now=NOW)
    assert guard(turn, EMPTY_OBSERVATION) is PermissionDecision.ALLOW
    assert guard(turn, EMPTY_OBSERVATION) is PermissionDecision.CONFIRM


def test_a_parked_goal_is_not_proposed_again_while_it_waits() -> None:
    """Observed live, and the block leaked in through the episode channel.

    A parked run is still recorded as a failed episode, because the work it did
    before the question is worth keeping. That record is exactly what an
    unattended session's goal-proposer reads — so in a two-run session, run one
    parked "delete the stale export" and run two saw the failed episode it had
    just written, proposed the same goal, walked to the same action and parked
    an identical second request. The mission was correctly held back; nothing
    held back the episode.
    """
    parked = _request(sub_goal="delete the stale export")
    answered = decided(
        _request(sub_goal="an older question"), approved=True, now=NOW
    )
    waiting = goals_awaiting_decision((parked, answered))
    assert parked.goal in waiting
    # An answered question stops blocking its goal, or approving one would
    # retire the work it was asked about.
    assert goals_awaiting_decision((answered,)) == frozenset()
