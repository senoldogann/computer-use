"""Adversarial bridge/driver boundary regressions, without touching user data."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator

import pytest

from computeruse.orchestrator.client import ActuationClient
from computeruse.orchestrator.loop import OodaRunner
from computeruse.orchestrator.schemas import Action, CallTool, MouseClick
from computeruse.repl.engine import CuaReplEngine
from computeruse.security.autonomy import AutonomyLevel
from computeruse.vision.ax import AXElement
from computeruse.vision.ax_diff import AXStateTracker
from tests.smoke.conftest import SOCKET_PATH


class RecordingDriver:
    """An external driver boundary recording exactly what could reach HID."""

    def __init__(self) -> None:
        self.actions: list[Action] = []
        self.releases: int = 0
        self.frontmost: str = "Form"
        self.modal_owned: bool = False

    def send(self, action: Action) -> None:
        self.actions.append(action)

    def release_inputs(self) -> None:
        self.releases += 1

    def focused_window(self) -> dict[str, str]:
        return {"app_name": self.frontmost, "bundle_id": ""}

    def activate_app(self, app_name: str) -> None:
        # Deliberately refuse activation so the modal proof is exercised.
        pass

    def app_pid(self, app_name: str) -> int:
        return 42

    def owns_focused_modal(self, pid: int) -> bool:
        assert pid == 42
        return self.modal_owned


@pytest.fixture
def engine() -> Iterator[CuaReplEngine]:
    runtime = CuaReplEngine()
    try:
        yield runtime
    finally:
        runtime.stop()


@pytest.mark.parametrize("code", [
    "return process.env",
    "return require('child_process')",
    "return global.process",
    "return Buffer.from('escape')",
    "return Function('return process')()",
    "return cua.getApp.constructor('return process')()",
    "return ({}).constructor.constructor('return process')()",
    "return __hostRpc.constructor('return process')()",
    "return await import('node:fs')",
    "const app = await cua.getApp('Form'); return app.constructor.constructor('return process')()",
    "try { await sendRpc('invalidMethod', {}); } catch(e) { return e.constructor.constructor('return process')(); }",
])
def test_untrusted_js_cannot_reach_node(engine: CuaReplEngine, code: str) -> None:
    result = engine.execute(code, timeout_s=3)
    assert result.is_error, result
    assert result.error
    assert not engine.execute("return 7", timeout_s=3).is_error


def test_guest_globals_and_prototypes_do_not_pollute_host(engine: CuaReplEngine) -> None:
    result = engine.execute(
        "Object.prototype.polluted = 'yes'; globalThis.secret = 'guest'; "
        "return [typeof process, typeof require, typeof Buffer, typeof fetch]"
    )
    assert json.loads(result.content) == ["undefined"] * 4
    # Every evaluation receives a fresh realm; a failed snippet cannot poison
    # the next caller's trusted CUA methods.
    result = engine.execute("return [typeof secret, typeof ({}).polluted]")
    assert json.loads(result.content) == ["undefined", "undefined"]


def test_timeout_releases_hardware_and_discards_worker() -> None:
    driver = RecordingDriver()
    runtime = CuaReplEngine(driver_client=driver)
    try:
        result = runtime.execute("while(true) {}", timeout_s=0.15)
        assert result.is_error and "timed out" in (result.error or "")
        assert driver.releases == 1
        assert runtime._proc is None
        assert runtime.execute("return 42", timeout_s=3).content == "42"
        assert driver.actions == []
    finally:
        runtime.stop()


def test_js_exception_releases_without_pressing_escape() -> None:
    driver = RecordingDriver()
    runtime = CuaReplEngine(driver_client=driver)
    try:
        result = runtime.execute("throw new Error('audit diagnostic')", timeout_s=3)
        assert result.is_error and "audit diagnostic" in (result.error or "")
        assert driver.releases == 1
        assert driver.actions == []
    finally:
        runtime.stop()


def test_ooda_receives_actual_bridge_diagnostic(engine: CuaReplEngine) -> None:
    runner = object.__new__(OodaRunner)
    runner.cua_repl = engine
    action = CallTool(type="call_tool", tool="js",
                      arguments={"code": "throw new Error('specific audit failure')"})
    result = runner._run_tool(action)
    assert "failed" in result and "specific audit failure" in result


def form(titles: tuple[str, ...]) -> AXElement:
    return AXElement(role="Window", title="Form", width=600, height=400,
                     children=tuple(AXElement(role="Button", title=title,
                                              x=10, y=50 + i * 40, width=100, height=30)
                                    for i, title in enumerate(titles)))


def test_index_insertion_never_retargets_old_id() -> None:
    tracker = AXStateTracker("Form")
    tracker.render_state(form(("Continue",)), "Form")
    diff = tracker.render_state(form(("Delete everything", "Continue")), "Form")
    assert tracker.get_element_by_index(1).title == "Continue"
    assert '[2] Button "Delete everything"' in diff
    tracker.render_state(form(("Delete everything",)), "Form")
    assert tracker.get_element_by_index(1) is None


def test_live_index_revalidation_prevents_shifted_click() -> None:
    driver = RecordingDriver()
    current = form(("Continue",))

    def snapshot(app: str) -> tuple[AXElement, str]:
        return current, "Form"

    runtime = CuaReplEngine(driver_client=driver, snapshot_provider=snapshot)
    try:
        assert not runtime.execute("await cua.getApp('Form')").is_error
        current = form(("Delete everything", "Continue"))
        result = runtime.execute("const app = await cua.getApp('Form'); await app.click(1)")
        assert not result.is_error, result.error
        click = next(action for action in driver.actions if isinstance(action, MouseClick))
        assert click.y == 105  # Continue moved; destructive replacement was not touched.
    finally:
        runtime.stop()


def test_missing_target_is_not_healed_to_duplicate_title() -> None:
    tracker = AXStateTracker("Form")
    tracker.render_state(form(("Continue", "Continue")), "Form")
    assert tracker.find_matching_element("Button", "Continue") is None


@pytest.mark.parametrize("owned", [True, False])
def test_modal_requires_live_owner_proof(owned: bool) -> None:
    driver = RecordingDriver()
    driver.frontmost = "Unrelated dialog service"
    driver.modal_owned = owned
    runtime = CuaReplEngine(driver_client=driver)
    try:
        result = runtime.execute(
            "const app = await cua.getApp('Form'); await app.pressKey('Escape')", timeout_s=3)
        assert result.is_error is not owned
        assert bool(driver.actions) is owned
    finally:
        runtime.stop()


def test_menu_source_is_fixed_and_arguments_are_data(
    monkeypatch: pytest.MonkeyPatch, engine: CuaReplEngine,
) -> None:
    captured: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    malicious = 'A"\\\n & do shell script "never execute"'
    assert engine._select_menu_item(malicious, ["File", malicious, "Don't Save"])
    argv = captured[0]
    assert malicious not in argv[2]
    assert 'menu bar item menuName of menu bar 1' in argv[2]
    assert 'menu item itemName of menu 1 of targetControl' in argv[2]
    assert argv[3:] == ["--", malicious, "File", malicious, "Don't Save"]


def test_destructive_menu_is_guarded_before_applescript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        pytest.fail("AppleScript was called before the security refusal")

    monkeypatch.setattr(subprocess, "run", forbidden)
    driver = RecordingDriver()
    runtime = CuaReplEngine(driver_client=driver, autonomy_level=AutonomyLevel.GUARDED)
    try:
        result = runtime.execute(
            "const app = await cua.getApp('Form'); await app.selectMenuItem(['File', 'Delete'])")
        assert result.is_error and "Security Refusal" in (result.error or "")
        assert driver.actions == []
    finally:
        runtime.stop()


def test_menu_failures_preserve_exit_status_and_stderr(
    monkeypatch: pytest.MonkeyPatch, engine: CuaReplEngine,
) -> None:
    def failed(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 17, stdout="", stderr="AX refused menu")

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(RuntimeError, match="status=17.*AX refused menu"):
        engine._select_menu_item("Form", ["File", "Open"])


def test_cleanup_and_modal_contract_over_real_rust_socket() -> None:
    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        client.release_inputs()
        assert client.owns_focused_modal(42) is False
        assert client.health()["backend"] == "simulated"


def test_native_memory_exhaustion_stays_inside_guest(engine: CuaReplEngine) -> None:
    result = engine.execute("return new Array(30000000).fill('memory')", timeout_s=3)
    assert result.is_error
    assert "memory" in (result.error or "").lower()
    assert engine.execute("return 5", timeout_s=3).content == "5"


def test_node_preload_and_host_secrets_are_not_inherited(
    monkeypatch: pytest.MonkeyPatch, engine: CuaReplEngine,
) -> None:
    monkeypatch.setenv("NODE_OPTIONS", "--require=/nonexistent/audit-preload.js")
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret-not-for-js")
    assert engine.execute("return typeof process", timeout_s=3).content == "undefined"


def test_unhandled_driver_interruption_releases_and_stops() -> None:
    class InterruptedDriver(RecordingDriver):
        def send(self, action: Action) -> None:
            raise KeyboardInterrupt("human takeover")

    driver = InterruptedDriver()
    runtime = CuaReplEngine(driver_client=driver)
    try:
        with pytest.raises(KeyboardInterrupt, match="human takeover"):
            runtime.execute("const app = await cua.getApp('Form'); await app.pressKey('a')")
        assert driver.releases == 1
        assert runtime._proc is None
    finally:
        runtime.stop()


def test_retired_window_ids_cannot_select_new_window() -> None:
    tracker = AXStateTracker("Form")
    tracker.render_state(form(("Continue",)), "First document")
    tracker.render_state(form(("Continue",)), "Second document")
    assert tracker.get_element_by_index(1) is None
    assert tracker.get_historical_element(1) is None


def test_guest_work_cannot_continue_after_evaluation(engine: CuaReplEngine) -> None:
    result = engine.execute(
        "void (async () => { await cua.sleep(100); await cua.getApp('LateApp'); })(); return 'done'",
        timeout_s=3,
    )
    assert result.content == "done"
    assert engine.execute("await cua.sleep(150); return 'next'", timeout_s=3).content == "next"
    assert "LateApp" not in engine.trackers


def test_internal_revalidation_does_not_consume_visible_diff() -> None:
    tracker = AXStateTracker("Form")
    tracker.render_state(form(("Continue",)), "Form")
    changed = form(("Delete everything", "Continue"))
    tracker.refresh_state(changed, "Form")
    diff = tracker.render_state(changed, "Form")
    assert '+ [2] Button "Delete everything"' in diff
    assert tracker.get_element_by_index(1).title == "Continue"
