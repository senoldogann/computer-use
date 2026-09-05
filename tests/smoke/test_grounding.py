"""ADR-2 grounding into the OODA loop tests.

AX is the *primary* localization source: the driver's ``ax_snapshot`` RPC
serves a deterministic Safari fixture, so the chain — pure summaries of the
actionable elements, working-state threading (every decision sees them), the
prompt rendering, and an end-to-end agent run whose provider picks real
coordinates off the summaries — is exercised through the real compiled driver.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from computeruse.agent import Agent, AgentConfig
from computeruse.orchestrator.client import ActuationClient
from computeruse.orchestrator.loop import AxProbeResult, OodaRunner, WorkingState
from computeruse.orchestrator.prompts import decision_prompt
from computeruse.orchestrator.schemas import AgentTurn, Finish, MouseClick
from computeruse.security.autonomy import AutonomyLevel
from computeruse.vision import (
    AXElement,
    element_summary,
    find_elements,
    interactive_summaries,
    summaries_to_image_space,
)
from computeruse.vision.ax import open_tabs_from_tree
from tests.smoke.conftest import SIMULATED_SETTLE, SOCKET_PATH

APP_PID = 4242

# The fixture's actionable elements (DFS order): three toolbar buttons and the
# address field. The Toolbar container itself is not actionable and must not
# appear.
FIXTURE_SUMMARIES = (
    'Button "Back" at (142,80) 44x24',
    'Button "Forward" at (198,80) 44x24',
    'Button "Reload" at (254,80) 44x24',
    # The fixture's address field holds focus and its AXValue mirrors the
    # address text: the consent-free "click landed" + "text landed" signals
    # the provider consumes (ADR-2 state source).
    'TextField "https://example.com" at (520,80) 400x24 value="https://example.com" (focused)',
)


def _fixture_root() -> AXElement:
    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        return client.ax_snapshot(pid=APP_PID)


def test_interactive_summaries_from_live_driver() -> None:
    """The driver's AX tree renders into exactly the actionable elements."""
    summaries = interactive_summaries(_fixture_root())
    assert summaries == FIXTURE_SUMMARIES


def test_element_summary_marks_focused_state() -> None:
    """Focused elements carry the consent-free click-landed signal."""
    focused = AXElement(
        role="TextField",
        title="https://example.com",
        focused=True,
        x=320.0,
        y=68.0,
        width=400.0,
        height=24.0,
    )
    line = element_summary(focused)
    assert line == 'TextField "https://example.com" at (520,80) 400x24 (focused)'
    # Unfocused elements keep the exact old format (parseable, no marker).
    assert not element_summary(focused.model_copy(update={"focused": False})).endswith(
        "(focused)"
    )


def test_element_summary_is_parseable_and_handles_missing_title() -> None:
    reload = find_elements(_fixture_root(), role="Button", title="Reload")[0]
    line = element_summary(reload)
    assert line == 'Button "Reload" at (254,80) 44x24'
    # Coordinates and size are recoverable from the line (grounding contract).
    match = re.search(r"at \((\d+),(\d+)\) (\d+)x(\d+)", line)
    assert match is not None
    # The point is the element's CENTRE, not its origin: the model clicks what
    # it is told, and a corner sits on the boundary where rounding lands outside.
    assert tuple(int(g) for g in match.groups()) == (254, 80, 44, 24)
    assert (254, 80) == (232 + 44 // 2, 68 + 24 // 2)
    # An element without a title is still actionable, just labelled generically.
    untitled = AXElement(role="Button", x=1.0, y=2.0, width=10.0, height=5.0)
    assert element_summary(untitled) == 'Button "(untitled)" at (6,4) 10x5'


def test_interactive_summaries_respect_depth_cap() -> None:
    root = _fixture_root()
    assert interactive_summaries(root, max_depth=0) == ()
    # Depth 1 exposes only the window; the toolbar buttons live at depth 2.
    assert interactive_summaries(root, max_depth=1) == ()
    assert len(interactive_summaries(root, max_depth=2)) == 4


def test_interactive_summaries_bounded_by_count_not_only_depth() -> None:
    """A deep interactive tree cannot balloon the working context (Law 4.3)."""

    def tree(depth: int) -> AXElement:
        # One interactive button per level, nested arbitrarily deep.
        return AXElement(
            role="Button",
            title=f"btn-{depth}",
            x=float(depth),
            y=0.0,
            width=10.0,
            height=10.0,
            children=(tree(depth - 1),) if depth > 1 else (),
        )

    deep = tree(depth=40)
    # The depth cap alone would allow 40 lines; the count cap stops at 24.
    summaries = interactive_summaries(deep, max_depth=40)
    assert len(summaries) == 24
    assert summaries[0].startswith('Button "btn-40"')


def test_interactive_summaries_put_web_content_before_browser_chrome() -> None:
    """The count budget is not starved by browser chrome (field regression).

    Chrome's AX tree lists its toolbar/tab chrome before the page's WebArea;
    a plain DFS with a count cap filled the budget with chrome buttons and
    the Google result links never reached the provider — the agent then
    guessed coordinates. Web content must get the budget first.
    """
    chrome = tuple(
        AXElement(
            role="Button", title=f"toolbar-{i}", x=100.0, y=50.0, width=40.0, height=24.0
        )
        for i in range(30)
    )
    links = tuple(
        AXElement(
            role="Link",
            title=f"result-{i}",
            x=150.0,
            y=200.0 + i * 40,
            width=400.0,
            height=30.0,
        )
        for i in range(10)
    )
    web_area = AXElement(
        role="WebArea",
        title="page",
        x=0.0,
        y=0.0,
        width=1200.0,
        height=800.0,
        children=links,
    )
    root = AXElement(role="Window", title="Chrome", children=(*chrome, web_area))
    summaries = interactive_summaries(root, max_depth=12, max_count=12)
    # All 10 page links come before any chrome element, and the budget caps
    # the rest of the chrome list.
    assert len(summaries) == 12
    assert summaries[0].startswith('Link "result-0"')
    assert all(line.startswith('Link "result-') for line in summaries[:10])
    assert any(line.startswith('Button "toolbar-') for line in summaries[10:])


def test_interactive_summaries_without_web_area_keep_dfs_order() -> None:
    """Non-browser apps (no WebArea) keep the plain DFS order."""
    root = _fixture_root()
    assert interactive_summaries(root) == FIXTURE_SUMMARIES


def test_summaries_to_image_space_divides_by_points_per_pixel() -> None:
    """AX rects are logical points; the model's space is the smaller image map.

    Direction matters more than magnitude here: multiplying instead of
    dividing pushed every AX coordinate off the right edge of the model's own
    screenshot, and the actuation gate then scaled it a second time into a
    point the bounds check rejected. AX grounding was silently dead.
    """
    from computeruse.vision import Point, ScreenMap, Size

    halving = ScreenMap(logical=Size(1024.0, 600.0), image=Size(512.0, 300.0))
    scaled = summaries_to_image_space(
        (
            'Button "Reload" at (254,80) 44x24 value="x" (focused)',
            "(AX grounding truncated — rely on the screenshot map for coordinates)",
        ),
        halving,
    )
    assert scaled[0] == 'Button "Reload" at (127,40) 22x12 value="x" (focused)'
    # Lines without a coordinate fragment pass through untouched.
    assert scaled[1] == "(AX grounding truncated — rely on the screenshot map for coordinates)"
    # An identity map (no scaling, no display offset) is a passthrough.
    identity = ScreenMap(logical=Size(512.0, 300.0), image=Size(512.0, 300.0))
    assert summaries_to_image_space(('Button "OK" at (2,4) 3x4',), identity) == (
        'Button "OK" at (2,4) 3x4',
    )
    # A secondary display: the element is placed relative to the frame the
    # model is looking at, not to the desktop's corner.
    offset = ScreenMap(
        logical=Size(1024.0, 600.0),
        image=Size(512.0, 300.0),
        origin=Point(1024.0, 0.0),
    )
    assert summaries_to_image_space(('Button "Far" at (1278,80) 44x24',), offset) == (
        'Button "Far" at (127,40) 22x12',
    )


def test_summaries_round_trip_through_the_actuation_gate() -> None:
    """An AX coordinate handed to the model comes back as the same screen point.

    This is the invariant the two directions exist to hold: whatever the model
    reads off an AX line, the coordinate gate must convert back to where the
    element actually is. Off-by-a-factor bugs die here rather than on a host.
    """
    from computeruse.orchestrator.loop import map_action_to_screen
    from computeruse.orchestrator.schemas import MouseClick
    from computeruse.vision import Point, ScreenMap, Size

    for origin in (Point(0.0, 0.0), Point(1512.0, 0.0)):
        # The round trip must hold on a secondary display too: both directions
        # carry the same origin, so an offset cancels out exactly.
        screen_map = ScreenMap(
            logical=Size(1512.0, 982.0), image=Size(512.0, 333.0), origin=origin
        )
        element_point = Point(600.0 + origin.x, 400.0 + origin.y)
        line = f'Button "Go" at ({element_point.x:.0f},{element_point.y:.0f}) 44x24'
        (in_image,) = summaries_to_image_space((line,), screen_map)
        model_x, model_y = (
            int(v) for v in in_image.split("at (")[1].split(")")[0].split(",")
        )
        actuated = map_action_to_screen(
            MouseClick(type="mouse_click", x=model_x, y=model_y), screen_map
        )
        assert isinstance(actuated, MouseClick)
        # One image pixel is ~3 screen points, so a round trip lands within that.
        assert abs(actuated.x - element_point.x) <= screen_map.points_per_pixel
        assert abs(actuated.y - element_point.y) <= screen_map.points_per_pixel


def test_summaries_include_deep_focused_element() -> None:
    """A focused element five levels deep is still surfaced (Chrome omnibox)."""
    window = AXElement(
        role="Window",
        title="Chrome",
        x=0.0,
        y=0.0,
        width=1200.0,
        height=800.0,
        children=(
            AXElement(role="Toolbar", title="", x=0.0, y=0.0, width=1200.0, height=40.0),
            AXElement(
                role="TabGroup",
                title="",
                x=0.0,
                y=40.0,
                width=1200.0,
                height=50.0,
                children=(
                    AXElement(
                        role="Group",
                        title="",
                        x=0.0,
                        y=40.0,
                        width=1200.0,
                        height=50.0,
                        children=(
                            AXElement(
                                role="Group",
                                title="",
                                x=0.0,
                                y=40.0,
                                width=1200.0,
                                height=50.0,
                                children=(
                                    AXElement(
                                        role="TextField",
                                        title="",
                                        focused=True,
                                        x=158.0,
                                        y=90.0,
                                        width=1164.0,
                                        height=24.0,
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    summaries = interactive_summaries(window)
    assert any("TextField \"(untitled)\" at (740,102) 1164x24 (focused)" in line for line in summaries)


def _provider() -> Callable[[WorkingState], AgentTurn]:
    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click the first thing",
                action=MouseClick(type="mouse_click", x=10, y=10),
            )
        if state.step_index == 1:
            return AgentTurn(
                thought="second",
                sub_goal="click the second thing",
                action=MouseClick(type="mouse_click", x=20, y=20),
            )
        return AgentTurn(
            thought="done",
            sub_goal="workflow complete",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    return provider


def test_loop_refreshes_elements_before_every_decision() -> None:
    """OBSERVE folds the element summaries into the state the provider sees."""
    seen: list[tuple[str, ...]] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state.ui_elements)
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click",
                action=MouseClick(type="mouse_click", x=1, y=1),
            )
        return AgentTurn(
            thought="done",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        ax_probe=lambda: AxProbeResult(summaries=FIXTURE_SUMMARIES),
    )
    state = runner.run("ground me")
    # Both turns see the elements; the pure reduction preserves them.
    assert seen == [FIXTURE_SUMMARIES, FIXTURE_SUMMARIES]
    assert state.ui_elements == FIXTURE_SUMMARIES


def test_element_probe_failure_degrades_without_aborting() -> None:
    """A grounding gap must not kill the workflow (best-effort, Law 6.3)."""

    def failing_probe() -> tuple[str, ...]:
        raise RuntimeError("accessibility tree unavailable")

    seen: list[tuple[str, ...]] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state.ui_elements)
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click",
                action=MouseClick(type="mouse_click", x=1, y=1),
            )
        return AgentTurn(
            thought="done",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _action: None,
        ax_probe=failing_probe,
    )
    state = runner.run("g")
    assert state.last_error is None, "a probe failure must not surface as a step error"
    assert seen == [(), ()]


def test_decision_prompt_pure_vision() -> None:
    state = WorkingState(goal="g", screenshot_b64="fake_base64_png")
    prompt = decision_prompt(state, app="Safari")
    assert "PRIMARY PERCEPTION (VISION-FIRST):" in prompt
    assert "UI elements on screen" not in prompt


def test_agent_grounds_provider_coordinates_from_ax(tmp_path) -> None:
    """Capstone: the provider reads real AX coordinates off the summaries.

    ADR-2 end to end — AX *generates* (fixture tree -> summaries -> provider),
    and the agent run distills a skill from the grounded trajectory.
    """

    def grounded_provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            # A weak model following the contract: find the Reload button line
                # and click the point it reports. No arithmetic: the summary
                # already gives the centre, the point that survives rounding.
                # Making the model offset from a corner is what put real
                # clicks one point outside their target on a live page.
            reload = next(
                line for line in state.ui_elements if 'Button "Reload"' in line
            )
            match = re.search(r"at \((\d+),(\d+)\) (\d+)x(\d+)", reload)
            assert match is not None
            x, y, _width, _height = (int(g) for g in match.groups())
            return AgentTurn(
                thought="reload the page",
                sub_goal="click the Reload button",
                action=MouseClick(
                    type="mouse_click", x=x, y=y
                ),
            )
        if state.step_index == 1:
            return AgentTurn(
                thought="address bar",
                sub_goal="click the address field",
                action=MouseClick(type="mouse_click", x=320, y=68),
            )
        return AgentTurn(
            thought="done",
            sub_goal="workflow complete",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    config = AgentConfig(
        goal="reload and focus address",
        app=None,
        provider=grounded_provider,
        socket_path=str(SOCKET_PATH),
        store_dir=tmp_path / "store",
        autonomy_level=AutonomyLevel.GUARDED,
        enable_visual_verification=False,  # simulated driver never renders
        max_steps=10,
        **SIMULATED_SETTLE,
    )
    result = Agent(config).run()
    # Discovery + grounding: the provider saw the Safari fixture and clicked
    # the point the summary reported — already the Reload button's centre
    # (232+22, 68+12) — then the address bar.
    assert result.app == "Safari"
    assert [a.type for a in result.trajectory] == ["mouse_click", "mouse_click"]
    first = result.trajectory[0]
    # The grounded click landed on the Reload button's centre (232+22, 68+12),
    # taken straight from the summary with no arithmetic in the model.
    assert (first.x, first.y) == (254, 80)
    assert result.distilled is not None and result.distilled.kind == "skill"


def test_open_tabs_from_tree_extracts_tab_titles() -> None:
    """Browser tab titles are extracted from the AX tree (pure)."""
    tab_bar = AXElement(
        role="TabGroup",
        children=(
            AXElement(role="Tab", title="GitHub - senoldogann/computer-use", x=0, y=0, width=200, height=30),
            AXElement(role="Tab", title="Logout", x=200, y=0, width=200, height=30),
            AXElement(role="Tab", title="Logout", x=400, y=0, width=200, height=30),
        ),
    )
    toolbar = AXElement(role="Toolbar", children=(tab_bar,))
    root = AXElement(role="Window", children=(toolbar,))
    tabs = open_tabs_from_tree(root)
    assert tabs == (
        "GitHub - senoldogann/computer-use",
        "Logout",
        "Logout",
    )
    # Empty tree returns empty tuple.
    assert open_tabs_from_tree(AXElement(role="Window")) == ()
    # Non-tab elements are ignored.
    assert open_tabs_from_tree(
        AXElement(role="Window", children=(AXElement(role="Button", title="Click"),))
    ) == ()


def test_ax_probe_result_carries_open_tabs() -> None:
    """AxProbeResult delivers tab titles alongside element summaries."""
    from computeruse.orchestrator.loop import AxProbeResult

    result = AxProbeResult(
        summaries=("Button \"OK\" at (120,210) 40x20",),
        open_tabs=("GitHub", "Tab2"),
    )
    assert result.summaries == ("Button \"OK\" at (120,210) 40x20",)
    assert result.open_tabs == ("GitHub", "Tab2")
    # Default has empty tabs.
    default = AxProbeResult()
    assert default.summaries == ()
    assert default.open_tabs == ()


def test_the_bounds_gate_runs_before_the_quiet_path_actuates() -> None:
    """"Rejected before any physical effect" has to hold on the quiet path too.

    ``_pressed_quietly``/``_typed_quietly`` actuate as they answer, so a bounds
    check placed after them honoured the promise only in foreground mode.
    Measured before the fix: the press landed at (200, 20) on a 100x100 display
    and *then* raised. The effect list must stay empty.
    """
    from computeruse.orchestrator.loop import OodaRunner
    from computeruse.orchestrator.schemas import AgentTurn, Finish, MouseClick
    from computeruse.vision.capture import ScreenCapture
    from computeruse.vision.coordinates import CoordinateOutOfBoundsError, Point

    effects: list[tuple[float, float]] = []
    frame = ScreenCapture(
        display_id=1, width=100, height=100, scale=1.0, data=bytes((0, 0, 0, 255)) * 10000
    )

    def quiet_press(point: Point) -> bool:
        effects.append((point.x, point.y))
        return True

    runner = OodaRunner(
        provider=lambda state: AgentTurn(
            thought="t",
            sub_goal="s",
            action=Finish(type="finish", status="success", summary="s"),
        ),
        execute_physical=lambda _action: None,
        sensor=lambda: frame,
        quiet_press=quiet_press,
    )

    with pytest.raises(CoordinateOutOfBoundsError):
        runner._act_and_verify(MouseClick(type="mouse_click", x=200, y=20))

    assert effects == [], "the host must not be touched before the gate rejects"
