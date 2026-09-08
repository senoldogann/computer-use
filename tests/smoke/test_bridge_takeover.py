from __future__ import annotations

import pytest

from computeruse.bridge.controller import BridgeController, BridgeHostError
from computeruse.vision.ax import AXElement
from computeruse.vision.focus import FocusedWindow


class TakeoverAfterFocusDriver:
    def __init__(self) -> None:
        self.sent: list[object] = []
        self.releases = 0
        self.tripped = False
        self.root = AXElement(
            role="Application",
            children=(AXElement(role="TextField", title="Notes", focused=True),),
        )

    def hotkey_state(self) -> bool:
        return self.tripped

    def focused_window(self) -> FocusedWindow:
        focused = FocusedWindow(
            pid=101,
            app_name="TextEdit",
            bundle_id="com.apple.TextEdit",
            window_title="Fixture",
            cursor_x=5,
            cursor_y=7,
        )
        self.tripped = True
        return focused

    def app_pid(self, app: str) -> int | None:
        return 101 if app == "TextEdit" else None

    def ax_snapshot(self, pid: int, max_depth: int = 20, max_nodes: int = 4096) -> AXElement:
        del pid, max_depth, max_nodes
        return self.root

    def send(self, action: object) -> None:
        self.sent.append(action)

    def release_inputs(self) -> None:
        self.releases += 1


@pytest.mark.parametrize(
    ("method", "params"),
    [
        (
            "drag",
            {
                "app": "TextEdit",
                "start": {"x": 1, "y": 1},
                "end": {"x": 2, "y": 2},
            },
        ),
        ("scroll", {"app": "TextEdit", "direction": "down", "pages": 1}),
        ("type_text", {"app": "TextEdit", "text": "hello"}),
        (
            "press_hotkey",
            {"app": "TextEdit", "modifiers": ["command"], "key": "a"},
        ),
    ],
)
def test_takeover_after_focus_blocks_every_physical_send(
    method: str,
    params: dict[str, object],
) -> None:
    driver = TakeoverAfterFocusDriver()
    controller = BridgeController(driver)

    with pytest.raises(BridgeHostError) as exc:
        controller.dispatch(method, params)

    assert exc.value.code == "KILL_SWITCH_TRIPPED"
    assert driver.sent == []
    assert driver.releases == 1
