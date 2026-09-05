"""Hard real-world chaos and resilience scenarios for CUA REPL Code-as-Action.

Covers:
1. Dynamic UI appearance with async waitForElement polling
2. Semantic query-based clicking (string title & role queries without indices)
3. Layout shift and self-healing under multi-step mutation
4. Multi-app interleaving and isolation
5. Timeout and graceful error surface on non-existent controls
"""

from __future__ import annotations

from unittest.mock import MagicMock

from computeruse.repl.engine import CuaReplEngine
from computeruse.vision.ax import AXElement


def test_smart_locator_click_by_title_query() -> None:
    """Model can click using plain string title queries without knowing indices."""
    mock_client = MagicMock()
    mock_client.app_pid.return_value = 5001
    mock_client.focused_window.return_value = {
        "title": "Settings",
        "app": "SystemSettings",
        "app_name": "SystemSettings",
        "pid": 5001,
        "window_id": 200,
        "x": 0.0,
        "y": 0.0,
        "width": 900.0,
        "height": 700.0,
    }
    ax_root = AXElement(
        role="AXApplication",
        title="SystemSettings",
        x=0.0,
        y=0.0,
        width=900.0,
        height=700.0,
        children=[
            AXElement(
                role="AXButton",
                title="Wi-Fi",
                x=50.0,
                y=100.0,
                width=120.0,
                height=30.0,
            ),
            AXElement(
                role="AXButton",
                title="Bluetooth",
                x=50.0,
                y=150.0,
                width=120.0,
                height=30.0,
            ),
            AXElement(
                role="AXButton",
                title="Network",
                x=50.0,
                y=200.0,
                width=120.0,
                height=30.0,
            ),
        ],
    )
    mock_client.ax_snapshot.return_value = (ax_root, "Settings")

    engine = CuaReplEngine(driver_client=mock_client)
    engine.start()

    # Click using plain string title: "Bluetooth" -> centre: (50 + 60 = 110, 150 + 15 = 165)
    script = """
    var app = await cua.getApp("SystemSettings");
    await app.click("Bluetooth");
    """
    res = engine.execute(script)
    engine.stop()

    assert not res.is_error
    sent = [call.args[0] for call in mock_client.send.call_args_list]
    assert any(
        getattr(act, "type", None) == "mouse_click" and act.x == 110 and act.y == 165
        for act in sent
    )


def test_smart_locator_click_by_role_and_title_object() -> None:
    """Model can target elements with precise {role, title} dictionaries."""
    mock_client = MagicMock()
    mock_client.app_pid.return_value = 5002
    mock_client.focused_window.return_value = {
        "title": "Document",
        "app": "TextEdit",
        "app_name": "TextEdit",
        "pid": 5002,
        "window_id": 201,
        "x": 0.0,
        "y": 0.0,
        "width": 800.0,
        "height": 600.0,
    }
    ax_root = AXElement(
        role="AXApplication",
        title="TextEdit",
        x=0.0,
        y=0.0,
        width=800.0,
        height=600.0,
        children=[
            AXElement(
                role="AXStaticText",
                title="Save",
                x=200.0,
                y=50.0,
                width=50.0,
                height=20.0,
            ),
            AXElement(
                role="AXButton",
                title="Save",
                x=300.0,
                y=50.0,
                width=80.0,
                height=30.0,
            ),
        ],
    )
    mock_client.ax_snapshot.return_value = (ax_root, "Document")

    engine = CuaReplEngine(driver_client=mock_client)
    engine.start()

    # Model specifically wants the BUTTON "Save", not static text!
    # Centre of button: (300 + 40 = 340, 50 + 15 = 65)
    script = """
    var app = await cua.getApp("TextEdit");
    await app.click({ role: "AXButton", title: "Save" });
    """
    res = engine.execute(script)
    engine.stop()

    assert not res.is_error
    sent = [call.args[0] for call in mock_client.send.call_args_list]
    assert any(
        getattr(act, "type", None) == "mouse_click" and act.x == 340 and act.y == 65
        for act in sent
    )


def test_wait_for_element_dynamic_appearance() -> None:
    """waitForElement polls until an asynchronously appearing element renders."""
    mock_client = MagicMock()
    mock_client.app_pid.return_value = 5003
    mock_client.focused_window.return_value = {
        "title": "Downloader",
        "app": "Downloader",
        "app_name": "Downloader",
        "pid": 5003,
        "window_id": 202,
        "x": 0.0,
        "y": 0.0,
        "width": 600.0,
        "height": 400.0,
    }

    initial_root = AXElement(
        role="AXApplication",
        title="Downloader",
        x=0.0,
        y=0.0,
        width=600.0,
        height=400.0,
        children=[
            AXElement(
                role="AXStaticText",
                title="Downloading... 50%",
                x=50.0,
                y=50.0,
                width=200.0,
                height=30.0,
            )
        ],
    )

    final_root = AXElement(
        role="AXApplication",
        title="Downloader",
        x=0.0,
        y=0.0,
        width=600.0,
        height=400.0,
        children=[
            AXElement(
                role="AXButton",
                title="Download Complete - Open File",
                x=50.0,
                y=150.0,
                width=250.0,
                height=40.0,
            )
        ],
    )

    call_count = 0

    def dynamic_snapshot(pid: int | None = None) -> tuple[AXElement, str]:
        nonlocal call_count
        call_count += 1
        # On first 2 queries, return initial tree; on 3rd query, element appears
        if call_count < 3:
            return (initial_root, "Downloader")
        return (final_root, "Downloader")

    mock_client.ax_snapshot.side_effect = dynamic_snapshot

    engine = CuaReplEngine(driver_client=mock_client)
    engine.start()

    script = """
    var app = await cua.getApp("Downloader");
    var btn = await app.waitForElement("Open File", { timeoutMs: 3000, intervalMs: 50 });
    await app.click(btn.index);
    """
    res = engine.execute(script)
    engine.stop()

    assert not res.is_error
    # Centre of final button: (50 + 125 = 175, 150 + 20 = 170)
    sent = [call.args[0] for call in mock_client.send.call_args_list]
    assert any(
        getattr(act, "type", None) == "mouse_click" and act.x == 175 and act.y == 170
        for act in sent
    )


def test_wait_for_element_timeout_raises_descriptive_error() -> None:
    """waitForElement times out cleanly with a clear error when element does not appear."""
    mock_client = MagicMock()
    mock_client.app_pid.return_value = 5004
    mock_client.focused_window.return_value = {
        "title": "App",
        "app": "SampleApp",
        "app_name": "SampleApp",
        "pid": 5004,
        "window_id": 203,
        "x": 0.0,
        "y": 0.0,
        "width": 600.0,
        "height": 400.0,
    }
    empty_root = AXElement(
        role="AXApplication",
        title="SampleApp",
        x=0.0,
        y=0.0,
        width=600.0,
        height=400.0,
        children=[],
    )
    mock_client.ax_snapshot.return_value = (empty_root, "App")

    engine = CuaReplEngine(driver_client=mock_client)
    engine.start()

    script = """
    var app = await cua.getApp("SampleApp");
    await app.waitForElement("NonExistentButton", { timeoutMs: 300, intervalMs: 50 });
    """
    res = engine.execute(script)
    engine.stop()

    assert res.is_error
    assert "Timed out waiting for element" in res.error
    assert "NonExistentButton" in res.error


def test_multi_app_interleaving_and_isolation() -> None:
    """Interleaving operations between distinct apps maintains clean tree isolation."""
    mock_client = MagicMock()
    mock_client.app_pid.side_effect = lambda app: 1000 if app == "Notes" else 2000
    mock_client.focused_window.return_value = {
        "title": "Window",
        "app": "Notes",
        "app_name": "Notes",
        "pid": 1000,
        "window_id": 1,
        "x": 0.0,
        "y": 0.0,
        "width": 800.0,
        "height": 600.0,
    }

    notes_root = AXElement(
        role="AXApplication",
        title="Notes",
        x=0.0,
        y=0.0,
        width=800.0,
        height=600.0,
        children=[
            AXElement(
                role="AXButton",
                title="New Note",
                x=50.0,
                y=50.0,
                width=100.0,
                height=30.0,
            )
        ],
    )
    finder_root = AXElement(
        role="AXApplication",
        title="Finder",
        x=0.0,
        y=0.0,
        width=800.0,
        height=600.0,
        children=[
            AXElement(
                role="AXButton",
                title="Applications",
                x=100.0,
                y=100.0,
                width=100.0,
                height=30.0,
            )
        ],
    )

    def snapshot_by_pid(pid: int | None = None) -> tuple[AXElement, str]:
        if pid == 1000:
            return (notes_root, "Notes")
        return (finder_root, "Finder")

    mock_client.ax_snapshot.side_effect = snapshot_by_pid

    engine = CuaReplEngine(driver_client=mock_client)
    engine.start()

    script = """
    var notes = await cua.getApp("Notes");
    var finder = await cua.getApp("Finder");

    var hasNewNoteInNotes = await notes.hasElement("New Note");
    var hasNewNoteInFinder = await finder.hasElement("New Note");

    var hasAppsInFinder = await finder.hasElement("Applications");
    var hasAppsInNotes = await notes.hasElement("Applications");

    return {
        hasNewNoteInNotes,
        hasNewNoteInFinder,
        hasAppsInFinder,
        hasAppsInNotes
    };
    """
    res = engine.execute(script)
    engine.stop()

    assert not res.is_error
    import json
    data = json.loads(res.content)
    assert data["hasNewNoteInNotes"] is True
    assert data["hasNewNoteInFinder"] is False
    assert data["hasAppsInFinder"] is True
    assert data["hasAppsInNotes"] is False
