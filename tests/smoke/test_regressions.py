"""Regression tests for the adversarial-audit findings (F1-F4).

Each test pins the *bug* that was found, so a future refactor that re-introduces
it fails here before it can bite in production.
"""

from __future__ import annotations

import socket
import time

from computeruse.orchestrator.client import ActuationClient
from computeruse.orchestrator.loop import OodaRunner, WorkingState
from computeruse.orchestrator.schemas import (
    AgentTurn,
    MouseClick,
    MouseMove,
    PressHotkey,
    TypeText,
)
from computeruse.skills.distiller import Trajectory, signature_of
from tests.smoke.conftest import SOCKET_PATH, rpc_call


# ---------------------------------------------------------------- F1: signature
def _sig(steps: tuple[object, ...], app: str = "Numbers") -> str:
    typed = tuple(steps)  # type: ignore[arg-type]
    return signature_of(Trajectory(app=app, description="x", steps=typed))  # type: ignore[arg-type]


def test_f1_same_action_types_different_text_distinct_signatures() -> None:
    """type_text with different payloads must NOT collide (F1)."""
    a = _sig((MouseClick(type="mouse_click", x=5, y=5), TypeText(type="type_text", text="save", wpm=40)))
    b = _sig((MouseClick(type="mouse_click", x=5, y=5), TypeText(type="type_text", text="delete", wpm=40)))
    assert a != b, "workflows typing different text must have distinct signatures"


def test_f1_same_action_types_different_hotkey_distinct_signatures() -> None:
    a = _sig((PressHotkey(type="press_hotkey", modifiers=["command"], key="c"),))
    b = _sig((PressHotkey(type="press_hotkey", modifiers=["command"], key="v"),))
    assert a != b, "copy vs paste must not be treated as the same workflow"


def test_f1_same_flow_same_signature_still_dedups() -> None:
    """The de-dup property (identical flows) must survive the fix."""
    steps = (
        MouseClick(type="mouse_click", x=5, y=5),
        TypeText(type="type_text", text="save", wpm=40),
    )
    assert _sig(steps) == _sig(steps)


def test_f1_coordinate_drift_still_same_signature() -> None:
    """UI coordinates drift between runs; signature must stay stable (pinned)."""
    a = _sig((MouseClick(type="mouse_click", x=10, y=10),))
    b = _sig((MouseClick(type="mouse_click", x=999, y=888),))
    assert a == b, "coordinate drift must not break de-dup"


# ---------------------------------------------------------------- F2: loop state
def test_f2_failed_step_not_recorded_as_completed() -> None:
    """A raising action must NOT appear in completed_steps (F2)."""

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index >= 2:
            return AgentTurn.model_validate(
                {"thought": "", "sub_goal": "", "action": {"type": "finish", "status": "success", "summary": "done"}}
            )
        return AgentTurn.model_validate(
            {"thought": "", "sub_goal": "", "action": {"type": "mouse_click", "x": 1, "y": 1}}
        )

    def boom(_action: object) -> None:
        raise RuntimeError("driver gone")

    runner = OodaRunner(provider=provider, execute_physical=boom, max_steps=3)
    final = runner.run(goal="retry")
    # Only the terminal finish may appear — neither failed click may leak in.
    assert final.completed_steps == ("step_2:finish",), (
        f"failed step leaked into completed_steps: {final.completed_steps}"
    )
    assert final.last_error is not None


def test_f2_successful_steps_recorded_failed_ones_are_not() -> None:
    """Mixed outcome: only successes land in completed_steps."""

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn.model_validate(
                {"thought": "", "sub_goal": "", "action": {"type": "mouse_click", "x": 1, "y": 1}}
            )
        if state.step_index == 1:
            return AgentTurn.model_validate(
                {"thought": "", "sub_goal": "", "action": {"type": "mouse_move", "x": 2, "y": 2}}
            )
        return AgentTurn.model_validate(
            {"thought": "", "sub_goal": "", "action": {"type": "finish", "status": "success", "summary": "done"}}
        )

    def execute(action: object) -> None:
        # Fail the second physical step, succeed the first.
        if isinstance(action, MouseMove):
            raise RuntimeError("boom")  # noqa: TRY004 - simulates a physical actuation failure

    runner = OodaRunner(provider=provider, execute_physical=execute, max_steps=10)
    final = runner.run(goal="mixed")
    recorded = [s for s in final.completed_steps]
    assert any("mouse_click" in s for s in recorded), "successful step missing"
    assert not any("mouse_move" in s for s in recorded), "failed step recorded as completed"


# ---------------------------------------------------------------- F3: buffer discard
def test_f3_pipelined_responses_not_discarded() -> None:
    """Two responses arriving in ONE recv must both be readable (F3)."""
    server, client_sock = socket.socketpair()
    client = ActuationClient("/unused", connect_retries=1)
    client._sock = client_sock  # type: ignore[attr-defined]  # test seam
    try:
        # Both responses arrive in a single write; a naive read discards the 2nd.
        server.sendall(b'{"ok":"ack"}\n{"ok":"pong"}\n')
        first = client._read_response()
        assert first == {"ok": "ack"}
        second = client._read_response()
        assert second == {"ok": "pong"}, f"pipelined response lost: {second}"
    finally:
        server.close()
        client.close()


def test_f3_split_response_reassembled() -> None:
    """A response split across recv() calls must still parse correctly."""
    server, client_sock = socket.socketpair()
    client = ActuationClient("/unused", connect_retries=1)
    client._sock = client_sock  # type: ignore[attr-defined]
    try:
        payload = b'{"ok":"ack"}'
        server.sendall(payload[:5])
        server.sendall(payload[5:] + b"\n")
        assert client._read_response() == {"ok": "ack"}
    finally:
        server.close()
        client.close()


# ---------------------------------------------------------------- F4: threaded driver
def test_f4_second_connection_served_while_first_idle() -> None:
    """A held-open idle connection must not block other clients (F4)."""
    blocker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    blocker.settimeout(5.0)
    blocker.connect(str(SOCKET_PATH))
    try:
        # Now a second connection must still get a response promptly.
        started = time.monotonic()
        response = rpc_call({"method": "ping"})
        assert response.get("ok") == "pong"
        assert time.monotonic() - started < 3.0, "second connection starved"
    finally:
        blocker.close()