"""Deterministic host controller for authority-scoped computer use.

No model/provider loop lives here. The controller translates a small bridge
vocabulary into the existing typed actuation and perception primitives while
preserving the physical safety floor: fresh focus checks, credential refusal,
emergency takeover polling, target validation, and input cleanup.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any, cast
from urllib.parse import urlsplit

from computeruse.orchestrator.evidence import Evidence, app_evidence
from computeruse.orchestrator.schemas import (
    MouseClick,
    MouseDrag,
    MouseMove,
    MouseScroll,
    PressHotkey,
    TypeText,
)
from computeruse.vision.ax import INTERACTIVE_ROLES, AXElement, asks_for_a_credential
from computeruse.vision.capture import ScreenCapture, capture_to_base64_png
from computeruse.vision.focus import FocusedWindow

FOCUS_SETTLE_POLLS = 10
FOCUS_SETTLE_INTERVAL_SECONDS = 0.05
DEFAULT_MAX_ELEMENTS = 128
MAX_ELEMENTS = 256


class BridgeHostError(RuntimeError):
    """Stable categorical refusal/failure at the bridge host boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _default_url_opener(url: str, app: str | None) -> None:
    if sys.platform != "darwin":
        raise BridgeHostError("POLICY_DENIED", "URL opening is supported only on macOS")
    argv = ["/usr/bin/open"]
    if app is not None:
        argv.extend(["-a", app])
    argv.append(url)
    completed = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise BridgeHostError("TARGET_NOT_FOUND", "macOS could not open the requested URL")


class BridgeController:
    """Translate bounded bridge requests into existing computer-use primitives."""

    def __init__(
        self,
        driver: Any,
        *,
        url_opener: Callable[[str, str | None], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._driver = driver
        self._url_opener = url_opener or _default_url_opener
        self._sleep = sleep
        self._last_snapshots: dict[str, tuple[AXElement, ...]] = {}

    def dispatch(self, method: str, params: dict[str, object]) -> object:
        handlers: dict[str, Callable[[dict[str, object]], object]] = {
            "health": self._health,
            "active_window": self._active_window,
            "list_apps": self._list_apps,
            "ui_snapshot": self._ui_snapshot,
            "open_app": self._open_app,
            "open_url": self._open_url,
            "screenshot": self._screenshot,
            "click": self._click,
            "drag": self._drag,
            "scroll": self._scroll,
            "type_text": self._type_text,
            "press_hotkey": self._press_hotkey,
            "release_inputs": self._release_inputs,
        }
        handler = handlers.get(method)
        if handler is None:
            raise BridgeHostError("BRIDGE_PROTOCOL_INVALID", "unsupported bridge method")
        return handler(params)

    def _health(self, _params: dict[str, object]) -> object:
        try:
            raw = cast(dict[str, object], self._driver.health())
        except Exception as exc:
            raise BridgeHostError("DRIVER_UNAVAILABLE", "actuation driver is unavailable") from exc
        return {
            "backend": str(raw.get("backend", "unknown")),
            "trusted": bool(raw.get("trusted", False)),
            "kill_listener_armed": raw.get("kill_listener_armed"),
        }

    def _focused(self) -> FocusedWindow:
        try:
            raw = self._driver.focused_window()
        except Exception as exc:
            raise BridgeHostError("DRIVER_UNAVAILABLE", "focused-window state is unavailable") from exc
        if isinstance(raw, FocusedWindow):
            return raw
        try:
            return FocusedWindow.model_validate(raw)
        except ValueError as exc:
            raise BridgeHostError("DRIVER_UNAVAILABLE", "driver returned invalid window state") from exc

    def _active_window(self, _params: dict[str, object]) -> object:
        focused = self._focused()
        return {
            "app_name": focused.app_name,
            "bundle_id": focused.bundle_id,
            "window_title": focused.window_title,
            "cursor_x": focused.cursor_x,
            "cursor_y": focused.cursor_y,
        }

    def _list_apps(self, _params: dict[str, object]) -> object:
        try:
            return {"apps": list(self._driver.list_apps())}
        except Exception as exc:
            raise BridgeHostError("DRIVER_UNAVAILABLE", "running applications are unavailable") from exc

    def _app_pid(self, app: str) -> int:
        if not app.strip():
            raise BridgeHostError("TARGET_NOT_FOUND", "target application is required")
        try:
            pid = self._driver.app_pid(app)
        except Exception as exc:
            raise BridgeHostError("DRIVER_UNAVAILABLE", "application lookup failed") from exc
        if not isinstance(pid, int):
            raise BridgeHostError("TARGET_NOT_FOUND", "target application is not running")
        return pid

    def _snapshot_root(self, app: str) -> AXElement:
        pid = self._app_pid(app)
        try:
            root = self._driver.ax_snapshot(pid=pid)
        except Exception as exc:
            raise BridgeHostError("DRIVER_UNAVAILABLE", "accessibility snapshot is unavailable") from exc
        if isinstance(root, AXElement):
            return root
        try:
            return AXElement.model_validate(root)
        except ValueError as exc:
            raise BridgeHostError("DRIVER_UNAVAILABLE", "invalid accessibility snapshot") from exc

    @staticmethod
    def _interactive(root: AXElement) -> tuple[AXElement, ...]:
        found: list[AXElement] = []

        def walk(node: AXElement) -> None:
            if node.role in INTERACTIVE_ROLES:
                found.append(node)
            for child in node.children:
                walk(child)

        walk(root)
        return tuple(found)

    def _ui_snapshot(self, params: dict[str, object]) -> object:
        app = self._required_string(params, "app")
        requested = params.get("max_elements", DEFAULT_MAX_ELEMENTS)
        if not isinstance(requested, int) or isinstance(requested, bool):
            raise BridgeHostError("BRIDGE_PROTOCOL_INVALID", "max_elements must be an integer")
        limit = min(max(requested, 1), MAX_ELEMENTS)
        all_elements = self._interactive(self._snapshot_root(app))
        elements = all_elements[:limit]
        self._last_snapshots[app] = elements
        return {
            "app": app,
            "elements": [
                {
                    "index": index,
                    "role": element.role,
                    "title": element.title,
                    "value": "",
                    "focused": element.focused,
                    "x": element.x,
                    "y": element.y,
                    "width": element.width,
                    "height": element.height,
                }
                for index, element in enumerate(elements, start=1)
            ],
            "truncated": len(all_elements) > limit,
        }

    def _kill_gate(self) -> None:
        try:
            tripped = self._driver.hotkey_state()
        except Exception as exc:
            raise BridgeHostError("DRIVER_UNAVAILABLE", "kill-switch state is unavailable") from exc
        if tripped is True:
            raise BridgeHostError("KILL_SWITCH_TRIPPED", "the user reclaimed control")

    @staticmethod
    def _matches_app(app: str, focused: FocusedWindow) -> bool:
        return app_evidence(app, focused.app_name, focused.bundle_id) is Evidence.CONFIRMED

    def _ensure_focus(self, app: str) -> None:
        self._kill_gate()
        if self._matches_app(app, self._focused()):
            return
        try:
            self._driver.activate_app(app)
        except Exception as exc:
            raise BridgeHostError("FOCUS_NOT_ACQUIRED", "target application could not be activated") from exc
        for _ in range(FOCUS_SETTLE_POLLS):
            if self._matches_app(app, self._focused()):
                return
            self._sleep(FOCUS_SETTLE_INTERVAL_SECONDS)
        raise BridgeHostError("FOCUS_NOT_ACQUIRED", "target application did not acquire focus")

    def _credential_gate(self, app: str) -> None:
        if asks_for_a_credential(self._snapshot_root(app)):
            raise BridgeHostError(
                "CREDENTIAL_ENTRY_REFUSED",
                "keyboard input is refused while a secure credential field is present",
            )

    def _open_app(self, params: dict[str, object]) -> object:
        app = self._required_string(params, "app")
        self._ensure_focus(app)
        return {"app": app, "opened": True}

    def _open_url(self, params: dict[str, object]) -> object:
        self._kill_gate()
        url = self._required_string(params, "url")
        app = self._optional_string(params.get("app"), "app")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise BridgeHostError("POLICY_DENIED", "only http and https URLs are allowed")
        try:
            self._url_opener(url, app)
        except BridgeHostError:
            raise
        except Exception as exc:
            raise BridgeHostError("TARGET_NOT_FOUND", "URL could not be opened") from exc
        if app is not None:
            self._ensure_focus(app)
        return {"opened": True, "app": app}

    def _screenshot(self, params: dict[str, object]) -> object:
        app = self._optional_string(params.get("app"), "app")
        pid = self._app_pid(app) if app is not None else None
        try:
            capture = self._driver.capture(display_id=0, window_pid=pid)
        except TypeError:
            capture = self._driver.capture()
        except Exception as exc:
            raise BridgeHostError("DRIVER_UNAVAILABLE", "screen capture failed") from exc
        if not isinstance(capture, ScreenCapture) or not capture.data:
            raise BridgeHostError("DRIVER_UNAVAILABLE", "screen capture returned no image")
        png = capture_to_base64_png(capture)
        if not png:
            raise BridgeHostError("DRIVER_UNAVAILABLE", "screen capture encoding failed")
        return {
            "mime_type": "image/png",
            "data_base64": png,
            "width": capture.width,
            "height": capture.height,
            "scale": capture.scale,
            "origin_x": capture.origin_x,
            "origin_y": capture.origin_y,
        }

    def _resolve_target(
        self, app: str, params: dict[str, object]
    ) -> tuple[int, int, str | None]:
        raw_x = params.get("x")
        raw_y = params.get("y")
        if (
            isinstance(raw_x, int)
            and not isinstance(raw_x, bool)
            and isinstance(raw_y, int)
            and not isinstance(raw_y, bool)
        ):
            if raw_x < 0 or raw_y < 0:
                raise BridgeHostError("TARGET_NOT_FOUND", "coordinates must be non-negative")
            return raw_x, raw_y, None

        index_raw = params.get("element_index", params.get("elementIndex"))
        if isinstance(index_raw, int) and not isinstance(index_raw, bool):
            previous = self._last_snapshots.get(app)
            if previous is None or index_raw < 1 or index_raw > len(previous):
                raise BridgeHostError("TARGET_NOT_FOUND", "element index is stale or unknown")
            expected = previous[index_raw - 1]
            current = self._interactive(self._snapshot_root(app))
            matches = [
                element
                for element in current
                if element.role == expected.role and element.title == expected.title
            ]
            if len(matches) != 1:
                raise BridgeHostError(
                    "TARGET_NOT_FOUND", "element index no longer identifies one target"
                )
            return self._centre(matches[0])

        query = self._optional_string(params.get("query"), "query")
        role = self._optional_string(params.get("role"), "role")
        title = self._optional_string(params.get("title"), "title")
        if query is None and role is None and title is None:
            raise BridgeHostError("TARGET_NOT_FOUND", "a target coordinate or locator is required")
        needle = query.casefold() if query is not None else None
        candidates: list[AXElement] = []
        for element in self._interactive(self._snapshot_root(app)):
            if role is not None and element.role.removeprefix("AX") != role.removeprefix("AX"):
                continue
            if title is not None and title.casefold() not in element.title.casefold():
                continue
            if needle is not None and needle not in f"{element.title} {element.role}".casefold():
                continue
            candidates.append(element)
        if len(candidates) != 1:
            raise BridgeHostError("TARGET_NOT_FOUND", "locator did not identify exactly one target")
        return self._centre(candidates[0])

    @staticmethod
    def _centre(element: AXElement) -> tuple[int, int, str | None]:
        return (
            int(element.x + element.width / 2),
            int(element.y + element.height / 2),
            element.title or element.role,
        )

    def _click(self, params: dict[str, object]) -> object:
        app = self._required_string(params, "app")
        self._ensure_focus(app)
        x, y, _label = self._resolve_target(app, params)
        try:
            move = MouseMove(type="mouse_move", x=x, y=y)
            click = MouseClick.model_validate(
                {
                    "type": "mouse_click",
                    "x": x,
                    "y": y,
                    "button": params.get("button", params.get("mouse_button", "left")),
                    "click_count": params.get("click_count", 1),
                }
            )
            self._driver.send(move)
            self._driver.send(click)
        except Exception:
            self._safe_release()
            raise
        return {"acted": True}

    def _drag(self, params: dict[str, object]) -> object:
        app = self._required_string(params, "app")
        self._ensure_focus(app)
        start_raw = params.get("start")
        end_raw = params.get("end")
        if not isinstance(start_raw, dict) or not isinstance(end_raw, dict):
            raise BridgeHostError("BRIDGE_PROTOCOL_INVALID", "drag requires start and end targets")
        start = self._resolve_target(app, cast(dict[str, object], start_raw))
        end = self._resolve_target(app, cast(dict[str, object], end_raw))
        try:
            action = MouseDrag.model_validate(
                {
                    "type": "mouse_drag",
                    "start_x": start[0],
                    "start_y": start[1],
                    "end_x": end[0],
                    "end_y": end[1],
                    "duration_ms": params.get("duration_ms", 250),
                }
            )
            self._driver.send(action)
        except Exception:
            self._safe_release()
            raise
        return {"acted": True}

    def _scroll(self, params: dict[str, object]) -> object:
        app = self._required_string(params, "app")
        self._ensure_focus(app)
        direction = self._optional_string(params.get("direction"), "direction") or "down"
        pages = params.get("pages", 1)
        if not isinstance(pages, int) or isinstance(pages, bool) or not 1 <= pages <= 20:
            raise BridgeHostError("BRIDGE_PROTOCOL_INVALID", "pages must be an integer from 1 to 20")
        unit = 120 * pages
        axes = {
            "down": (0, unit),
            "up": (0, -unit),
            "right": (unit, 0),
            "left": (-unit, 0),
        }
        if direction not in axes:
            raise BridgeHostError("BRIDGE_PROTOCOL_INVALID", "unsupported scroll direction")
        try:
            dx, dy = axes[direction]
            self._driver.send(MouseScroll(type="mouse_scroll", dx=dx, dy=dy))
        except Exception:
            self._safe_release()
            raise
        return {"acted": True}

    def _type_text(self, params: dict[str, object]) -> object:
        app = self._required_string(params, "app")
        text = self._required_string(params, "text", allow_empty=True)
        self._ensure_focus(app)
        self._credential_gate(app)
        try:
            self._driver.send(
                TypeText.model_validate(
                    {"type": "type_text", "text": text, "wpm": params.get("wpm", 120)}
                )
            )
        except Exception:
            self._safe_release()
            raise
        return {"acted": True}

    def _press_hotkey(self, params: dict[str, object]) -> object:
        app = self._required_string(params, "app")
        key = self._required_string(params, "key")
        modifiers_object = params.get("modifiers", [])
        if not isinstance(modifiers_object, list):
            raise BridgeHostError("BRIDGE_PROTOCOL_INVALID", "modifiers must be a list of strings")
        modifiers: list[str] = []
        for item in cast(list[object], modifiers_object):
            if not isinstance(item, str):
                raise BridgeHostError(
                    "BRIDGE_PROTOCOL_INVALID", "modifiers must be a list of strings"
                )
            modifiers.append(item)
        self._ensure_focus(app)
        self._credential_gate(app)
        try:
            self._driver.send(
                PressHotkey.model_validate(
                    {"type": "press_hotkey", "modifiers": modifiers, "key": key}
                )
            )
        except Exception:
            self._safe_release()
            raise
        return {"acted": True}

    def _release_inputs(self, _params: dict[str, object]) -> object:
        self._safe_release()
        return {"released": True}

    def _safe_release(self) -> None:
        with suppress(Exception):
            self._driver.release_inputs()

    @staticmethod
    def _required_string(
        params: dict[str, object], key: str, *, allow_empty: bool = False
    ) -> str:
        value = params.get(key)
        if not isinstance(value, str) or (not allow_empty and not value.strip()):
            raise BridgeHostError("BRIDGE_PROTOCOL_INVALID", f"{key} must be a string")
        return value

    @staticmethod
    def _optional_string(value: object, key: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise BridgeHostError("BRIDGE_PROTOCOL_INVALID", f"{key} must be a non-empty string")
        return value
