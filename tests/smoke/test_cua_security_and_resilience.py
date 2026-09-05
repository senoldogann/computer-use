"""Tests for CUA REPL constitutional security gates, grants, and self-healing resilience."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from computeruse.orchestrator.schemas import Action, MouseClick
from computeruse.repl.engine import CuaReplEngine
from computeruse.security.autonomy import AutonomyLevel
from computeruse.security.grants import CapabilityGrant, GrantStore
from computeruse.vision.ax import AXElement


class MockDriverClient:
    """Mock client capturing driver calls and app activations."""

    def __init__(self) -> None:
        self.sent_actions: list[Action] = []
        self.activated_apps: list[str] = []

    def send(self, action: Action) -> None:
        self.sent_actions.append(action)

    def activate_app(self, app_name: str) -> None:
        self.activated_apps.append(app_name)

    def list_apps(self) -> list[str]:
        return ["TextEdit", "Finder"]

    def screenshot(self) -> Any:
        class FakeCap:
            data = b"fake_png_data"

        return FakeCap()


def test_cua_repl_security_blocks_destructive_click_without_grant() -> None:
    """Destructive action (Move to Trash) must be blocked under GUARDED autonomy."""
    driver = MockDriverClient()

    def snapshot_provider(app_name: str) -> tuple[AXElement, str]:
        root = AXElement(
            role="Window",
            title="Trash Window",
            width=500,
            height=400,
            children=(
                AXElement(
                    role="Button",
                    title="Move to Trash",
                    x=100,
                    y=100,
                    width=120,
                    height=30,
                ),
            ),
        )
        return root, "Trash Window"

    engine = CuaReplEngine(
        driver_client=driver,
        snapshot_provider=snapshot_provider,
        autonomy_level=AutonomyLevel.GUARDED,
    )

    try:
        code = """
        var app = await cua.getApp("Finder");
        await app.click(1);
        """
        res = engine.execute(code, title="Dosyayı çöpe at")
        assert res.status == "failed"
        assert res.error is not None
        assert "Security Refusal" in res.error
        assert "DESTRUCTIVE" in res.error
        assert len(driver.sent_actions) == 0
    finally:
        engine.stop()


def test_cua_repl_security_blocks_dangerous_typed_commands() -> None:
    """Typing rm -rf must be blocked as destructive under GUARDED autonomy."""
    driver = MockDriverClient()
    engine = CuaReplEngine(
        driver_client=driver,
        autonomy_level=AutonomyLevel.GUARDED,
    )

    try:
        code = """
        var app = await cua.getApp("Terminal");
        await app.typeText("rm -rf /tmp/important_data");
        """
        res = engine.execute(code, title="Komut çalıştır")
        assert res.status == "failed"
        assert res.error is not None
        assert "Security Refusal" in res.error
        assert len(driver.sent_actions) == 0
    finally:
        engine.stop()


def test_cua_repl_security_permits_destructive_action_with_valid_grant(
    tmp_path: Path,
) -> None:
    """When a valid capability grant exists, the destructive action runs and consumes 1 use."""
    driver = MockDriverClient()
    grant_store = GrantStore(tmp_path / "grants")
    now = datetime.now(UTC)

    # Save a grant allowing 'delete' on 'Finder' for 'Move to Trash'
    grant = CapabilityGrant(
        grant_id="grant.finder.trash",
        verb="delete",
        app="Finder",
        target_pattern="Move to Trash",
        max_invocations=1,
        used=0,
        expires_at=now + timedelta(hours=1),
        created_at=now,
        note="Allow moving temporary test artifacts to trash",
    )
    grant_store.save(grant)

    def snapshot_provider(app_name: str) -> tuple[AXElement, str]:
        root = AXElement(
            role="Window",
            title="Finder Window",
            width=500,
            height=400,
            children=(
                AXElement(
                    role="Button",
                    title="Move to Trash",
                    x=100,
                    y=100,
                    width=120,
                    height=30,
                ),
            ),
        )
        return root, "Finder Window"

    engine = CuaReplEngine(
        driver_client=driver,
        snapshot_provider=snapshot_provider,
        autonomy_level=AutonomyLevel.GUARDED,
        grant_store=grant_store,
        now_provider=lambda: now,
    )

    try:
        code = """
        var app = await cua.getApp("Finder");
        await app.click(1);
        """
        # First execution should succeed under grant
        res = engine.execute(code, title="Çöpe at (izinli)")
        assert res.status == "completed"
        assert any(isinstance(a, MouseClick) for a in driver.sent_actions)

        # Verify grant was consumed
        stored = next(g for g in grant_store.grants() if g.grant_id == "grant.finder.trash")
        assert stored.used == 1
        assert stored.remaining == 0

        # Second execution should now be refused because grant invocations are exhausted
        driver.sent_actions.clear()
        res_second = engine.execute(code, title="Çöpe at (tükenmiş izin)")
        assert res_second.status == "failed"
        assert res_second.error is not None
        assert "Security Refusal" in res_second.error
        assert "exhausted" in res_second.error or "spent" in res_second.error
        assert len(driver.sent_actions) == 0
    finally:
        engine.stop()


def test_cua_repl_self_healing_stale_locators() -> None:
    """When UI mutates and shifts index, CuaReplEngine self-heals by matching role and title."""
    driver = MockDriverClient()
    version = 1

    def snapshot_provider(app_name: str) -> tuple[AXElement, str]:
        nonlocal version
        if version == 1:
            # Initial layout: Submit button is index 1
            root = AXElement(
                role="Window",
                title="Form",
                width=600,
                height=400,
                children=(
                    AXElement(
                        role="Button",
                        title="Submit",
                        x=100,
                        y=100,
                        width=80,
                        height=30,
                    ),
                ),
            )
            return root, "Form"

        # Mutated layout: A new banner appeared above, pushing Submit to index 2 and y=250
        root = AXElement(
            role="Window",
            title="Form",
            width=600,
            height=400,
            children=(
                AXElement(
                    role="StaticText",
                    title="Important Notice Banner",
                    x=50,
                    y=50,
                    width=400,
                    height=50,
                ),
                AXElement(
                    role="Button",
                    title="Submit",
                    x=100,
                    y=250,
                    width=80,
                    height=30,
                ),
            ),
        )
        return root, "Form"

    engine = CuaReplEngine(
        driver_client=driver,
        snapshot_provider=snapshot_provider,
    )

    try:
        # Step 1: Initialize app (records historical Submit at index 1, y=100)
        res1 = engine.execute('var app = await cua.getApp("WebForm");')
        assert res1.status == "completed"
        assert '[1] Button "Submit"' in res1.content

        # Background mutation happens: layout changes
        version = 2
        # We also manually invalidate current index 1 to simulate stale index
        engine.trackers["WebForm"].current_index_map.clear()

        # Step 2: Code still calls click(1) using the old index
        res2 = engine.execute(
            'var app = await cua.getApp("WebForm"); await app.click(1);'
        )
        assert res2.status == "completed"

        # The driver should have clicked the newly healed Submit button at y=250 (centre_y=265)!
        click = next(a for a in driver.sent_actions if isinstance(a, MouseClick))
        assert click.y == 265
    finally:
        engine.stop()


def test_cua_repl_cross_app_focus_and_state_isolation() -> None:
    """Interacting with multiple apps in one JS block ensures focus activation and isolated trackers."""
    driver = MockDriverClient()

    def snapshot_provider(app_name: str) -> tuple[AXElement, str]:
        return AXElement(role="Window", title=app_name, width=600, height=400), app_name

    engine = CuaReplEngine(
        driver_client=driver,
        snapshot_provider=snapshot_provider,
    )

    try:
        code = """
        var te = await cua.getApp("TextEdit");
        var finder = await cua.getApp("Finder");
        await te.typeText("TextEdit document content");
        await finder.click([200, 200]);
        await te.typeText("More TextEdit content");
        """
        res = engine.execute(code)
        assert res.status == "completed"

        # Verify both apps were activated in appropriate order
        assert "TextEdit" in driver.activated_apps
        assert "Finder" in driver.activated_apps

        # Verify state trackers are isolated
        assert "TextEdit" in engine.trackers
        assert "Finder" in engine.trackers
        assert engine.trackers["TextEdit"].app_name == "TextEdit"
        assert engine.trackers["Finder"].app_name == "Finder"
    finally:
        engine.stop()
