"""One accessibility reading per step, not two.

VERIFY reads the accessibility tree after an action, and the next OBSERVE read
it again immediately afterwards. On a real web page that second reading is not
cheap: a full walk costs ~400ms, because every node is a handful of
cross-process attribute reads. Measured over a twelve-step run against a live
Chrome, the loop issued 24 ``ax_snapshot`` calls where 13 carried the same
information.

Reuse is only sound because the verification reading is taken *after* the
settle wait, with nothing actuated since. Measured against a live GitHub page
under the production settle budget (8 x 100ms): a carried reading and a fresh
one taken straight after it were identical 16 times out of 16, across both
scrolls and clicks. Under a 180ms wait they differed — which is why the budget,
not the reuse, is what keeps this honest.

These tests pin the invariant that makes it safe: the reading may be reused
once, by the very next observation, and only when this step's own verification
produced it.
"""

from __future__ import annotations

from computeruse.orchestrator.evidence import CompletionVerdict
from computeruse.orchestrator.loop import AxProbeResult, OodaRunner, WorkingState
from computeruse.orchestrator.schemas import (
    AgentTurn,
    CallTool,
    Finish,
    MouseClick,
    Wait,
)


def _always_satisfied(_state: WorkingState, _claim: str) -> CompletionVerdict:
    return CompletionVerdict(satisfied=True, evidence="ok")


def _turn(action: object, sub_goal: str = "step") -> AgentTurn:
    return AgentTurn(thought="t", sub_goal=sub_goal, action=action)  # pyright: ignore[reportArgumentType]


class CountingProbe:
    """An AX probe that reports how often the loop actually called it."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> AxProbeResult:
        self.calls += 1
        return AxProbeResult(
            summaries=(f'Button "Save" at (10,10) 40x20 (reading {self.calls})',),
            content=(f"reading {self.calls}",),
        )


def _click_then_finish(actions: list[object]) -> object:
    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index < len(actions):
            return _turn(actions[state.step_index])
        return _turn(Finish(type="finish", status="success", summary="done"))

    return provider


def test_a_verified_click_spares_the_next_observation_a_second_reading() -> None:
    probe = CountingProbe()
    runner = OodaRunner(
        provider=_click_then_finish(
            [MouseClick(type="mouse_click", x=10, y=10)]
        ),
        execute_physical=lambda _action: None,
        ax_probe=probe,
        completion_check=_always_satisfied,
        max_steps=5,
    )
    runner.run(goal="reuse")
    # Step 0 observes (1), verifies (2). Step 1 reuses (2) rather than making a
    # third call before the finish.
    assert probe.calls == 2, (
        f"expected one observation and one verification, got {probe.calls} readings"
    )


def test_a_step_that_never_verified_still_gets_a_fresh_reading() -> None:
    """A wait changes the screen without any verification to vouch for it."""
    probe = CountingProbe()
    runner = OodaRunner(
        provider=_click_then_finish(
            [Wait(type="wait", duration_ms=0, reason="let it render")]
        ),
        execute_physical=lambda _action: None,
        ax_probe=probe,
        completion_check=_always_satisfied,
        max_steps=5,
    )
    runner.run(goal="wait")
    # Two observations, no verification reading to carry: the wait's own
    # duration is exactly the interval a stale reading would lie about.
    assert probe.calls == 2, (
        f"a wait must not hand a stale reading forward; got {probe.calls} readings"
    )


def test_a_tool_step_does_not_inherit_an_earlier_reading() -> None:
    """A tool runs for an unbounded time with nothing watching the screen."""
    probe = CountingProbe()
    runner = OodaRunner(
        provider=_click_then_finish(
            [
                MouseClick(type="mouse_click", x=10, y=10),
                CallTool(type="call_tool", tool="anything", arguments={}),
            ]
        ),
        execute_physical=lambda _action: None,
        ax_probe=probe,
        completion_check=_always_satisfied,
        max_steps=6,
    )
    runner.run(goal="tool")
    # observe, verify(click), reuse for step 1, then step 2 must observe afresh
    # because the tool step verified nothing.
    assert probe.calls == 3, (
        f"a tool step must re-read rather than inherit; got {probe.calls} readings"
    )
