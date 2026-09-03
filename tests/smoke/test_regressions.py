"""Regression tests for the adversarial-audit findings (F1-F4).

Each test pins the *bug* that was found, so a future refactor that re-introduces
it fails here before it can bite in production.
"""

from __future__ import annotations

import logging
import socket
import time

import pytest

from computeruse.orchestrator.client import ActuationClient
from computeruse.orchestrator.loop import (
    MaxStepsError,
    OodaRunner,
    UnrecoverableFailureError,
    WorkingState,
)
from computeruse.orchestrator.prompts import InvalidDecisionError
from computeruse.orchestrator.schemas import (
    Action,
    ActivateApp,
    AgentTurn,
    Finish,
    MouseClick,
    MouseMove,
    MouseScroll,
    PressHotkey,
    TypeText,
)
from computeruse.skills.distiller import Trajectory, signature_of
from computeruse.vision import FocusedWindow
from computeruse.vision.coordinates import Point
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
        first = client._read_response("test", timeout_seconds=5.0)
        assert first == {"ok": "ack"}
        second = client._read_response("test", timeout_seconds=5.0)
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
        assert client._read_response("test", timeout_seconds=5.0) == {"ok": "ack"}
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

# --- Per-action deadlines and stream integrity -------------------------------


def test_action_deadline_covers_human_paced_typing() -> None:
    """A long ``type_text`` must not time out on a perfectly healthy driver.

    The driver paces keystrokes at human speed by design, so a 400-character
    paste legitimately takes minutes. Under the old flat 10s deadline the
    client gave up mid-word, and — worse — kept using the same socket, so the
    driver's late ACK was read as the answer to the *next* request and every
    response after that answered the wrong question.
    """
    from computeruse.orchestrator.client import action_timeout_seconds
    from computeruse.orchestrator.schemas import ActivateApp, MouseClick, TypeText

    long_text = "x" * 400
    assert action_timeout_seconds(TypeText(type="type_text", text=long_text, wpm=40)) > 120
    # A short action keeps a tight deadline: a hung driver must not stall a run.
    assert action_timeout_seconds(MouseClick(type="mouse_click", x=1, y=1)) <= 15
    # ``open -a`` may cold-launch an application.
    assert action_timeout_seconds(ActivateApp(type="activate_app", app="Safari")) >= 30


def test_timeout_drops_the_socket_instead_of_desyncing_it() -> None:
    """An expired read abandons the stream rather than reusing it.

    Keeping the socket would pair the driver's late reply with the following
    request forever. Dropping it costs one reconnect; misattributing every
    later response costs the run.
    """
    import socket

    from computeruse.orchestrator.client import DriverTimeoutError

    server, client_sock = socket.socketpair()
    client = ActuationClient("/unused", connect_retries=1)
    client._sock = client_sock  # type: ignore[attr-defined]  # test seam
    try:
        # The server never answers; the deadline must fire and reset the stream.
        with pytest.raises(DriverTimeoutError, match="did not answer"):
            client._read_response("screenshot", timeout_seconds=0.05)
        assert not client.is_connected, "a timed-out stream must not be reused"
    finally:
        server.close()
        client.close()


def test_a_malformed_model_turn_does_not_kill_the_run() -> None:
    """One unusable reply must cost a step, not the whole run.

    Observed live: on step 20 of a 30-step goal the model returned no parseable
    JSON, the error escaped the loop, and the process died with a traceback —
    discarding twenty steps of correct work. A model that returns nothing
    usable is a failure like any other and climbs the same finite ladder.
    """
    turns: list[int] = []

    def provider(state: WorkingState) -> AgentTurn:
        turns.append(state.step_index)
        if len(turns) == 1:
            raise InvalidDecisionError(
                cause="no JSON object found in the reply",
                hint="reply with one JSON object",
            )
        return AgentTurn(
            thought="recovered",
            sub_goal="finish",
            action=Finish(type="finish", status="success", summary="done"),
        )

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        max_steps=5,
    )
    final = runner.run(goal="survive a bad turn")
    assert len(turns) == 2
    # The bad turn is visible to the model as a recoverable error, and the run
    # still finishes.
    assert "step_1:finish" in final.completed_steps or final.completed_steps


def test_repeated_malformed_turns_still_terminate() -> None:
    """The ladder must stay finite: never a run that loops on a broken model."""

    def provider(_state: WorkingState) -> AgentTurn:
        raise InvalidDecisionError(cause="garbage", hint="reply with JSON")

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        max_steps=50,
    )
    with pytest.raises((UnrecoverableFailureError, MaxStepsError)):
        runner.run(goal="never gets a decision")


def test_background_mode_bypasses_the_focus_gate() -> None:
    """The quiet path is tried before the gate that fronts the app.

    That gate exists because a synthetic click goes to whatever is frontmost,
    so it brings the target forward first — precisely what background mode is
    for avoiding. Guarding first fronted the app on every action and gave the
    whole benefit back; a live run finished correctly with the window pulled
    to the foreground.
    """
    pressed: list[tuple[float, float]] = []
    clicked: list[Action] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="",
                sub_goal="press it",
                action=MouseClick(type="mouse_click", x=30, y=16),
            )
        return AgentTurn(
            thought="",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    def quiet_press(point: Point) -> bool:
        pressed.append((point.x, point.y))
        return True

    runner = OodaRunner(
        provider=provider,
        execute_physical=clicked.append,
        quiet_press=quiet_press,
        max_steps=5,
    )
    runner.run(goal="press quietly")
    assert pressed == [(30, 16)]
    # The synthetic click never ran: no cursor moved, nothing was fronted.
    assert clicked == []


def test_a_declined_quiet_press_falls_back_to_a_real_click() -> None:
    """It can only ever add reach: what AX refuses still gets clicked."""
    clicked: list[Action] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="",
                sub_goal="press it",
                action=MouseClick(type="mouse_click", x=30, y=16),
            )
        return AgentTurn(
            thought="",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    runner = OodaRunner(
        provider=provider,
        execute_physical=clicked.append,
        quiet_press=lambda _point: False,
        max_steps=5,
    )
    runner.run(goal="press loudly")
    assert [a.type for a in clicked] == ["mouse_click"]


def test_only_a_plain_left_click_takes_the_quiet_path() -> None:
    """Drags, scrolls and multi-clicks have no accessibility equivalent."""
    runner = OodaRunner(
        provider=lambda _s: AgentTurn(
            thought="", sub_goal="", action=Finish(type="finish", status="success", summary="")
        ),
        execute_physical=lambda _a: None,
        quiet_press=lambda _p: True,
    )
    assert runner._pressed_quietly(MouseClick(type="mouse_click", x=1, y=1)) is True
    assert (
        runner._pressed_quietly(MouseClick(type="mouse_click", x=1, y=1, click_count=2)) is False
    )
    assert (
        runner._pressed_quietly(MouseClick(type="mouse_click", x=1, y=1, button="right")) is False
    )
    assert runner._pressed_quietly(TypeText(type="type_text", text="x", wpm=40)) is False


def test_background_mode_refuses_to_front_the_app() -> None:
    """The mode's one promise cannot depend on the model reading prose.

    A run told in its prompt not to bring the target forward emitted
    activate_app anyway. Actuation reaches the app wherever it is, so fronting
    it buys nothing and costs the exact thing the mode protects.
    """
    executed: list[Action] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="",
                sub_goal="bring it forward",
                action=ActivateApp(type="activate_app", app="Calculator"),
            )
        return AgentTurn(
            thought="",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    runner = OodaRunner(
        provider=provider,
        execute_physical=executed.append,
        quiet_press=lambda _p: True,
        max_steps=5,
    )
    runner.run(goal="stay in the background")
    assert executed == []

    # Without the mode it is an ordinary action and still runs.
    executed.clear()
    OodaRunner(
        provider=provider, execute_physical=executed.append, max_steps=5
    ).run(goal="ordinary run")
    assert [a.type for a in executed] == ["activate_app"]


def test_text_takes_the_quiet_path_in_background_mode() -> None:
    """Typing goes to whatever is frontmost, so it needed its own quiet path.

    Without it the mode covered clicks only: the focus gate had to front the
    target for the keystrokes to land, and the run silently stopped being a
    background run the first time the agent typed.
    """
    written: list[str] = []
    executed: list[Action] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="",
                sub_goal="type it",
                action=TypeText(type="type_text", text="hello", wpm=40),
            )
        return AgentTurn(
            thought="",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    runner = OodaRunner(
        provider=provider,
        execute_physical=executed.append,
        quiet_type=lambda text: bool(written.append(text)) or True,
        max_steps=5,
    )
    runner.run(goal="type quietly")
    assert written == ["hello"]
    assert executed == []


def test_a_declined_quiet_write_still_types() -> None:
    executed: list[Action] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="",
                sub_goal="type it",
                action=TypeText(type="type_text", text="hello", wpm=40),
            )
        return AgentTurn(
            thought="",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    OodaRunner(
        provider=provider,
        execute_physical=executed.append,
        quiet_type=lambda _text: False,
        max_steps=5,
    ).run(goal="type loudly")
    assert [a.type for a in executed] == ["type_text"]


def test_leaving_the_background_is_announced(caplog) -> None:
    """A promise that stops holding must not stop holding silently."""
    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="",
                sub_goal="scroll",
                action=MouseScroll(type="mouse_scroll", dx=0, dy=100),
            )
        return AgentTurn(
            thought="",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _a: None,
        quiet_press=lambda _p: True,
        app="Calculator",
        max_steps=5,
    )
    with caplog.at_level(logging.WARNING):
        runner.run(goal="scroll in the background")
    assert any("coming to the front" in record.getMessage() for record in caplog.records)


def test_leaving_the_background_actually_brings_the_target_forward() -> None:
    """Saying it and doing it have to be the same act.

    The announcement claimed the target was "coming to the front" and nothing
    brought it: only pointer actions reach the focus gate, so a hotkey went
    into the global event stream aimed at whichever window happened to own the
    screen. Measured on a real run, an agent pressed Cmd+L and Return thirty
    times against a browser it never fronted.
    """
    executed: list[Action] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="",
                sub_goal="focus the address bar",
                action=PressHotkey(type="press_hotkey", modifiers=["command"], key="l"),
            )
        return AgentTurn(
            thought="",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    OodaRunner(
        provider=provider,
        execute_physical=executed.append,
        quiet_press=lambda _p: True,
        app="Google Chrome",
        max_steps=5,
    ).run(goal="open the address bar")
    assert [a.type for a in executed] == ["activate_app", "press_hotkey"]


def test_the_quiet_write_stands_down_once_the_keyboard_is_here() -> None:
    """It buys nothing when the target already owns the screen, and costs fidelity.

    Measured on Chrome's address bar with the app frontmost: the accessibility
    write returned true, the field showed nothing, Return did nothing, and the
    agent repeated the sequence thirty-three times — a write that reports
    success and changes nothing verifies as inconclusive, and inconclusive
    never fails an action. Real keystrokes navigated on the first try.
    """
    executed: list[Action] = []
    quiet_calls: list[str] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="",
                sub_goal="type the address",
                action=TypeText(type="type_text", text="example.com", wpm=40),
            )
        return AgentTurn(
            thought="",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    def quiet_type(text: str) -> bool:
        quiet_calls.append(text)
        return True

    OodaRunner(
        provider=provider,
        execute_physical=executed.append,
        quiet_type=quiet_type,
        frontmost_probe=lambda: FocusedWindow(
            pid=1, app_name="Google Chrome", bundle_id="com.google.Chrome"
        ),
        app="Google Chrome",
        max_steps=5,
    ).run(goal="type the address")
    assert quiet_calls == [], "the quiet write must not be attempted"
    assert [a.type for a in executed] == ["type_text"]
