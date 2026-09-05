"""CUA REPL Engine (Code-as-Action multi-step executor).

Executes model-generated JavaScript in a dedicated Node.js bridge environment
with the `globalThis.cua` API surface, coordinating with the Rust Micro-Driver
for fast accessibility-guided actions and token-efficient AX diffing.

Provides enterprise-grade resilience:
- Constitutional autonomy and capability grant enforcement (security gate).
- Dynamic self-healing element resolution (stale node recovery).
- Cross-application focus synchronization and state isolation.
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from computeruse.orchestrator.schemas import (
    Action,
    AgentTurn,
    ClipboardPaste,
    MouseClick,
    MouseDrag,
    MouseMove,
    MouseScroll,
    PressHotkey,
    TypeText,
)
from computeruse.security.autonomy import (
    AutonomyLevel,
    classify_risk,
    decide_permission,
)
from computeruse.security.grants import GrantStore, authorize
from computeruse.security.permissions import (
    PermissionDecision,
    PermissionDeniedError,
)
from computeruse.vision.ax import AXElement
from computeruse.vision.ax_diff import AXStateTracker

LOGGER = logging.getLogger(__name__)

BRIDGE_SCRIPT_PATH = Path(__file__).parent / "cua_bridge.js"

ModifierType = Literal["command", "control", "alt", "shift"]


def parse_hotkey_action(
    raw_key: str, raw_modifiers: list[str] | None = None
) -> PressHotkey:
    """Parse hotkey combinations (e.g. 'Cmd+Shift+P' or modifiers=['command'], key='a')."""
    normalized_mods: list[ModifierType] = []
    alias_map: dict[str, ModifierType] = {
        "cmd": "command",
        "command": "command",
        "ctrl": "control",
        "control": "control",
        "alt": "alt",
        "opt": "alt",
        "option": "alt",
        "shift": "shift",
    }

    if raw_modifiers:
        for m in raw_modifiers:
            clean_m = alias_map.get(m.lower().strip())
            if clean_m and clean_m not in normalized_mods:
                normalized_mods.append(clean_m)

    key_parts = [p.strip().lower() for p in raw_key.split("+") if p.strip()]
    if len(key_parts) > 1:
        primary = key_parts[-1]
        for part in key_parts[:-1]:
            mod = alias_map.get(part)
            if mod and mod not in normalized_mods:
                normalized_mods.append(mod)
    else:
        primary = key_parts[0] if key_parts else raw_key.lower().strip()

    # Normalize special key names
    key_aliases: dict[str, str] = {
        "enter": "return",
        "esc": "escape",
        "spacebar": "space",
        " ": "space",
    }
    canonical_key = key_aliases.get(primary, primary)

    return PressHotkey(
        type="press_hotkey",
        modifiers=normalized_mods,
        key=canonical_key,
    )


@dataclass
class CuaReplResult:
    """Result of evaluating a CUA JavaScript code block."""

    status: str
    duration_ms: int
    content: str
    error: str | None = None

    def to_mcp_tool_call(
        self,
        call_id: str,
        code: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Format as an official CUA MCP tool call response."""
        return {
            "type": "mcpToolCall",
            "id": call_id,
            "tool": "js",
            "server": "cua_repl",
            "status": self.status,
            "arguments": {
                "code": code,
                "title": title,
            },
            "appContext": None,
            "error": self.error,
            "durationMs": self.duration_ms,
            "result": {
                "content": self.content,
            },
        }


class CuaReplEngine:
    """Manages the Node.js bridge process and handles callbacks from JavaScript."""

    def __init__(
        self,
        *,
        driver_client: Any = None,
        node_binary: str = "node",
        snapshot_provider: Callable[[str], tuple[AXElement, str]] | None = None,
        autonomy_level: AutonomyLevel = AutonomyLevel.FULL,
        grant_store: GrantStore | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.driver_client = driver_client
        self.node_binary = node_binary
        self.snapshot_provider = snapshot_provider
        self.autonomy_level = autonomy_level
        self.grant_store = grant_store
        self.now_provider = now_provider
        self.trackers: dict[str, AXStateTracker] = {}
        self._proc: subprocess.Popen[str] | None = None
        self._last_content: str = ""
        self._active_app: str | None = None

    def start(self) -> None:
        """Spawn the background Node.js bridge process."""
        if self._proc is not None:
            return

        self._proc = subprocess.Popen(
            [self.node_binary, str(BRIDGE_SCRIPT_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        # Wait for the "ready" signal
        assert self._proc.stdout is not None
        ready_line = self._proc.stdout.readline()
        try:
            data = json.loads(ready_line)
            if data.get("method") != "ready":
                raise RuntimeError(f"Unexpected bridge startup output: {ready_line}")
        except Exception as exc:
            self.stop()
            raise RuntimeError(f"Failed to initialize CUA REPL bridge: {exc}") from exc

    def stop(self) -> None:
        """Terminate the Node.js bridge process."""
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2.0)
            except Exception:  # noqa: BLE001
                self._proc.kill()
            finally:
                self._proc = None

    def execute(
        self, code: str, title: str | None = None, timeout_s: float = 60.0
    ) -> CuaReplResult:
        """Execute a JavaScript snippet through the bridge and handle incoming RPCs."""
        self.start()
        assert self._proc is not None
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None

        start_time = time.monotonic()
        eval_id = int(time.monotonic() * 1000)
        self._last_content = ""

        # Send eval request
        request = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": eval_id,
                "method": "eval",
                "params": {"code": code},
            }
        )
        self._proc.stdin.write(request + "\n")
        self._proc.stdin.flush()

        deadline = start_time + timeout_s

        while time.monotonic() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_id = msg.get("id")

            # Check if this is the final response to our eval request
            if msg_id == eval_id:
                duration_ms = int((time.monotonic() - start_time) * 1000)
                if "error" in msg:
                    return CuaReplResult(
                        status="failed",
                        duration_ms=duration_ms,
                        content="",
                        error=msg["error"].get("message", "Unknown error"),
                    )
                # Success
                content = msg.get("result", {}).get("content", "")
                if not content and self._last_content:
                    content = self._last_content
                if not content:
                    content = "## Computer Use"

                return CuaReplResult(
                    status="completed",
                    duration_ms=duration_ms,
                    content=content,
                )

            # Otherwise, this is a method call from JS to Python (e.g. getApp, click, getAXState)
            method = msg.get("method")
            params = msg.get("params", {})
            try:
                result = self._dispatch_js_call(method, params)
                resp = json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result})
            except Exception as exc:
                LOGGER.exception("Error executing bridge RPC %s", method)
                resp = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32603, "message": str(exc)},
                    }
                )

            self._proc.stdin.write(resp + "\n")
            self._proc.stdin.flush()

        duration_ms = int((time.monotonic() - start_time) * 1000)
        return CuaReplResult(
            status="failed",
            duration_ms=duration_ms,
            content="",
            error="CUA REPL execution timed out",
        )

    def _get_tracker(self, app_name: str) -> AXStateTracker:
        if app_name not in self.trackers:
            self.trackers[app_name] = AXStateTracker(app_name=app_name)
        return self.trackers[app_name]

    def _ensure_app_active(self, app_name: str) -> None:
        """Ensure targeted application is frontmost before actuation."""
        if self._active_app != app_name and self.driver_client:
            try:
                self.driver_client.activate_app(app_name)
                self._active_app = app_name
                time.sleep(0.05)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("activate_app failed for %s: %s", app_name, exc)

    def _get_app_snapshot(self, app_name: str) -> tuple[AXElement, str]:
        """Fetch accessibility snapshot and window title for an app."""
        if self.snapshot_provider:
            return self.snapshot_provider(app_name)

        if self.driver_client:
            # Native driver call
            try:
                self.driver_client.activate_app(app_name)
                self._active_app = app_name
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("activate_app failed for %s: %s", app_name, exc)

            pid = None
            try:
                pid = self.driver_client.app_pid(app_name)
            except Exception:  # noqa: BLE001, S110
                pass

            if pid is None:
                try:
                    win = self.driver_client.focused_window()
                    pid = win.pid
                except Exception:  # noqa: BLE001, S110
                    pass

            if pid is not None:
                try:
                    snap = self.driver_client.ax_snapshot(pid=pid)
                    win = self.driver_client.focused_window()
                    title = (win.window_title or win.app_name) if win else app_name
                    return snap, title
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("ax_snapshot failed for pid %s: %s", pid, exc)

        # Fallback simulated root
        return (
            AXElement(role="Window", title=app_name, width=800, height=600),
            app_name,
        )

    def _resolve_target_point(
        self,
        app_name: str,
        elem_index: int | None,
        x: int | None,
        y: int | None,
    ) -> tuple[int | None, int | None, str | None]:
        """Resolve element coordinates and target label with self-healing stale locator recovery."""
        if elem_index is not None:
            idx = int(elem_index)
            tracker = self._get_tracker(app_name)
            elem = tracker.get_element_by_index(idx)
            if elem:
                label = elem.title or elem.role
                return elem.centre_x, elem.centre_y, label

            # Self-healing locator: Check historical elements
            historical = tracker.get_historical_element(idx)
            if historical:
                LOGGER.info(
                    "Attempting self-healing recovery for stale index [%d] (%s '%s')",
                    idx,
                    historical.role,
                    historical.title,
                )
                # Refresh snapshot and re-render
                snap, win_title = self._get_app_snapshot(app_name)
                tracker.render_state(snap, win_title)
                healed = tracker.find_matching_element(historical.role, historical.title)
                if healed:
                    LOGGER.info(
                        "Self-healed stale index [%d] -> new index [%d] at (%d, %d)",
                        idx,
                        healed.index,
                        healed.centre_x,
                        healed.centre_y,
                    )
                    return healed.centre_x, healed.centre_y, healed.title or healed.role

            raise ValueError(
                f"Element index [{idx}] was not found or has become stale in '{app_name}'. "
                f"Call `await app.getAXState()` to inspect current layout."
            )

        return x, y, None

    def _check_security(
        self, action: Action, app_name: str, target_label: str | None = None
    ) -> None:
        """Enforce constitutional autonomy levels and capability grants."""
        turn = AgentTurn(
            thought="CUA REPL execution",
            sub_goal=f"CUA REPL execute in {app_name}",
            action=action,
        )
        risk = classify_risk(turn, target_label=target_label)
        decision = decide_permission(self.autonomy_level, risk)

        if decision == PermissionDecision.ALLOW:
            return

        # Attempt to authorize via active capability grants
        if self.grant_store is not None:
            now = self.now_provider() if self.now_provider else datetime.now(UTC)
            all_grants = self.grant_store.grants()
            verdict = authorize(
                action,
                sub_goal=turn.sub_goal,
                target_label=target_label,
                app=app_name,
                grants=all_grants,
                now=now,
            )
            if verdict.is_granted and verdict.grant_id:
                self.grant_store.consume(verdict.grant_id)
                LOGGER.info(
                    "CUA REPL action covered by grant %s (%s)",
                    verdict.grant_id,
                    verdict.reason,
                )
                return
            raise PermissionDeniedError(
                f"CUA REPL Security Refusal: Action risk '{risk.value.upper()}' on "
                f"{target_label or 'control'!r} in {app_name} refused: {verdict.reason}"
            )

        raise PermissionDeniedError(
            f"CUA REPL Security Refusal: Action risk '{risk.value.upper()}' on "
            f"{target_label or 'control'!r} in {app_name} blocked under autonomy level {self.autonomy_level.name}"
        )

    def _dispatch_js_call(self, method: str, params: dict[str, Any]) -> Any:
        """Route calls from the JS runtime to the appropriate host driver handler."""
        if method == "getApp":
            app_name = params["app"]
            self._ensure_app_active(app_name)
            tracker = self._get_tracker(app_name)
            if not tracker.last_nodes:
                snap, win_title = self._get_app_snapshot(app_name)
                state_text = tracker.render_state(snap, win_title)
            else:
                lines = [
                    "## Computer Use",
                    f'Window: "{tracker.last_window_title or app_name}", App: {app_name}.',
                    "",
                    "Accessibility Tree:",
                ]
                for n in tracker.last_nodes:
                    lines.append(n.summary_line())
                state_text = "\n".join(lines)

            self._last_content = state_text
            return {
                "id": f"com.apple.{app_name}",
                "name": app_name,
                "initialAXState": state_text,
            }

        if method == "getAXState":
            app_name = params["app"]
            self._ensure_app_active(app_name)
            disable_diff = params.get("disableDiffing", False)
            tracker = self._get_tracker(app_name)
            snap, win_title = self._get_app_snapshot(app_name)
            state_text = tracker.render_state(
                snap, win_title, disable_diffing=disable_diff
            )
            self._last_content = state_text
            return state_text

        if method == "click":
            app_name = params["app"]
            self._ensure_app_active(app_name)
            target_x, target_y, target_label = self._resolve_target_point(
                app_name,
                params.get("elementIndex"),
                params.get("x"),
                params.get("y"),
            )

            if target_x is not None and target_y is not None:
                click_action = MouseClick(
                    type="mouse_click",
                    x=int(target_x),
                    y=int(target_y),
                    button=params.get("mouseButton", "left"),
                    click_count=params.get("clickCount", 1),
                )
                self._check_security(click_action, app_name, target_label)

                if self.driver_client:
                    self.driver_client.send(
                        MouseMove(type="mouse_move", x=int(target_x), y=int(target_y))
                    )
                    self.driver_client.send(click_action)

            return None

        if method == "drag":
            app_name = params["app"]
            self._ensure_app_active(app_name)
            start_x, start_y, start_label = self._resolve_target_point(
                app_name,
                params.get("startElementIndex"),
                params.get("startX"),
                params.get("startY"),
            )
            end_x, end_y, _ = self._resolve_target_point(
                app_name,
                params.get("endElementIndex"),
                params.get("endX"),
                params.get("endY"),
            )

            if (
                start_x is not None
                and start_y is not None
                and end_x is not None
                and end_y is not None
            ):
                drag_action = MouseDrag(
                    type="mouse_drag",
                    start_x=int(start_x),
                    start_y=int(start_y),
                    end_x=int(end_x),
                    end_y=int(end_y),
                    duration_ms=params.get("durationMs", 250),
                )
                self._check_security(drag_action, app_name, start_label)

                if self.driver_client:
                    self.driver_client.send(drag_action)
            return None

        if method == "scroll":
            app_name = params["app"]
            self._ensure_app_active(app_name)
            target_x, target_y, _ = self._resolve_target_point(
                app_name,
                params.get("elementIndex"),
                params.get("x"),
                params.get("y"),
            )

            if target_x is not None and target_y is not None and self.driver_client:
                self.driver_client.send(
                    MouseMove(type="mouse_move", x=int(target_x), y=int(target_y))
                )

            direction = str(params.get("direction", "down")).lower()
            pages = int(params.get("pages", 1))
            unit = 120 * pages

            dx = 0
            dy = 0
            if direction == "down":
                dy = unit
            elif direction == "up":
                dy = -unit
            elif direction == "right":
                dx = unit
            elif direction == "left":
                dx = -unit

            scroll_action = MouseScroll(type="mouse_scroll", dx=dx, dy=dy)
            self._check_security(scroll_action, app_name, None)

            if self.driver_client:
                self.driver_client.send(scroll_action)
            return None

        if method == "pressKey":
            app_name = params.get("app", "System")
            self._ensure_app_active(app_name)
            raw_key = str(params["key"])
            raw_mods = params.get("modifiers")
            mod_list: list[str] = (
                [str(m) for m in cast(list[object], raw_mods)]
                if isinstance(raw_mods, list)
                else []
            )
            hotkey_action = parse_hotkey_action(raw_key, mod_list)
            self._check_security(hotkey_action, app_name, None)

            if self.driver_client:
                self.driver_client.send(hotkey_action)
            return None

        if method == "typeText":
            app_name = params.get("app", "System")
            self._ensure_app_active(app_name)
            text = params["text"]
            type_action = TypeText(type="type_text", text=text)
            self._check_security(type_action, app_name, None)

            if self.driver_client:
                self.driver_client.send(type_action)
            return None

        if method == "paste":
            app_name = params.get("app", "System")
            self._ensure_app_active(app_name)
            text = params["text"]
            paste_action = ClipboardPaste(type="clipboard_paste", text=text)
            self._check_security(paste_action, app_name, None)

            if self.driver_client:
                self.driver_client.send(paste_action)
            return None

        if method == "setValue":
            app_name = params["app"]
            self._ensure_app_active(app_name)
            elem_index = params["elementIndex"]
            value = params["value"]
            # Click to focus, select all, then type
            self._dispatch_js_call(
                "click", {"app": app_name, "elementIndex": elem_index}
            )
            if self.driver_client:
                self.driver_client.send(
                    PressHotkey(type="press_hotkey", modifiers=["command"], key="a")
                )
                type_action = TypeText(type="type_text", text=value)
                self._check_security(type_action, app_name, None)
                self.driver_client.send(type_action)
            return None

        if method == "getScreenshot":
            if self.driver_client:
                try:
                    if hasattr(self.driver_client, "capture"):
                        cap = self.driver_client.capture()
                    else:
                        cap = self.driver_client.screenshot()
                    b64 = base64.b64encode(cap.data).decode("ascii")
                    return f"data:image/png;base64,{b64}"
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("screenshot failed: %s", exc)
            return "data:image/png;base64,"

        if method == "listApps":
            if self.driver_client:
                apps = self.driver_client.list_apps()
                return [{"id": a, "displayName": a, "isRunning": True} for a in apps]
            return [
                {
                    "id": "com.apple.TextEdit",
                    "displayName": "TextEdit",
                    "isRunning": True,
                }
            ]

        if method == "getState":
            return {"apps": []}

        raise NotImplementedError(f"Unsupported CUA bridge method: {method}")
