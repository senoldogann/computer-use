"""ADR-2 accessibility grounding tests.

The simulated driver serves a deterministic Safari window fixture, so the
whole grounding pipeline — RPC wire shape, typed tree, pure element search,
and the AX → display-px → screenshot-pixel coordinate chain — is exercised
end to end through the real compiled driver.
"""

from __future__ import annotations

from computeruse.orchestrator.client import ActuationClient
from computeruse.vision import AXElement, element_rect, find_elements
from computeruse.vision.coordinates import (
    DisplayGeometry,
    Point,
    Rect,
    Size,
    point_to_screenshot_offset,
)
from tests.smoke.conftest import SOCKET_PATH, rpc_call

APP_PID = 4242


def _fixture_root() -> AXElement:
    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        return client.ax_snapshot(pid=APP_PID)


def test_ax_snapshot_wire_shape() -> None:
    payload = rpc_call({"method": "ax_snapshot", "params": {"pid": APP_PID, "max_depth": 8}})
    assert payload.get("ok") == "ax_snapshot"
    root = payload.get("root")
    assert isinstance(root, dict)
    assert root.get("role") == "Application"
    assert root.get("title") == "Safari"


def test_typed_ax_snapshot_via_client() -> None:
    root = _fixture_root()
    assert root.role == "Application"
    assert len(root.children) == 1
    window = root.children[0]
    assert window.role == "Window"
    assert window.width == 800.0


def test_focused_text_value_reads_ax_value() -> None:
    """The focused text field's AXValue feeds paste/type verification."""
    from computeruse.vision import focused_text_value

    root = _fixture_root()
    assert focused_text_value(root) == "https://example.com"


def test_focused_text_value_ignores_unfocused_or_empty() -> None:
    """No focused text-like element with a value -> None (skip verification)."""
    from computeruse.vision import AXElement, focused_text_value

    root = AXElement(role="Application", title="X")
    assert focused_text_value(root) is None
    # Focused button: not a text entry, so still None.
    root = AXElement(
        role="Application",
        title="X",
        children=(
            AXElement(role="Button", title="Reload", focused=True, value=""),
        ),
    )
    assert focused_text_value(root) is None
    # Focused text field with empty value: not determinable -> None.
    root = AXElement(
        role="Application",
        title="X",
        children=(
            AXElement(role="TextField", title="", focused=True, value=""),
        ),
    )
    assert focused_text_value(root) is None


def test_max_depth_caps_traversal() -> None:
    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        shallow = client.ax_snapshot(pid=APP_PID, max_depth=1)
    assert shallow.children[0].role == "Window"
    assert shallow.children[0].children == (), "depth 1 must not expose descendants"


def test_find_elements_by_role_and_title() -> None:
    root = _fixture_root()
    buttons = find_elements(root, role="Button")
    assert [b.title for b in buttons] == ["Back", "Forward", "Reload"]
    reload = find_elements(root, role="Button", title="reload")
    assert len(reload) == 1
    assert reload[0].title == "Reload"
    # No match returns nothing; an unmatched role too.
    assert find_elements(root, title="nonexistent") == ()
    assert find_elements(root, role="Checkbox") == ()


def test_element_rect_bridges_to_coordinate_layer() -> None:
    root = _fixture_root()
    reload = find_elements(root, role="Button", title="Reload")[0]
    rect = element_rect(reload)
    assert rect == Rect(Point(232.0, 68.0), Size(44.0, 24.0))


def test_ax_coordinates_map_to_screenshot_pixels() -> None:
    """ADR-2 end to end: AX generates a location, coordinates map it to pixels.

    The Reload button lives at (232, 68) in the global logical space; on a
    display whose frame starts at (100, 60) with scale 1.0, its center lands
    at pixel (154, 20) of that display's capture.
    """
    root = _fixture_root()
    reload = find_elements(root, role="Button", title="Reload")[0]
    center = Point(reload.x + reload.width / 2, reload.y + reload.height / 2)
    geometry = DisplayGeometry(
        display_id=1,
        frame=Rect(Point(100, 60), Size(800, 600)),
        scale=1.0,
    )
    px = point_to_screenshot_offset(center, geometry, Size(800, 600))
    assert (px.x, px.y) == (154.0, 20.0)
