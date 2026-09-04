"""Pure unit tests for the OODA loop (Law 6: no OS I/O).

``decide_step`` is a pure function, so these run without touching the driver.
The imperative shell (``OodaRunner``) tests use injected fakes so execution
stays deterministic and offline. The stuck-loop guard and the max-steps
termination contract are pinned here too: a degenerate provider must never
make the loop run forever, and a truncated run must never be silent.
"""

from __future__ import annotations

import logging

import pytest

from computeruse.orchestrator.failures import FailureKind, UnrecoverableFailureError
from computeruse.orchestrator.loop import (
    TOOL_REPEAT_ABORT_AFTER,
    TOOL_REPEAT_WARN_AFTER,
    AxProbeResult,
    MaxStepsError,
    OodaRunner,
    StuckLoopError,
    WorkingState,
    _extend_trail,
    decide_step,
    equivalent_action,
    map_action_to_screen,
    repetition_diagnostic,
    same_physical_action,
)
from computeruse.orchestrator.prompts import completion_auditor, completion_prompt
from computeruse.orchestrator.schemas import (
    AgentTurn,
    CallTool,
    Finish,
    MouseClick,
    MouseMove,
    Wait,
)
from computeruse.vision import Point, ScreenCapture, ScreenMap, Size
from computeruse.vision.focus import FocusedWindow


def _turn(action: object) -> AgentTurn:
    return AgentTurn.model_validate({"thought": "", "sub_goal": "", "action": action})

def _click(x: int, y: int) -> MouseClick:
    return MouseClick(type="mouse_click", x=x, y=y)


def test_decide_step_is_pure_and_routes_physical() -> None:
    start = WorkingState(goal="click")
    outcome = decide_step(start, _turn(MouseMove(type="mouse_move", x=10, y=10)))
    assert outcome.route == "physical"
    assert outcome.state.step_index == 1
    assert start.step_index == 0  # immutability: original untouched


def test_decide_step_routes_wait_as_internal() -> None:
    outcome = decide_step(
        WorkingState(goal="x"),
        _turn(Wait(type="wait", duration_ms=5, reason="settle")),
    )
    assert outcome.route == "internal_wait"


def test_runner_executes_physical_then_finishes() -> None:
    executed: list[str] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=1, y=1))
        return _turn(Finish(type="finish", status="success", summary="done"))

    def execute_physical(action: object) -> None:
        executed.append(str(action))

    runner = OodaRunner(provider=provider, execute_physical=execute_physical, max_steps=5)
    final = runner.run(goal="demo")
    assert final.step_index == 2
    assert executed, "physical action was never dispatched"


def test_runner_folds_failure_into_state() -> None:
    """A raising execute_physical failure must survive into the next state."""
    seen: list[str | None] = []

    def failing_provider(state: WorkingState) -> AgentTurn:
        seen.append(state.last_error)
        return _turn(MouseMove(type="mouse_move", x=0, y=0))

    def boom(_action: object) -> None:
        raise RuntimeError("driver gone")

    runner = OodaRunner(provider=failing_provider, execute_physical=boom, max_steps=3)
    # The provider never finishes, so the run now ends loudly (bounded
    # termination) instead of silently returning a truncated state.
    with pytest.raises(MaxStepsError):
        runner.run(goal="retry")
    # The failure was folded into the provider's second state, not swallowed.
    assert seen[0] is None
    assert seen[1] is not None and "driver gone" in seen[1]


def test_success_after_failure_clears_last_error() -> None:
    """M1: a verified success clears the folded failure from the working context.

    A recovery must not keep steering the provider around a failure that
    already resolved: the next turn sees ``last_error=None``, and a later
    terminal finish never resurfaces the stale failure either.
    """
    seen: list[str | None] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state.last_error)
        if state.step_index == 0:
            return _turn(_click(1, 1))
        if state.step_index == 1:
            return _turn(_click(2, 2))
        return _turn(Finish(type="finish", status="success", summary="recovered"))

    def execute_physical(action: object) -> None:
        if isinstance(action, MouseClick) and action.x == 1:
            raise RuntimeError("driver gone")

    runner = OodaRunner(provider=provider, execute_physical=execute_physical, max_steps=5)
    final = runner.run(goal="recover")
    # The failure was folded into the retry turn, not swallowed...
    assert seen[1] is not None and "driver gone" in seen[1]
    # ...and the successful retry cleared it for every later turn.
    assert seen[2] is None, "the successful retry must clear the folded failure"
    assert final.last_error is None, "a terminal finish must not resurface a recovered failure"


def test_same_physical_action_compares_full_payload() -> None:
    """Two clicks are identical only when every parameter matches."""
    assert same_physical_action(_click(10, 10), _click(10, 10))
    assert not same_physical_action(_click(10, 10), _click(11, 10))
    assert not same_physical_action(_click(10, 10), MouseMove(type="mouse_move", x=10, y=10))
    assert "action repetition detected" in repetition_diagnostic(_click(10, 10), 3)


def test_equivalent_action_tolerates_small_coordinate_jitter() -> None:
    """Pointer actions within one UI row are the same intent (guard regression).

    A lost model stuck on one target jitters its click coordinates by a few
    pixels per repeat (observed: 2-60px drift), which defeated the exact
    comparator and let it click forever. The guard now compares intent.
    """
    assert equivalent_action(_click(10, 10), _click(10, 10))
    # A 2px jitter is the same click.
    assert equivalent_action(_click(10, 10), _click(12, 11))
    # A 31px shift is still one row (within the 32px tolerance).
    assert equivalent_action(_click(145, 114), _click(173, 115))
    # A genuinely different target is a different action.
    assert not equivalent_action(_click(10, 10), _click(300, 300))
    # Different type is never equivalent even at identical coordinates.
    assert not equivalent_action(_click(10, 10), MouseMove(type="mouse_move", x=10, y=10))
    # Non-pointer actions keep the exact payload comparison.
    assert not equivalent_action(
        MouseMove(type="mouse_move", x=10, y=10, duration_ms=180),
        MouseMove(type="mouse_move", x=10, y=10, duration_ms=250),
    )


def _tool_call(tool: str = "tavily.search", query: str = "current BTC") -> CallTool:
    return CallTool(type="call_tool", tool=tool, arguments={"query": query})


def test_stuck_loop_catches_repeated_tool_calls() -> None:
    """Word-shuffled re-asks are the same question (guard regression).

    A stuck model re-asks "current BTC" as "BTC current", and the exact
    payload comparison reset the counter every time — the loop that asked
    six times in three minutes never tripped anything. The guard now
    compares the question, not the string.
    """
    same = _tool_call()
    assert equivalent_action(same, _tool_call())
    # Same words, shuffled order: still the same question.
    assert equivalent_action(same, _tool_call(query="BTC current"))
    assert equivalent_action(same, _tool_call(query="  Current   BTC?! "))
    # A different tool, a different key, or a genuinely different question.
    assert not equivalent_action(same, _tool_call(tool="brave.web_search"))
    assert not equivalent_action(
        same, CallTool(type="call_tool", tool="tavily.search", arguments={"q": "current BTC"})
    )
    assert not equivalent_action(same, _tool_call(query="weather in Berlin"))
    # A bare subset is still the same question, asked with less.
    assert equivalent_action(same, _tool_call(query="BTC"))
    # Non-string values only ever match exactly: a new page is a new question.
    paged = CallTool(
        type="call_tool", tool="tavily.search", arguments={"query": "x", "page": 1}
    )
    assert equivalent_action(
        paged,
        CallTool(
            type="call_tool", tool="tavily.search", arguments={"query": "x", "page": 1}
        ),
    )
    assert not equivalent_action(
        paged,
        CallTool(
            type="call_tool", tool="tavily.search", arguments={"query": "x", "page": 2}
        ),
    )
    # The tool-tier hint names the way out.
    assert "different question" in repetition_diagnostic(same, 3)


def test_tool_repeats_hint_on_the_third_and_refuse_on_the_fifth() -> None:
    """Law 2.2 tool tier: roomier than the physical tier, still finite."""
    assert TOOL_REPEAT_WARN_AFTER == 3
    assert TOOL_REPEAT_ABORT_AFTER == 5
    runner = OodaRunner(
        provider=lambda state: _turn(_tool_call()),
        execute_physical=lambda _action: None,
    )
    repeats = [runner._note_tool_call(_tool_call(), "g") for _ in range(4)]
    assert repeats[0] is None and repeats[1] is None
    assert repeats[2] is not None and "different question" in repeats[2]
    assert repeats[3] is not None
    with pytest.raises(StuckLoopError):
        runner._note_tool_call(_tool_call(), "g")
    # A different question starts a new count rather than continuing the old.
    runner._reset_tool_streak()
    assert runner._note_tool_call(_tool_call(query="weather in Berlin"), "g") is None


def test_map_action_to_screen_converts_image_picks_to_screen_points() -> None:
    """The coordinate gate converts image-space picks into real screen points."""
    doubling = ScreenMap(logical=Size(1024.0, 600.0), image=Size(512.0, 300.0))
    click = map_action_to_screen(_click(50, 25), doubling)
    assert isinstance(click, MouseClick)
    assert (click.x, click.y) == (100, 50)
    move = map_action_to_screen(MouseMove(type="mouse_move", x=10, y=20), doubling)
    assert isinstance(move, MouseMove)
    assert (move.x, move.y) == (20, 40)
    # An identity map is a passthrough (small display, no scaling and no
    # offset): the same object, not a copy.
    identity = ScreenMap(logical=Size(512.0, 300.0), image=Size(512.0, 300.0))
    click1 = _click(1, 2)
    assert map_action_to_screen(click1, identity) is click1
    # Non-coordinate actions pass through untouched.
    from computeruse.orchestrator.schemas import PressHotkey

    hotkey = PressHotkey(type="press_hotkey", key="return")
    assert map_action_to_screen(hotkey, doubling) is hotkey


def test_map_action_to_screen_carries_the_display_offset() -> None:
    """A pick on a secondary display lands on that display, not the primary one.

    Actuation coordinates are global. A screenshot of a display whose corner
    sits at x=1512 shows its own (0,0) as the desktop's (1512,0), so a click at
    image x=10 belongs at 1512+20 — applying only the scale would put it on the
    primary display's left edge.
    """
    secondary = ScreenMap(
        logical=Size(1024.0, 600.0),
        image=Size(512.0, 300.0),
        origin=Point(1512.0, 0.0),
    )
    click = map_action_to_screen(_click(10, 20), secondary)
    assert isinstance(click, MouseClick)
    assert (click.x, click.y) == (1532, 40)
    # A same-size map is still not an identity while the display is offset.
    offset_only = ScreenMap(
        logical=Size(512.0, 300.0),
        image=Size(512.0, 300.0),
        origin=Point(1512.0, 0.0),
    )
    assert not offset_only.is_identity
    shifted = map_action_to_screen(_click(5, 6), offset_only)
    assert isinstance(shifted, MouseClick)
    assert (shifted.x, shifted.y) == (1517, 6)


def test_runner_coordinate_gate_lands_model_picks_on_screen_points() -> None:
    """End to end: the model reports map-image coords, the driver clicks screen pts.

    A 1024x600 screen becomes a 512x300 screenshot map (factor 2.0). The
    provider points at image (50,25); the physical layer must receive the
    real screen point (100,50) — the exact failure that made the agent click
    the browser toolbar instead of the first Google result.
    """
    width, height = 1024, 600
    frame = ScreenCapture(
        display_id=0, width=width, height=height, scale=1.0, data=bytes(width * height * 4)
    )
    executed: list[tuple[int, int]] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(_click(50, 25))
        return _turn(Finish(type="finish", status="success", summary="done"))

    def execute_physical(action: object) -> None:
        if isinstance(action, MouseClick):
            executed.append((action.x, action.y))

    runner = OodaRunner(
        provider=provider,
        execute_physical=execute_physical,
        sensor=lambda: frame,
        vision_enabled=True,
        max_steps=5,
    )
    runner.run(goal="map")
    assert executed == [(100, 50)]


def test_ax_summaries_are_scaled_into_the_screenshot_map_space() -> None:
    """AX coordinates share the map's image space (one source of truth)."""
    width, height = 1024, 600
    frame = ScreenCapture(
        display_id=0, width=width, height=height, scale=1.0, data=bytes(width * height * 4)
    )
    seen: list[tuple[str, ...]] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state.ui_elements)
        if state.step_index == 0:
            return _turn(_click(1, 1))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        sensor=lambda: frame,
        vision_enabled=True,
        ax_probe=lambda: AxProbeResult(summaries=('Button "Reload" at (232,68) 44x24',)),
        max_steps=5,
    )
    runner.run(goal="ground")
    # 1024pt wide -> a 512px map, so one image pixel is 2 screen points and an
    # AX rect in points is HALVED on the way to the model. The gate doubles it
    # back before actuation, so the model points at the element it read about.
    assert seen[0] == ('Button "Reload" at (116,34) 22x12',)


def test_stuck_loop_injects_corrective_hint_after_two() -> None:
    """Two identical clicks with no screen change fold a corrective hint."""
    seen: list[str | None] = []
    executed: list[tuple[int, int]] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state.last_error)
        if state.step_index >= 3:
            return _turn(Finish(type="finish", status="success", summary="ok"))
        return _turn(_click(42, 42))

    def execute_physical(action: object) -> None:
        if isinstance(action, MouseClick):
            executed.append((action.x, action.y))

    runner = OodaRunner(provider=provider, execute_physical=execute_physical, max_steps=10)
    final = runner.run(goal="click")
    assert final.step_index == 4
    # The hint appears after the 3rd identical click (streak hits REPEAT_WARN_AFTER=2
    # after the 3rd click: 1st→0, 2nd→1, 3rd→2=warn).
    hints = [e for e in seen if e is not None and "action repetition detected" in e]
    assert len(hints) == 1
    assert "emit finish" in hints[0]  # type: ignore[operator]
    assert executed == [(42, 42), (42, 42), (42, 42)]


def test_stuck_loop_refuses_the_fourth_identical_click_and_then_ends() -> None:
    """A model that never varies is stopped, and the run still terminates.

    Streak: 1st click→0, 2nd→1, 3rd→2 (warn), 4th would be 3 → refused before
    it touches the physical layer. The refusal is a *recoverable* failure, so
    the model gets escalating "change your approach" guidance rather than an
    immediate kill — and when it changes nothing, the recovery ladder ends the
    run. Both halves matter: no endless clicking, and no giving up on the
    first obstacle.
    """
    executed: list[tuple[int, int]] = []

    def provider(state: WorkingState) -> AgentTurn:
        return _turn(_click(7, 7))

    def execute_physical(action: object) -> None:
        if isinstance(action, MouseClick):
            executed.append((action.x, action.y))

    runner = OodaRunner(provider=provider, execute_physical=execute_physical, max_steps=50)
    with pytest.raises(UnrecoverableFailureError) as excinfo:
        runner.run(goal="click")
    assert excinfo.value.failure.kind is FailureKind.REPETITION
    # The guard fires at decision time: only 3 clicks ever ran, however many
    # turns the model spent insisting.
    assert len(executed) == 3


def test_stuck_guard_ignores_distinct_actions() -> None:
    """Alternating genuinely distinct targets never trip the guard."""
    executed: list[tuple[int, int]] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index >= 6:
            return _turn(Finish(type="finish", status="success", summary="ok"))
        # Far apart: 10 vs 300 is beyond the 32px intent tolerance, so these
        # are distinct targets and must not trip the repetition guard.
        x = 10 if state.step_index % 2 == 0 else 300
        return _turn(_click(x, x))

    def execute_physical(action: object) -> None:
        if isinstance(action, MouseClick):
            executed.append((action.x, action.y))

    runner = OodaRunner(provider=provider, execute_physical=execute_physical, max_steps=10)
    final = runner.run(goal="alternate")
    assert final.step_index == 7
    assert len(executed) == 6


def test_a_confirmed_repeat_is_not_a_stuck_loop() -> None:
    """Verification outranks the progress signature when the two disagree.

    The signature is the cheap change detector — frame, window title, element
    list — and it deliberately leaves out the content digest, which is what the
    witnesses read. So a click that changes only text moves no signature while
    verification, looking at that very text, confirms it. Measured on a real
    run of "research this, then write it into Notes": three confirmed clicks
    into the note body convinced the guard the run was stuck, and every later
    click on that field was refused before it reached the host. The agent could
    never type, and spent the rest of its budget searching the web instead.
    """
    reading = 0

    def ax_probe() -> AxProbeResult:
        nonlocal reading
        reading += 1
        # Same elements every time (signature unmoved); different text every
        # time (the witnesses see the field respond).
        return AxProbeResult(
            summaries=("TextArea 'note body' at (945,551) 600x400",),
            content=(f"note body revision {reading}",),
        )

    clicks = 0

    def provider(_state: WorkingState) -> AgentTurn:
        nonlocal clicks
        if clicks < 5:
            clicks += 1
            return AgentTurn(
                thought="",
                sub_goal="focus the note body",
                action=MouseClick(type="mouse_click", x=945, y=551),
            )
        return AgentTurn(
            thought="",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    final = OodaRunner(
        provider=provider,
        execute_physical=lambda _a: None,
        ax_probe=ax_probe,
        max_steps=12,
    ).run(goal="write the summary into the note")
    assert len(final.completed_steps) == 6, "no click should have been refused"


def test_jittered_repeats_without_progress_trip_the_guard() -> None:
    """A model jittering click coords with no screen change aborts (regression).

    The observed failure: the agent clicked the same wrong spot eight times
    with 2-60px coordinate drift and the exact-payload guard never fired.
    With intent tolerance, the run warns and then aborts instead of clicking
    forever.
    """
    executed: list[tuple[int, int]] = []
    seen_errors: list[str | None] = []
    # Field-observed drift: same top-left region, slightly different pixels.
    clicks = [(145, 114), (145, 114), (173, 115), (148, 116)]

    def provider(state: WorkingState) -> AgentTurn:
        seen_errors.append(state.last_error)
        # A lost model keeps nudging the same spot; it never volunteers a
        # finish, which is exactly why the guard has to end the run.
        return _turn(_click(*clicks[min(state.step_index, len(clicks) - 1)]))

    def execute_physical(action: object) -> None:
        if isinstance(action, MouseClick):
            executed.append((action.x, action.y))

    runner = OodaRunner(provider=provider, execute_physical=execute_physical, max_steps=50)
    with pytest.raises(UnrecoverableFailureError):
        runner.run(goal="open the result")
    # The 3rd and 4th clicks are all one row apart: warn after the 3rd, abort
    # before the 4th — never eight blind clicks.
    assert len(executed) == 3
    assert any(
        e is not None and "action repetition detected" in e for e in seen_errors
    )


def test_mouse_move_repetition_is_not_a_stuck_signal() -> None:
    """Repeated identical moves (cursor positioning) never trip the guard."""
    executed: list[int] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index >= 5:
            return _turn(Finish(type="finish", status="success", summary="ok"))
        return _turn(MouseMove(type="mouse_move", x=100, y=100))

    def execute_physical(action: object) -> None:
        if isinstance(action, MouseMove):
            executed.append(action.x)

    runner = OodaRunner(provider=provider, execute_physical=execute_physical, max_steps=10)
    final = runner.run(goal="position")
    assert final.step_index == 6
    assert len(executed) == 5


def test_max_steps_raises_instead_of_silent_stop() -> None:
    """Exhausting max_steps surfaces a typed error, not a silent return."""

    def provider(state: WorkingState) -> AgentTurn:
        # Alternate coordinates so the stuck-loop guard never fires;
        # this test is about max_steps termination, not repetition.
        x = 1 + state.step_index
        return _turn(_click(x, x))

    def execute_physical(_action: object) -> None:
        return None

    runner = OodaRunner(provider=provider, execute_physical=execute_physical, max_steps=3)
    with pytest.raises(MaxStepsError, match="max_steps=3"):
        runner.run(goal="never")


def test_runner_logs_executed_physical_step(caplog) -> None:
    """Every executed physical action is visible at INFO (live run UX)."""

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(_click(42, 42))
        return _turn(Finish(type="finish", status="success", summary="ok"))

    def execute_physical(_action: object) -> None:
        return None

    with caplog.at_level(logging.INFO, logger="computeruse.orchestrator.loop"):
        runner = OodaRunner(provider=provider, execute_physical=execute_physical, max_steps=5)
        runner.run(goal="demo")
    # The interactive user sees the step + payload streaming on stderr while
    # the run is live — a silent terminal reads as "nothing is happening".
    assert "step_0:mouse_click" in caplog.text
    assert "'x': 42" in caplog.text


def test_repeated_action_with_changing_screen_is_not_stuck() -> None:
    """Same action repeated, but the screen IS changing → not stuck.

    A repeated action against a changing screen is legitimate (e.g. clicking
    through a multi-step wizard where each click advances a page). The streak
    resets when the screen fingerprint differs from the previous step.
    """
    executed: list[tuple[int, int]] = []
    seen_errors: list[str | None] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen_errors.append(state.last_error)
        if state.step_index >= 6:
            return _turn(Finish(type="finish", status="success", summary="ok"))
        return _turn(_click(50, 50))

    def execute_physical(action: object) -> None:
        if isinstance(action, MouseClick):
            executed.append((action.x, action.y))

    # Each step's state gets a different active_window, simulating screen change.
    def window_probe():
        return FocusedWindow(pid=1, app_name="App", window_title=f"page_{len(executed)}")

    runner = OodaRunner(
        provider=provider,
        execute_physical=execute_physical,
        window_probe=window_probe,
        max_steps=10,
    )
    final = runner.run(goal="click through wizard")
    assert final.step_index == 7
    assert len(executed) == 6
    # No stuck-loop hint was ever injected (screen was changing each step).
    hints = [e for e in seen_errors if e is not None and "action repetition" in e]
    assert hints == []


def _batch_turn(action: object, *batch: object) -> AgentTurn:
    """An AgentTurn carrying a batch (``action`` repeats the first element)."""
    return AgentTurn.model_validate(
        {
            "thought": "",
            "sub_goal": "batch",
            "action": action,
            "actions": [action, *batch],
        }
    )


def test_runner_executes_batch_in_one_turn() -> None:
    """OpenAI action-sequence lesson: several actions run per model turn.

    The provider returns ONE decision; the loop executes the whole batch
    before asking again — so three physical actions cost one LLM turn, not
    three. Each action still lands in ``completed_steps`` and the trajectory.
    """
    executed: list[str] = []
    provider_calls = 0

    def provider(state: WorkingState) -> AgentTurn:
        nonlocal provider_calls
        provider_calls += 1
        if state.step_index == 0:
            return _batch_turn(
                MouseClick(type="mouse_click", x=1, y=1),
                MouseClick(type="mouse_click", x=2, y=2),
                Finish(type="finish", status="success", summary="batched"),
            )
        raise AssertionError("single-action turn unexpectedly requested after batch")

    def execute_physical(action: object) -> None:
        executed.append(action.type if isinstance(action, MouseClick) else str(action))

    runner = OodaRunner(provider=provider, execute_physical=execute_physical, max_steps=5)
    final = runner.run(goal="batch")
    assert provider_calls == 1, "a batch must cost exactly one provider turn"
    assert executed == ["mouse_click", "mouse_click"]
    assert final.step_index == 3
    assert len(runner.executed_trajectory) == 2, "finish must not enter the trajectory"


def test_runner_stops_batch_on_first_failure() -> None:
    """A mid-batch failure aborts the batch; the next turn sees the error."""
    executed: list[str] = []
    seen_errors: list[str | None] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen_errors.append(state.last_error)
        if state.step_index == 0:
            return _batch_turn(
                MouseClick(type="mouse_click", x=1, y=1),
                MouseClick(type="mouse_click", x=2, y=2),
                MouseClick(type="mouse_click", x=3, y=3),
            )
        return _turn(Finish(type="finish", status="success", summary="recovered"))

    def execute_physical(action: object) -> None:
        if isinstance(action, MouseClick):
            executed.append(str(action.x))
            if action.x == 2:
                raise RuntimeError("driver gone")

    runner = OodaRunner(provider=provider, execute_physical=execute_physical, max_steps=5)
    final = runner.run(goal="batch fail")
    # First action ran; second failed; the batch's third never ran.
    assert executed == ["1", "2"]
    assert final.step_index == 3  # 2 batch actions advanced the index, +1 finish turn
    assert seen_errors[1] is not None and "driver gone" in seen_errors[1]
    # Only the succeeded action entered the honest trajectory (F2).
    assert [a.x for a in runner.executed_trajectory if isinstance(a, MouseClick)] == [1]


def test_runner_refreshes_observation_between_batch_actions() -> None:
    """Mid-batch actions re-observe, so they never act on the pre-batch frame."""
    observes: list[int] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _batch_turn(
                MouseClick(type="mouse_click", x=1, y=1),
                MouseClick(type="mouse_click", x=2, y=2),
                Finish(type="finish", status="success", summary="done"),
            )
        raise AssertionError("unexpected extra provider turn")

    def execute_physical(action: object) -> None:
        pass

    def ax_probe() -> AxProbeResult:
        # Counting AX probes rather than window reads keeps the fixture
        # honest: the window identity must stay stable, because a title that
        # changed on every read would (correctly) trip the staleness gate.
        observes.append(len(observes))
        return AxProbeResult(summaries=(f'Button "step {len(observes)}" at (1,1) 2x2',))

    runner = OodaRunner(
        provider=provider,
        execute_physical=execute_physical,
        window_probe=lambda: FocusedWindow(
            pid=1, app_name="App", window_title="stable"
        ),
        ax_probe=ax_probe,
        max_steps=5,
    )
    runner.run(goal="batch observe")
    # Turn-start observe + one per action after the first batch element.
    assert len(observes) >= 2

# --- auditor navigation trail --------------------------------------------------


def test_auditor_accepts_opened_search_result_without_visible_serp() -> None:
    """An opened top result is primary evidence; the SERP need not persist.

    Field pathology: the agent searched "latest AI news", opened the top
    result, and finished on the article — and the auditor rejected it for
    showing no search-results evidence beside the article, demanding two
    pages on screen at once. Two fixes pin the correction: the trail keeps
    the results list the article replaced, and the contract names the opened
    target the primary evidence for search-and-open goals.
    """
    window = FocusedWindow(pid=1, app_name="Chrome", window_title="Google Chrome")
    trail = _extend_trail(
        (),
        window,
        ("StaticText=Top stories", "Link=OneRail uses Nvidia AI to cut emissions"),
        6,
    )
    trail = _extend_trail(
        trail,
        window,
        ("StaticText=OneRail uses Nvidia AI to cut emissions", "StaticText=By Jane Doe"),
        6,
    )
    assert len(trail) == 2, f"the results list must survive the article: {trail}"
    assert "Top stories" in trail[0]
    assert "Jane Doe" in trail[1]

    state = WorkingState(
        goal="search for the latest AI news and open the top result",
        active_window="Chrome — OneRail uses Nvidia AI to cut emissions",
        ui_elements=('StaticText "OneRail uses Nvidia AI to cut emissions" at (100,100) 400x24',),
        observed_trail=trail,
    )
    prompt = completion_prompt(state, "opened the top result", app="Google Chrome")
    assert "Do NOT reject simply because the" in prompt
    assert "Top stories" in prompt

    def model(prompt_text: str, _image_b64: object = None) -> str:
        # The verdict below is only meaningful if the auditor decided with
        # both exhibits in front of it — the article on screen and the
        # results list in the trail. A regression that drops either fails
        # here, not in a field run at 2am.
        assert "OneRail uses Nvidia AI" in prompt_text
        assert "Top stories" in prompt_text
        return '{"satisfied": true, "evidence": "article open and visibly on the requested topic"}'

    verdict = completion_auditor(model, app="Google Chrome")(state, "opened the top result")
    assert verdict.satisfied is True
