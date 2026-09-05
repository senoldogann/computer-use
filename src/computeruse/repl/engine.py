"""CUA REPL Engine (Code-as-Action multi-step executor).

Executes model-generated JavaScript in a dedicated Node.js bridge environment
with the `globalThis.cua` API surface, coordinating with the Rust Micro-Driver
for fast accessibility-guided actions and token-efficient AX diffing.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from computeruse.orchestrator.schemas import (
    ClipboardPaste,
    MouseClick,
    MouseMove,
    PressHotkey,
    TypeText,
)
from computeruse.vision.ax import AXElement
from computeruse.vision.ax_diff import AXStateTracker

LOGGER = logging.getLogger(__name__)

BRIDGE_SCRIPT_PATH = Path(__file__).parent / "cua_bridge.js"


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
    ) -> None:
        self.driver_client = driver_client
        self.node_binary = node_binary
        self.snapshot_provider = snapshot_provider
        self.trackers: dict[str, AXStateTracker] = {}
        self._proc: subprocess.Popen[str] | None = None
        self._last_content: str = ""

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

    def _get_app_snapshot(self, app_name: str) -> tuple[AXElement, str]:
        """Fetch accessibility snapshot and window title for an app."""
        if self.snapshot_provider:
            return self.snapshot_provider(app_name)

        if self.driver_client:
            # Native driver call
            try:
                self.driver_client.activate_app(app_name)
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

    def _dispatch_js_call(self, method: str, params: dict[str, Any]) -> Any:
        """Route calls from the JS runtime to the appropriate host driver handler."""
        if method == "getApp":
            app_name = params["app"]
            tracker = self._get_tracker(app_name)
            if not tracker.last_nodes:
                snap, win_title = self._get_app_snapshot(app_name)
                state_text = tracker.render_state(snap, win_title)
            else:
                # Re-use current rendered state if already present
                state_text = tracker.render_state(
                    AXElement(
                        role="Window",
                        title=tracker.last_window_title or app_name,
                        width=800,
                        height=600,
                    ),
                    tracker.last_window_title or app_name,
                    disable_diffing=True,
                )
            self._last_content = state_text
            return {
                "id": f"com.apple.{app_name}",
                "name": app_name,
                "initialAXState": state_text,
            }

        if method == "getAXState":
            app_name = params["app"]
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
            tracker = self._get_tracker(app_name)
            elem_index = params.get("elementIndex")
            x = params.get("x")
            y = params.get("y")

            target_x = x
            target_y = y
            if elem_index is not None:
                elem = tracker.get_element_by_index(int(elem_index))
                if elem:
                    target_x = elem.centre_x
                    target_y = elem.centre_y
                else:
                    LOGGER.warning(
                        "Element index %d not found in %s tracker", elem_index, app_name
                    )

            if target_x is not None and target_y is not None and self.driver_client:
                self.driver_client.send(
                    MouseMove(type="mouse_move", x=int(target_x), y=int(target_y))
                )
                self.driver_client.send(
                    MouseClick(
                        type="mouse_click",
                        x=int(target_x),
                        y=int(target_y),
                        button=params.get("mouseButton", "left"),
                        click_count=params.get("clickCount", 1),
                    )
                )

            return None

        if method == "pressKey":
            key = params["key"]
            if self.driver_client:
                self.driver_client.send(PressHotkey(type="press_hotkey", key=key))
            return None

        if method == "typeText":
            text = params["text"]
            if self.driver_client:
                self.driver_client.send(TypeText(type="type_text", text=text))
            return None

        if method == "paste":
            text = params["text"]
            if self.driver_client:
                self.driver_client.send(
                    ClipboardPaste(type="clipboard_paste", text=text)
                )
            return None

        if method == "setValue":
            app_name = params["app"]
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
                self.driver_client.send(TypeText(type="type_text", text=value))
            return None

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
