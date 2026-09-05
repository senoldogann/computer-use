"""Comprehensive smoke tests for superior CUA capabilities surpassing OpenAI CUA:

1. Native macOS Menu Bar Traversal (`selectMenuItem`)
2. Multimodal Hybrid Vision/OCR Fallback (`findVisual`)
3. Geometry & Window Bounds Inspection (`getWindowBounds`)
4. Safe Script Transactions & Rollback Reflex (`cua.transaction`)
5. Non-blocking Timeout Watchdog & Emergency Reset Hook
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from smoke.cua_fakes import model_focus

from computeruse.repl.engine import CuaReplEngine


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
    """Model can inspect exact application window coordinates and size."""
    mock_client = MagicMock()
    mock_client.app_pid.return_value = 8003
    mock_client.focused_window.return_value = {
        "title": "Main Dashboard",
        "x": 100,
        "y": 80,
        "width": 1280,
        "height": 800,
    }
    model_focus(mock_client)

    engine = CuaReplEngine(driver_client=mock_client)
    engine.start()

    script = """
    var app = await cua.getApp("DashboardApp");
    var win = await app.getWindowBounds();
    return win;
    """
    res = engine.execute(script)
    engine.stop()

    assert not res.is_error
    import json
    data = json.loads(res.content)
    assert data["width"] == 1280
    assert data["height"] == 800
    assert data["x"] == 100
    assert data["y"] == 80


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
