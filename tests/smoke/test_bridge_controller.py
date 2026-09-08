from __future__ import annotations

from collections.abc import Callable

import pytest
from computeruse.bridge.controller import BridgeController, BridgeHostError

from computeruse.orchestrator.schemas import (
    MouseClick,
    MouseMove,
    PressHotkey,
    TypeText,
)
from computeruse.vision.ax import AXElement
from computeruse.vision.capture import ScreenCapture
from computeruse.vision.focus import FocusedWindow


class FakeDriver:
    def __init__(self, root: AXElement | None = None) -> None:
        self.frontmost = "TextEdit"
        self.bundle_id = "com.apple.TextEdit"
        self.sent: list[object] = []
        self.releases = 0
        self.tripped = False
        self.fail_send = False
        self.root = root or AXElement(
            role="Application",
            children=(
                AXElement(role="Button", title="Save", x=10, y=20, width=80, height=24),
                AXElement(
                    role="TextField",
                    title="Notes",
                    value="secret-looking-document-text",
                    focused=True,
                    x=10,
                    y=60,
                    width=240,
                    height=28,
                ),
            ),
        )

    def health(self) -> dict[str, object]:
        return {"ok": "health", "backend": "simulated", "trusted": True, "kill_listener_armed": None}

    def focused_window(self) -> FocusedWindow:
        return FocusedWindow(
            pid=101,
            app_name=self.frontmost,
            bundle_id=self.bundle_id if self.frontmost == "TextEdit" else "",
            window_title="Fixture",
            cursor_x=5,
            cursor_y=7,
        )

    def list_apps(self) -> tuple[str, ...]:
        return ("TextEdit", "Safari")

    def activate_app(self, app: str) -> None:
        self.frontmost = app
        self.bundle_id = "com.apple.TextEdit" if app == "TextEdit" else "com.apple.Safari"

    def app_pid(self, app: str) -> int | None:
        return 101 if app in {"TextEdit", "Safari"} else None

    def ax_snapshot(self, pid: int, max_depth: int = 20, max_nodes: int = 4096) -> AXElement:
        del pid, max_depth, max_nodes
        return self.root

    def capture(self, display_id: int = 0, window_pid: int | None = None) -> ScreenCapture:
        del display_id, window_pid
        return ScreenCapture(
            display_id=0,
            width=2,
            height=2,
            scale=1.0,
            origin_x=0.0,
            origin_y=0.0,
            data=bytes([32, 64, 96, 255]) * 4,
        )

    def hotkey_state(self) -> bool:
        return self.tripped

    def release_inputs(self) -> None:
        self.releases += 1

    def send(self, action: object) -> None:
        if self.fail_send:
            raise RuntimeError("simulated actuation failure")
        self.sent.append(action)


@pytest.fixture
def driver() -> FakeDriver:
    return FakeDriver()


def test_health_and_active_window_never_expose_pid(driver: FakeDriver) -> None:
    controller = BridgeController(driver)

    health = controller.dispatch("health", {})
    window = controller.dispatch("active_window", {})

    assert health == {"backend": "simulated", "trusted": True, "kill_listener_armed": None}
    assert isinstance(window, dict)
    assert window["app_name"] == "TextEdit"
    assert "pid" not in window


def test_ui_snapshot_is_bounded_indexed_and_redacts_values(driver: FakeDriver) -> None:
    controller = BridgeController(driver)
    snapshot = controller.dispatch("ui_snapshot", {"app": "TextEdit", "max_elements": 32})

    assert isinstance(snapshot, dict)
    elements = snapshot["elements"]
    assert isinstance(elements, list)
    assert [item["index"] for item in elements] == [1, 2]
    assert elements[0]["title"] == "Save"
    assert elements[1]["role"] == "TextField"
    assert elements[1]["value"] == ""
    assert "secret-looking-document-text" not in repr(snapshot)


def test_type_text_refuses_when_any_secure_field_is_present() -> None:
    driver = FakeDriver(
        AXElement(
            role="Application",
            children=(AXElement(role="SecureTextField", title="Password", focused=True),),
        )
    )
    controller = BridgeController(driver)

    with pytest.raises(BridgeHostError) as exc:
        controller.dispatch("type_text", {"app": "TextEdit", "text": "not-a-password"})

    assert exc.value.code == "CREDENTIAL_ENTRY_REFUSED"
    assert driver.sent == []


def test_hotkey_takeover_blocks_mutation_before_action(driver: FakeDriver) -> None:
    driver.tripped = True
    controller = BridgeController(driver)

    with pytest.raises(BridgeHostError) as exc:
        controller.dispatch("click", {"app": "TextEdit", "query": "Save"})

    assert exc.value.code == "KILL_SWITCH_TRIPPED"
    assert driver.sent == []


def test_click_resolves_unique_semantic_target_and_moves_then_clicks(driver: FakeDriver) -> None:
    controller = BridgeController(driver)

    controller.dispatch("click", {"app": "TextEdit", "query": "Save"})

    assert len(driver.sent) == 2
    assert isinstance(driver.sent[0], MouseMove)
    assert isinstance(driver.sent[1], MouseClick)
    assert (driver.sent[1].x, driver.sent[1].y) == (50, 32)


def test_focus_sensitive_typing_activates_and_confirms_target(driver: FakeDriver) -> None:
    driver.frontmost = "Safari"
    driver.bundle_id = "com.apple.Safari"
    controller = BridgeController(driver)

    controller.dispatch("type_text", {"app": "TextEdit", "text": "hello", "wpm": 120})

    assert driver.frontmost == "TextEdit"
    assert len(driver.sent) == 1
    assert isinstance(driver.sent[0], TypeText)


def test_mutation_failure_releases_inputs(driver: FakeDriver) -> None:
    driver.fail_send = True
    controller = BridgeController(driver)

    with pytest.raises(RuntimeError, match="simulated actuation failure"):
        controller.dispatch("press_hotkey", {"app": "TextEdit", "modifiers": ["command"], "key": "a"})

    assert driver.releases == 1


def test_open_url_rejects_non_http_schemes_and_uses_injected_opener(driver: FakeDriver) -> None:
    opened: list[tuple[str, str | None]] = []
    opener: Callable[[str, str | None], None] = lambda url, app: opened.append((url, app))
    controller = BridgeController(driver, url_opener=opener)

    with pytest.raises(BridgeHostError) as exc:
        controller.dispatch("open_url", {"url": "javascript:alert(1)"})
    assert exc.value.code == "POLICY_DENIED"

    controller.dispatch("open_url", {"url": "https://example.com/path?q=1#frag", "app": "Safari"})
    assert opened == [("https://example.com/path?q=1#frag", "Safari")]
    assert driver.frontmost == "Safari"


def test_screenshot_returns_real_png_payload_not_raw_frame(driver: FakeDriver) -> None:
    controller = BridgeController(driver)
    result = controller.dispatch("screenshot", {})

    assert isinstance(result, dict)
    assert result["mime_type"] == "image/png"
    assert result["width"] == 2
    assert result["height"] == 2
    assert isinstance(result["data_base64"], str)
    assert result["data_base64"].startswith("iVBOR")


def test_press_hotkey_uses_typed_action(driver: FakeDriver) -> None:
    controller = BridgeController(driver)
    controller.dispatch(
        "press_hotkey",
        {"app": "TextEdit", "modifiers": ["command", "shift"], "key": "p"},
    )

    assert isinstance(driver.sent[-1], PressHotkey)
