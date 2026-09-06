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

import json
import logging
import os
import selectors
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from computeruse.orchestrator.client import OCR_MAX_LINES, OCR_MIN_CONFIDENCE
from computeruse.orchestrator.evidence import Evidence, app_evidence
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
    Risk,
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
from computeruse.vision.capture import (
    ScreenCapture,
    capture_to_base64_png,
    crop_capture,
    logical_region_to_pixels,
)
from computeruse.vision.coordinates import Point, Rect, Size

LOGGER = logging.getLogger(__name__)

BRIDGE_SCRIPT_PATH = Path(__file__).parent / "cua_bridge.js"

#: How long to wait for a newly activated application to actually come
#: forward. macOS answers ``activate_app`` before the switch has happened, so
#: the confirming read has to be given a moment — but only a moment: an app
#: that has not surfaced in half a second is not going to.
FOCUS_SETTLE_POLLS: int = 10
FOCUS_SETTLE_INTERVAL_S: float = 0.05


def _first_window(node: AXElement) -> AXElement | None:
    """The application's window element, or ``None`` (pure).

    Breadth-first: a window is a direct child of the application root in every
    tree this has been seen on, and going deep would find sheets and popovers
    before the window that contains them.
    """
    queue: list[AXElement] = [node]
    while queue:
        current = queue.pop(0)
        if current.role.endswith("Window"):
            return current
        queue.extend(current.children)
    return None


class WindowBoundsUnavailableError(RuntimeError):
    """The application's window geometry could not be read.

    Raised rather than answered with a plausible-looking rectangle: a model
    that trusts invented geometry computes click coordinates from it, and a
    wrong coordinate on a physical host is a click on whatever is really there.
    """


class ScreenCaptureUnavailableError(RuntimeError):
    """The screen could not be photographed.

    Raised rather than answered with an empty image. A failed capture used to
    return the bare data-URI prefix ``data:image/png;base64,`` — well-formed,
    zero pixels — so every caller's "did I get a screenshot?" test passed and
    the model was handed a blank frame described as the screen. Measured
    against the live backend with Screen Recording consent absent: the driver
    refused display 0, the engine logged a warning, and the audit's own
    ``"data:image/png;base64," in content`` assertion passed anyway.

    Visual confirmation that cannot fail confirms nothing, so this says so.
    """


class FocusNotAcquiredError(RuntimeError):
    """The target application could not be confirmed frontmost.

    Raised instead of actuating, because a keystroke is delivered to whatever
    holds focus at the moment it is posted — not to the application the script
    named. Continuing after a failed activation does not degrade the action, it
    redirects it.
    """

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

    @property
    def is_error(self) -> bool:
        return self.status == "failed" or bool(self.error)

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
            # The bridge does not need credentials or Node preload hooks.
            env={key: value for key, value in os.environ.items()
                 if key in {"PATH", "SYSTEMROOT", "LANG", "LC_ALL", "TMPDIR"}},
        )

        # Wait for the "ready" signal
        assert self._proc.stdout is not None
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(self._proc.stdout, selectors.EVENT_READ)
                if not ready.select(timeout=10):
                    raise TimeoutError("CUA bridge did not become ready within 10s")
            ready_line = self._proc.stdout.readline()
            if not ready_line:
                self._proc.wait(timeout=2)
                assert self._proc.stderr is not None
                diagnostic = self._proc.stderr.read()
                raise RuntimeError(
                    f"CUA bridge exited with status {self._proc.returncode}: {diagnostic}. "
                    f"Install runtime with npm ci --prefix {BRIDGE_SCRIPT_PATH.parent} --ignore-scripts"
                )
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
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=2.0)
            finally:
                for stream in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
                    if stream is not None:
                        stream.close()
                self._proc = None

    def execute(
        self, code: str, title: str | None = None, timeout_s: float = 60.0
    ) -> CuaReplResult:
        """Execute with cleanup covering startup, transport and callback failures."""
        try:
            return self._execute(code, title, timeout_s)
        except BaseException:
            self._emergency_reset()
            raise

    def _execute(
        self, code: str, title: str | None, timeout_s: float
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
        sel = selectors.DefaultSelector()
        sel.register(self._proc.stdout, selectors.EVENT_READ)

        try:
            while time.monotonic() < deadline:
                remaining = max(0.05, deadline - time.monotonic())
                events = sel.select(timeout=remaining)
                if not events:
                    break

                line = self._proc.stdout.readline()
                if not line:
                    self._proc.wait(timeout=2)
                    assert self._proc.stderr is not None
                    diagnostic = self._proc.stderr.read()
                    error = f"CUA bridge exited with status {self._proc.returncode}: {diagnostic}"
                    self._emergency_reset()
                    return CuaReplResult(
                        status="failed", duration_ms=int((time.monotonic() - start_time) * 1000),
                        content="", error=error,
                    )
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_id = msg.get("id")

                # Check if this is the final response to our eval request
                if msg_id == eval_id:
                    duration_ms = int((time.monotonic() - start_time) * 1000)
                    if "error" in msg:
                        self._release_inputs()
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
                    self._release_inputs()
                    LOGGER.exception("Error executing bridge RPC", extra={"method": method})
                    resp = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "error": {"code": -32603, "message": str(exc)},
                        }
                    )

                self._proc.stdin.write(resp + "\n")
                self._proc.stdin.flush()
        finally:
            sel.close()

        self._emergency_reset()
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

    def _emergency_reset(self) -> None:
        """Reset input states and recycle process on hang or timeout."""
        # Stop queued JS first. Escape is not a release and can dismiss a
        # user's dialog; only explicit hardware-up events are safe here.
        try:
            self.stop()
        finally:
            self._release_inputs()

    def _release_inputs(self) -> None:
        """Release held hardware through the driver's independent cleanup RPC."""
        if self.driver_client is not None:
            self.driver_client.release_inputs()

    def _select_menu_item(self, app_name: str, path: list[str]) -> bool:
        """Select a macOS menu bar item natively via AppleScript System Events."""
        if not path:
            raise ValueError("selectMenuItem requires a nonempty menu path")
        # Fixed source, all app/menu strings are argv data, including quotes,
        # backslashes and newlines. The root is a menu BAR item, not a menu item.
        script = """on run argv
    set appName to item 1 of argv
    set menuName to item 2 of argv
    tell application "System Events"
        tell process appName
            set targetControl to menu bar item menuName of menu bar 1
            repeat with idx from 3 to count of argv
                set itemName to item idx of argv
                set targetControl to menu item itemName of menu 1 of targetControl
            end repeat
            click targetControl
        end tell
    end tell
end run"""
        try:
            proc = subprocess.run(
                ["osascript", "-e", script, "--", app_name, *path],
                capture_output=True, text=True, timeout=3.0, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # A timeout here is almost never a slow menu. System Events needs
            # its own Automation consent — a different grant from the
            # Accessibility one the driver already holds — and without it
            # osascript blocks on a TCC prompt nobody is there to answer.
            # Measured on this host: the call hung for the full timeout and
            # said nothing about why, which reads as "that menu is not there"
            # and sends the model hunting a menu that is sitting right in
            # front of it.
            raise RuntimeError(
                f"selectMenuItem timed out: app={app_name!r}, path={path!r}. "
                "System Events Automation consent is probably not granted "
                "(System Settings > Privacy & Security > Automation)"
            ) from exc
        if proc.returncode != 0:
            raise RuntimeError(
                f"selectMenuItem failed: app={app_name!r}, path={path!r}, "
                f"status={proc.returncode}, stderr={proc.stderr!r}, stdout={proc.stdout!r}"
            )
        return True

    def _crop_data_uri(self, region: Rect, *, padding: float) -> str:
        """A base64 PNG data URI of one region of the live screen.

        The point of this is what it does *not* send. A full Retina frame costs
        the model roughly 2000 tokens per look; the region around a single
        control is two orders of magnitude smaller, and for the question
        visual confirmation actually asks — "did this button change?" — the
        rest of the screen is noise the model still pays for.

        ``region`` is in global logical points, the space AX bounds and actions
        already use, and is converted through
        :func:`logical_region_to_pixels` — the same conversion the ORIENT diff
        uses, so the rectangle a witness measures and the rectangle a model is
        shown are one rectangle.

        ``padding`` widens the region on every side. A control cropped exactly
        to its own bounds shows the control and nothing it changed *about* its
        surroundings — a checkbox's label, a button's focus ring — so a caller
        that wants context asks for it in points rather than doing rectangle
        arithmetic in JavaScript.
        """
        if region.size.width <= 0 or region.size.height <= 0:
            raise ValueError(
                f"crop region must have positive size, got "
                f"{region.size.width}x{region.size.height}"
            )
        capture = self._capture_screen()
        padded = Rect(
            origin=Point(region.origin.x - padding, region.origin.y - padding),
            size=Size(
                region.size.width + padding * 2, region.size.height + padding * 2
            ),
        )
        cropped = crop_capture(capture, logical_region_to_pixels(capture, padded))
        if cropped.width == 0 or cropped.height == 0:
            raise ScreenCaptureUnavailableError(
                f"crop region {padded.origin.x:.0f},{padded.origin.y:.0f} "
                f"{padded.size.width:.0f}x{padded.size.height:.0f} does not "
                "overlap the captured display"
            )
        return f"data:image/png;base64,{capture_to_base64_png(cropped)}"

    def _capture_screen(self) -> ScreenCapture:
        """One frame of the live screen, or a typed refusal (never a blank).

        Shared by ``getScreenshot`` and the crops so both fail the same way.
        """
        if self.driver_client is None:
            raise ScreenCaptureUnavailableError(
                "cannot capture the screen: the engine has no driver client"
            )
        try:
            if hasattr(self.driver_client, "capture"):
                capture = self.driver_client.capture()
            else:
                capture = self.driver_client.screenshot()
        except Exception as exc:
            raise ScreenCaptureUnavailableError(
                f"cannot capture the screen: {exc}"
            ) from exc
        if not capture.data:
            raise ScreenCaptureUnavailableError(
                "cannot capture the screen: the driver returned no image data"
            )
        return cast(ScreenCapture, capture)

    def _frontmost_is(self, app_name: str) -> bool:
        """Is ``app_name`` the application that owns the front window right now?

        Asks the host rather than a cached belief, and judges the answer with
        :func:`app_evidence` — the same matcher the verification layer uses, so
        the engine and the OODA loop agree about when two names mean one app.
        That matters here: LaunchServices says "Google Chrome" where the
        accessibility API says "Chrome", and on a localized desktop neither
        matches the English name the script wrote, which is why the bundle id
        is passed alongside.
        """
        name, bundle_id = self._frontmost_identity()
        if app_evidence(app_name, name, bundle_id) is Evidence.CONFIRMED:
            return True
        # A generic dialog role/title is NOT proof of ownership. The driver
        # must trace its actual AX parent chain to the requested process.
        if not hasattr(self.driver_client, "owns_focused_modal"):
            return False
        pid = self.driver_client.app_pid(app_name)
        return isinstance(pid, int) and self.driver_client.owns_focused_modal(pid) is True

    def _frontmost_identity(self) -> tuple[str | None, str]:
        """The frontmost application's name and bundle id, in either shape.

        ``ActuationClient.focused_window`` answers with a validated
        :class:`FocusedWindow`; a mapping is accepted for the same reason
        :meth:`_get_app_snapshot` accepts one — the engine is handed whatever
        driver-like object its caller has. Both identities travel together
        because either alone can be wrong about which app is in front: the name
        is localized per desktop, the bundle id is empty for hosts without one.
        """
        focused: Any = self.driver_client.focused_window()
        if isinstance(focused, dict):
            entry = cast(dict[str, object], focused)
            raw_name = entry.get("app_name") or entry.get("app")
            raw_bundle = entry.get("bundle_id")
            return (
                str(raw_name) if raw_name is not None else None,
                str(raw_bundle) if raw_bundle is not None else "",
            )
        return focused.app_name, focused.bundle_id

    def _ensure_app_active(self, app_name: str) -> None:
        """Put the target application in front, and confirm that it got there.

        This used to trust a cache: an app activated once was assumed frontmost
        for the rest of the engine's life, and a failed activation was logged
        at warning level while the caller actuated anyway. Both halves are
        unsafe on a physical host, because a keystroke goes to whoever holds
        focus when it is posted, not to the application the script named.

        Measured live on macOS (real backend): activate TextEdit, let another
        application take the front, then call this again — it returned having
        done nothing, with Notes frontmost. The very next line of the audit
        script is ``pressKey("Cmd+A")`` followed by ``pressKey("Delete")``,
        which would have selected and deleted the contents of the user's Notes.
        None of this is observable against the simulated backend, where the
        frontmost window is a fixture and every activation succeeds.

        So: observe, act, then observe again — and raise rather than actuate
        when the target cannot be confirmed in front.
        """
        if self.driver_client is None:
            return
        if (hasattr(self.driver_client, "hotkey_state")
                and self.driver_client.hotkey_state() is True):
            raise PermissionDeniedError("CUA stopped: the user triggered the emergency kill-switch")
        if self._frontmost_is(app_name):
            return
        try:
            self.driver_client.activate_app(app_name)
        except Exception as exc:
            raise FocusNotAcquiredError(
                f"cannot actuate in {app_name!r}: activating it failed ({exc})"
            ) from exc
        for _ in range(FOCUS_SETTLE_POLLS):
            time.sleep(FOCUS_SETTLE_INTERVAL_S)
            if self._frontmost_is(app_name):
                return
        raise FocusNotAcquiredError(
            f"cannot actuate in {app_name!r}: it did not come to the front "
            f"within {FOCUS_SETTLE_POLLS * FOCUS_SETTLE_INTERVAL_S:.2f}s; "
            "the keystroke would have gone to whatever is in front instead"
        )

    def _get_app_snapshot(self, app_name: str) -> tuple[AXElement, str]:
        """Fetch accessibility snapshot and window title for an app."""
        if self.snapshot_provider:
            return self.snapshot_provider(app_name)

        if self.driver_client:
            # Best-effort, and deliberately *not* routed through
            # ``_ensure_app_active``. Reading an accessibility tree is
            # perception, not actuation: the pid lookup below reaches the app
            # wherever it is, so an app that stays behind another window is
            # still readable — and background mode depends on exactly that.
            # A keystroke is the opposite case, which is why only the actuating
            # callers insist on confirmed focus.
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
                    snap_raw: Any = self.driver_client.ax_snapshot(pid=pid)
                    if isinstance(snap_raw, (tuple, list)) and len(cast(tuple[object, ...], snap_raw)) == 2:
                        raw_snap, raw_title = cast(tuple[object, object], snap_raw)
                        return cast(AXElement, raw_snap), str(raw_title or app_name)
                    snap: AXElement = cast(AXElement, snap_raw)
                    win: Any = self.driver_client.focused_window()
                    if isinstance(win, dict):
                        win_dict = cast(dict[str, object], win)
                        title = str(
                            win_dict.get("title")
                            or win_dict.get("window_title")
                            or win_dict.get("app")
                            or win_dict.get("app_name")
                            or app_name
                        )
                    elif win is not None:
                        title = str(
                            getattr(win, "window_title", None)
                            or getattr(win, "title", None)
                            or getattr(win, "app_name", None)
                            or app_name
                        )
                    else:
                        title = app_name
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
        elem_index: int | None = None,
        x: int | None = None,
        y: int | None = None,
        query: str | None = None,
        role: str | None = None,
        title: str | None = None,
    ) -> tuple[int | None, int | None, str | None]:
        """Resolve element coordinates and target label with self-healing and smart locators."""
        tracker = self._get_tracker(app_name)

        if elem_index is not None:
            idx = int(elem_index)
            expected = tracker.get_historical_element(idx)
            snap, win_title = self._get_app_snapshot(app_name)
            tracker.refresh_state(snap, win_title)
            self._ensure_app_active(app_name)
            elem = tracker.get_element_by_index(idx)
            if elem:
                if expected is None or (elem.role, elem.title) != (expected.role, expected.title):
                    raise ValueError(f"Element [{idx}] identity changed; re-observe {app_name!r}")
                if role is not None and elem.role.removeprefix("AX") != role.removeprefix("AX"):
                    raise ValueError(f"Element [{idx}] does not match expected role {role!r}")
                if title is not None and elem.title != title:
                    raise ValueError(f"Element [{idx}] does not match expected title {title!r}")
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
                snap, win_title = self._get_app_snapshot(app_name)
                tracker.refresh_state(snap, win_title)
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

        # Smart semantic locator: Match by query, title, or role
        if query is not None or title is not None or role is not None:
            snap, win_title = self._get_app_snapshot(app_name)
            tracker.refresh_state(snap, win_title)
            self._ensure_app_active(app_name)
            matches = tracker.find_elements(role=role, title=title, query=query)
            if len(matches) > 1:
                raise ValueError(
                    f"Ambiguous locator in {app_name!r}: query={query!r}, "
                    f"title={title!r}, role={role!r}; use a freshly observed index"
                )
            matched = matches[0] if matches else None

            if matched:
                LOGGER.info(
                    "Smart locator matched element [%d] ('%s' %s) at (%d, %d)",
                    matched.index,
                    matched.title,
                    matched.role,
                    matched.centre_x,
                    matched.centre_y,
                )
                return matched.centre_x, matched.centre_y, matched.title or matched.role

            raise ValueError(
                f"Could not find element matching query={query!r}, title={title!r}, role={role!r} in '{app_name}'."
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
        if (self.grant_store is not None
                and decision == PermissionDecision.CONFIRM
                and risk == Risk.DESTRUCTIVE
                and self.autonomy_level in (AutonomyLevel.GUARDED, AutonomyLevel.FULL)):
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

        if method == "selectMenuItem":
            app_name = str(params["app"])
            path_segments = cast(list[str], params.get("path", []))
            self._ensure_app_active(app_name)
            if not path_segments:
                raise ValueError("selectMenuItem requires a nonempty menu path")
            self._check_security(
                MouseClick(type="mouse_click", x=0, y=0, button="left", click_count=1),
                app_name, " > ".join(path_segments),
            )
            success = self._select_menu_item(app_name, path_segments)
            return {"success": success, "path": path_segments}

        if method == "findVisualElement":
            app_name = str(params["app"])
            query = str(params.get("query", "")).strip().casefold()
            pid: int | None = self.driver_client.app_pid(app_name) if self.driver_client else None

            if self.driver_client and hasattr(self.driver_client, "recognize_text"):
                try:
                    # Every argument is required and the pid one is called
                    # ``window_pid``. Calling it ``pid=`` raised TypeError on
                    # every single invocation, the broad handler below logged
                    # it and answered ``None``, and the OCR fallback therefore
                    # reported "nothing on screen matches" for two releases
                    # without ever having looked. Verified live: the warning
                    # was "got an unexpected keyword argument 'pid'".
                    lines = cast(
                        list[Any],
                        self.driver_client.recognize_text(
                            display_id=0,
                            window_pid=pid,
                            min_confidence=OCR_MIN_CONFIDENCE,
                            max_lines=OCR_MAX_LINES,
                        ),
                    )
                    for line in lines:
                        text_val = str(getattr(line, "text", ""))
                        if query in text_val.casefold():
                            lx = int(getattr(line, "x", 0))
                            ly = int(getattr(line, "y", 0))
                            lw = int(getattr(line, "width", 0))
                            lh = int(getattr(line, "height", 0))
                            res_box: dict[str, object] = {
                                "text": text_val,
                                "x": lx,
                                "y": ly,
                                "width": lw,
                                "height": lh,
                                "centre_x": lx + lw // 2,
                                "centre_y": ly + lh // 2,
                                "confidence": float(getattr(line, "confidence", 1.0)),
                            }
                            return res_box
                except TypeError:
                    # A wrong call is a coding error, not "no text found".
                    # Swallowing it is what kept the bug above alive.
                    raise
                except Exception as exc:  # noqa: BLE001 - a fallback may not raise
                    LOGGER.warning("findVisualElement failed for %s: %s", app_name, exc)
            return None

        if method == "getWindowBounds":
            app_name = str(params["app"])
            pid_val: int | None = self.driver_client.app_pid(app_name) if self.driver_client else None

            # Read off the accessibility tree, which carries every element's
            # rect — including the window's. The previous implementation called
            # ``focused_window(pid=...)``, which takes no arguments at all, so
            # it raised on every call, logged at debug level, and returned a
            # hardcoded 800x600 window at the origin. Measured live on a
            # 1710x1112 display: TextEdit came back as 800x600 at (0,0), which
            # is not a degraded answer but an invented one — and a model
            # deriving click coordinates from it would aim at nothing.
            snapshot, window_title = self._get_app_snapshot(app_name)
            window = _first_window(snapshot)
            if window is None:
                raise WindowBoundsUnavailableError(
                    f"cannot read {app_name!r}'s window geometry: its "
                    "accessibility tree exposes no window element"
                )
            return {
                "title": window.title or window_title,
                "x": int(window.x),
                "y": int(window.y),
                "width": int(window.width),
                "height": int(window.height),
                "pid": pid_val,
            }

        if method == "cropScreenshot":
            raw_bounds = cast(dict[str, float], params.get("bounds", {}))
            return self._crop_data_uri(
                Rect(
                    origin=Point(
                        float(raw_bounds.get("x", 0)), float(raw_bounds.get("y", 0))
                    ),
                    size=Size(
                        float(raw_bounds.get("width", 0)),
                        float(raw_bounds.get("height", 0)),
                    ),
                ),
                padding=float(cast(float, params.get("padding", 0))),
            )

        if method == "findElement":
            app_name = str(params["app"])
            tracker = self._get_tracker(app_name)
            elem = tracker.find_element(
                role=cast(str | None, params.get("role")),
                title=cast(str | None, params.get("title")),
                query=cast(str | None, params.get("query")),
            )
            if not elem:
                snap, win_title = self._get_app_snapshot(app_name)
                tracker.refresh_state(snap, win_title)
                elem = tracker.find_element(
                    role=cast(str | None, params.get("role")),
                    title=cast(str | None, params.get("title")),
                    query=cast(str | None, params.get("query")),
                )
            return elem.to_dict() if elem else None

        if method == "findAllElements":
            app_name = str(params["app"])
            tracker = self._get_tracker(app_name)
            elems = tracker.find_elements(
                role=cast(str | None, params.get("role")),
                title=cast(str | None, params.get("title")),
                query=cast(str | None, params.get("query")),
            )
            if not elems:
                snap, win_title = self._get_app_snapshot(app_name)
                tracker.refresh_state(snap, win_title)
                elems = tracker.find_elements(
                    role=cast(str | None, params.get("role")),
                    title=cast(str | None, params.get("title")),
                    query=cast(str | None, params.get("query")),
                )
            return [e.to_dict() for e in elems]

        if method == "click":
            app_name = str(params["app"])
            self._ensure_app_active(app_name)
            target_x, target_y, target_label = self._resolve_target_point(
                app_name,
                elem_index=cast(int | None, params.get("elementIndex")),
                x=cast(int | None, params.get("x")),
                y=cast(int | None, params.get("y")),
                query=cast(str | None, params.get("query")),
                role=cast(str | None, params.get("role")),
                title=cast(str | None, params.get("title")),
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
            app_name = str(params["app"])
            self._ensure_app_active(app_name)
            target_x, target_y, _ = self._resolve_target_point(
                app_name,
                elem_index=cast(int | None, params.get("elementIndex")),
                x=cast(int | None, params.get("x")),
                y=cast(int | None, params.get("y")),
                query=cast(str | None, params.get("query")),
                role=cast(str | None, params.get("role")),
                title=cast(str | None, params.get("title")),
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
            capture = self._capture_screen()
            return f"data:image/png;base64,{capture_to_base64_png(capture)}"

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
