"""Acting on a display that is not the primary one.

Actuation coordinates are global (origin at the primary display's top-left),
while a screenshot describes exactly one display. Every test here is about the
gap between those two facts: without the captured display's own origin, a
coordinate read off a secondary display's frame lands on the primary display
instead — confidently, and with nothing to catch it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

from computeruse.agent import Agent, AgentConfig
from computeruse.cli import parse_args
from computeruse.orchestrator.loop import (
    AxProbeResult,
    OodaRunner,
    WorkingState,
    map_action_to_screen,
)
from computeruse.orchestrator.schemas import Action, AgentTurn, Finish, MouseClick
from computeruse.security.autonomy import AutonomyLevel
from computeruse.vision.ax import summaries_within
from computeruse.vision.capture import (
    ScreenCapture,
    screen_map_of,
    to_logical_resolution,
    verify_capture_region,
)
from computeruse.vision.coordinates import Point, Rect, ScreenMap, Size
from computeruse.vision.focus import FocusedWindow
from tests.smoke.conftest import SIMULATED_SETTLE, SOCKET_PATH

#: A second display 1024x600 points wide, sitting to the right of a 1512pt one.
SECONDARY_ORIGIN = Point(1512.0, 0.0)


def _frame(*, width: int, height: int, origin: Point, scale: float = 1.0) -> ScreenCapture:
    return ScreenCapture(
        display_id=1,
        width=width,
        height=height,
        scale=scale,
        origin_x=origin.x,
        origin_y=origin.y,
        data=bytes(width * height * 4),
    )


def _turn(action: object) -> AgentTurn:
    return AgentTurn.model_validate({"thought": "t", "sub_goal": "s", "action": action})


def test_a_capture_knows_which_rectangle_of_the_desktop_it_is() -> None:
    frame = _frame(width=2048, height=1200, origin=SECONDARY_ORIGIN, scale=2.0)
    assert frame.logical_size == Size(1024.0, 600.0)
    assert frame.display_frame == Rect(origin=SECONDARY_ORIGIN, size=Size(1024.0, 600.0))


def test_the_screen_map_carries_the_origin_from_the_capture() -> None:
    physical = _frame(width=2048, height=1200, origin=SECONDARY_ORIGIN, scale=2.0)
    logical = to_logical_resolution(physical)
    assert logical.origin == SECONDARY_ORIGIN, "downscaling must not lose the offset"
    screen_map = screen_map_of(logical, logical)
    assert screen_map.origin == SECONDARY_ORIGIN
    assert not screen_map.is_identity, "an offset display is never an identity map"


def test_the_conversion_round_trips_on_a_secondary_display() -> None:
    """to_image and to_screen must cancel exactly, offset included."""
    screen_map = ScreenMap(
        logical=Size(1024.0, 600.0), image=Size(512.0, 300.0), origin=SECONDARY_ORIGIN
    )
    on_screen = Point(1800.0, 240.0)
    assert screen_map.to_screen(screen_map.to_image(on_screen)) == on_screen


def test_a_click_on_the_second_display_never_lands_on_the_first() -> None:
    """The bug this whole feature exists to prevent, stated as an assertion."""
    screen_map = ScreenMap(
        logical=Size(1024.0, 600.0), image=Size(512.0, 300.0), origin=SECONDARY_ORIGIN
    )
    click = map_action_to_screen(MouseClick(type="mouse_click", x=0, y=0), screen_map)
    assert isinstance(click, MouseClick)
    assert click.x == 1512, "the frame's top-left is the desktop's (1512,0)"


def test_the_bounds_gate_names_the_display_being_observed() -> None:
    """A hallucinated coordinate is judged against this display's rectangle.

    Coordinates the model reads off its own screenshot always land somewhere on
    the observed display — that is what the map guarantees. The gate is there
    for the ones it invents, and on a secondary display "0 to width" is the
    wrong rectangle to judge them by.
    """
    executed: list[Action] = []
    frame = _frame(width=512, height=300, origin=SECONDARY_ORIGIN)

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=4000, y=10))
        return _turn(Finish(type="finish", status="failed", summary="gave up"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=executed.append,
        sensor=lambda: frame,
        verify_enabled=False,
        max_steps=4,
    )
    final = runner.run(goal="click on the second screen")
    assert executed == [], "an invented coordinate must never actuate"
    assert final.last_error is not None
    assert "outside the observed display" in final.last_error
    assert "(1512,0)" in final.last_error, "the diagnostic names the real frame"


def test_a_pick_on_the_observed_display_is_shifted_onto_it() -> None:
    """The model points at its own frame; the driver clicks the global point."""
    executed: list[Action] = []
    frame = _frame(width=512, height=300, origin=SECONDARY_ORIGIN)

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(MouseClick(type="mouse_click", x=100, y=50))
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=executed.append,
        sensor=lambda: frame,
        verify_enabled=False,
        max_steps=4,
    )
    runner.run(goal="click on the second screen")
    assert [(a.x, a.y) for a in executed if isinstance(a, MouseClick)] == [(1612, 50)]


def test_elements_on_another_display_are_not_offered_to_the_model() -> None:
    """An app's AX tree spans displays; the frame does not."""
    frame = Rect(origin=SECONDARY_ORIGIN, size=Size(1024.0, 600.0))
    summaries = (
        'Button "On the primary" at (400,300) 80x24',
        'Button "On the secondary" at (1900,300) 80x24',
        "(AX grounding truncated at 64 elements)",
    )
    kept = summaries_within(summaries, frame)
    assert len(kept) == 2
    assert "On the secondary" in kept[0]
    assert kept[1].startswith("(AX grounding truncated")


def test_the_runner_lists_and_marks_only_what_is_on_screen() -> None:
    """Filtering happens before numbering, so [N] and mark N stay the same thing."""
    frame = _frame(width=1024, height=600, origin=SECONDARY_ORIGIN)

    def ax_probe() -> AxProbeResult:
        return AxProbeResult(
            summaries=(
                'Button "Elsewhere" at (200,100) 40x20',
                'Button "Here" at (1600,100) 40x20',
            )
        )

    seen: list[tuple[str, ...]] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state.ui_elements)
        return _turn(Finish(type="finish", status="success", summary="done"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _a: None,
        sensor=lambda: frame,
        ax_probe=ax_probe,
        max_steps=2,
    )
    runner.run(goal="look")
    assert len(seen[0]) == 1 and "Here" in seen[0][0]
    marks = runner._observation.marks
    assert [m.index for m in marks] == [1]
    assert "Here" in marks[0].label


def test_region_verification_localises_a_global_point() -> None:
    """A verified region is named in global points but cropped from one display."""
    before = _frame(width=64, height=48, origin=SECONDARY_ORIGIN)
    changed = bytearray(before.data)
    for index in range(0, 40 * 4, 4):  # repaint the first rows white
        changed[index : index + 4] = b"\xff\xff\xff\xff"
    after = ScreenCapture(
        display_id=before.display_id,
        width=before.width,
        height=before.height,
        scale=before.scale,
        origin_x=before.origin_x,
        origin_y=before.origin_y,
        data=bytes(changed),
    )
    # The repainted pixels are at the display's own top-left, which in global
    # points is (1512,0) — the region has to be localised before it is cropped.
    at_corner = verify_capture_region(
        before, after, Rect(origin=Point(1512.0, 0.0), size=Size(8.0, 4.0))
    )
    assert at_corner.changed
    untouched = verify_capture_region(
        before, after, Rect(origin=Point(1560.0, 30.0), size=Size(8.0, 4.0))
    )
    assert not untouched.changed


def test_an_older_driver_without_an_origin_still_reads_as_the_primary_display() -> None:
    """Wire back-compat: no origin field means a single display at 0,0."""
    response: dict[str, object] = {
        "ok": "screenshot",
        "display_id": 0,
        "format": "bgra8",
        "width": 2,
        "height": 1,
        "scale": 1.0,
        "data_base64": "AAAAAAAAAAA=",
    }
    capture = ScreenCapture.from_response(response)
    assert capture.origin == Point(0.0, 0.0)


def test_the_cli_exposes_the_display_selector() -> None:
    assert parse_args(["--goal", "x"]).display == 0
    assert parse_args(["--goal", "x", "--display", "2"]).display == 2


def test_the_agent_captures_the_configured_display(tmp_path: Path) -> None:
    """End to end: --display reaches the driver's screenshot RPC."""
    requested: list[int] = []
    frame = _frame(width=64, height=48, origin=Point(0.0, 0.0))

    class RecordingClient:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def capture(self, display_id: int = 0) -> ScreenCapture:
            requested.append(display_id)
            return frame

        def focused_window(self) -> FocusedWindow:
            return FocusedWindow(
                pid=1,
                app_name="Safari",
                bundle_id="com.apple.Safari",
                window_title="t",
                cursor_x=0.0,
                cursor_y=0.0,
            )

        def hotkey_state(self) -> bool:
            return False

        def send(self, action: Action) -> None:
            return None

        def ax_snapshot(self, pid: int, max_depth: int, max_nodes: int) -> object:
            raise RuntimeError("no accessibility tree in this test")

        def activate_app(self, app: str) -> None:
            return None

    import computeruse.agent as agent_module

    original = agent_module.ActuationClient
    agent_module.ActuationClient = lambda *_a, **_k: RecordingClient()  # type: ignore[assignment]
    try:
        config = AgentConfig(
            goal="look at the second screen",
            app="Safari",
            provider=lambda _s: _turn(
                Finish(type="finish", status="success", summary="done")
            ),
            socket_path=str(SOCKET_PATH),
            store_dir=tmp_path / "store",
            autonomy_level=AutonomyLevel.FULL,
            enable_visual_verification=False,
            enable_vision=True,
            display_id=2,
            max_steps=2,
            **SIMULATED_SETTLE,
        )
        Agent(config).run()
    finally:
        agent_module.ActuationClient = original  # type: ignore[assignment]
    assert requested, "the sensor must have been used"
    assert set(requested) == {2}, "every capture must target the configured display"
