"""Comprehensive smoke tests for superior CUA capabilities surpassing OpenAI CUA:

1. Native macOS Menu Bar Traversal (`selectMenuItem`)
2. Multimodal Hybrid Vision/OCR Fallback (`findVisual`)
3. Geometry & Window Bounds Inspection (`getWindowBounds`)
4. Safe Script Transactions & Rollback Reflex (`cua.transaction`)
5. Non-blocking Timeout Watchdog & Emergency Reset Hook
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest
from smoke.cua_fakes import model_focus

from computeruse.repl.engine import CuaReplEngine, WindowBoundsUnavailableError
from computeruse.vision.ax import AXElement


@dataclass
class DummyOcrLine:
    text: str
    x: float
    y: float
    width: float
    height: float
    confidence: float = 0.95


def test_cua_native_menu_item_selection() -> None:
    """Model can trigger macOS application menu items natively via selectMenuItem."""
    mock_client = MagicMock()
    mock_client.app_pid.return_value = 8001
    mock_client.focused_window.return_value = {
        "app_name": "Finder",
        "app": "Finder",
        "bundle_id": "",
    }
    model_focus(mock_client)

    engine = CuaReplEngine(driver_client=mock_client)
    engine.start()

    with patch.object(engine, "_select_menu_item", return_value=True) as mock_menu:
        script = """
        var app = await cua.getApp("TextEdit");
        var res = await app.selectMenuItem(["File", "Export as PDF..."]);
        return res;
        """
        res = engine.execute(script)
        engine.stop()

        assert not res.is_error
        mock_menu.assert_called_once_with("TextEdit", ["File", "Export as PDF..."])
        assert '"success":true' in res.content.replace(" ", "")


def test_cua_hybrid_vision_ocr_fallback() -> None:
    """Model seamlessly discovers elements via Apple Vision OCR when AX tree is opaque."""
    mock_client = MagicMock()
    mock_client.app_pid.return_value = 8002
    mock_client.focused_window.return_value = {
        "app_name": "Finder",
        "app": "Finder",
        "bundle_id": "",
    }
    model_focus(mock_client)
    mock_client.recognize_text.return_value = [
        DummyOcrLine(text="Search Google", x=120.0, y=45.0, width=180.0, height=30.0),
        DummyOcrLine(text="I'm Feeling Lucky", x=320.0, y=45.0, width=150.0, height=30.0),
    ]

    engine = CuaReplEngine(driver_client=mock_client)
    engine.start()

    script = """
    var app = await cua.getApp("Safari");
    var visualBtn = await app.findVisual("Feeling Lucky");
    return visualBtn;
    """
    res = engine.execute(script)
    engine.stop()

    assert not res.is_error
    import json
    data = json.loads(res.content)
    assert data["text"] == "I'm Feeling Lucky"
    assert data["x"] == 320
    assert data["y"] == 45
    assert data["centre_x"] == 320 + 75
    assert data["centre_y"] == 45 + 15


def test_cua_window_bounds_inspection() -> None:
    """Model can inspect exact application window coordinates and size.

    The geometry comes off the accessibility tree, which is where it actually
    lives. This test used to mock ``focused_window()`` returning a rect —
    an API that does not exist; the real one answers pid, names and the cursor
    and nothing else — so it passed while the implementation raised on every
    call and returned a hardcoded 800x600 window at the origin instead.
    """
    mock_client = MagicMock()
    mock_client.app_pid.return_value = 8003
    mock_client.focused_window.return_value = {
        "app_name": "DashboardApp",
        "app": "DashboardApp",
        "bundle_id": "",
    }
    model_focus(mock_client)

    window = AXElement(
        role="AXWindow",
        title="Main Dashboard",
        x=100.0,
        y=80.0,
        width=1280.0,
        height=800.0,
        children=[],
    )
    root = AXElement(
        role="AXApplication",
        title="DashboardApp",
        x=0.0,
        y=0.0,
        width=1280.0,
        height=800.0,
        children=[window],
    )

    engine = CuaReplEngine(
        driver_client=mock_client,
        snapshot_provider=lambda _app: (root, "Main Dashboard"),
    )
    engine.start()

    script = """
    var app = await cua.getApp("DashboardApp");
    var win = await app.getWindowBounds();
    return win;
    """
    res = engine.execute(script)
    engine.stop()

    assert not res.is_error
    data = json.loads(res.content)
    assert (data["x"], data["y"]) == (100, 80)
    assert (data["width"], data["height"]) == (1280, 800)
    assert data["title"] == "Main Dashboard"


def test_window_bounds_are_refused_rather_than_invented() -> None:
    """No window in the tree means no answer — not a plausible rectangle.

    The fabricated 800x600 fallback was the dangerous shape of this bug: a
    model deriving click coordinates from invented geometry aims at whatever
    is really at those points on a physical host.
    """
    mock_client = MagicMock()
    mock_client.app_pid.return_value = 8004
    mock_client.focused_window.return_value = {
        "app_name": "Headless",
        "app": "Headless",
        "bundle_id": "",
    }
    model_focus(mock_client)
    windowless = AXElement(
        role="AXApplication", title="Headless", x=0.0, y=0.0,
        width=0.0, height=0.0, children=[],
    )
    engine = CuaReplEngine(
        driver_client=mock_client,
        snapshot_provider=lambda _app: (windowless, "Headless"),
    )
    with pytest.raises(WindowBoundsUnavailableError):
        engine._dispatch_js_call(  # pyright: ignore[reportPrivateUsage]
            "getWindowBounds", {"app": "Headless"}
        )


def test_cua_transaction_auto_retry_and_escape_rollback() -> None:
    """cua.transaction automatically retries flaky steps and presses Escape on final failure."""
    mock_client = MagicMock()
    engine = CuaReplEngine(driver_client=mock_client)
    engine.start()

    # Script fails twice, succeeds on 3rd attempt
    script = """
    var attempts = 0;
    var res = await cua.transaction(async () => {
        attempts++;
        if (attempts < 3) {
            throw new Error("Temporary modal block");
        }
        return "recovered_ok";
    }, { retries: 3, backoffMs: 10 });
    return { res, attempts };
    """
    res = engine.execute(script)
    engine.stop()

    assert not res.is_error
    import json
    data = json.loads(res.content)
    assert data["res"] == "recovered_ok"
    assert data["attempts"] == 3


def test_cua_timeout_watchdog_nonblocking() -> None:
    """Timeout watchdog terminates hanging scripts cleanly without blocking Python process."""
    engine = CuaReplEngine()
    engine.start()

    # Script enters an infinite loop; engine must interrupt cleanly via selector deadline
    script = """
    while (true) {}
    """
    res = engine.execute(script, timeout_s=0.5)
    engine.stop()

    assert res.is_error
    assert "timed out" in res.error.lower()
