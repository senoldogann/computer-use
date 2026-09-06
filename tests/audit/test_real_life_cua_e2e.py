"""Empirical real-life end-to-end audit for CUA REPL on macOS.

Drives the full stack against a *live* macOS application:

1. Rust micro-driver IPC actuation (keyboard, AX snapshot, screenshot)
2. Node.js bridge executing ``globalThis.cua`` JavaScript
3. AX tree diffing and element index mapping
4. The MCP ``mcpToolCall`` JSON contract matching OpenAI CUA's format.

**Why the backend assertion is the first thing here.** This audit previously
spawned the driver with no ``--real`` flag, so it ran against the *simulated*
backend: a fixture that answers ``focused_window`` with a hardcoded Safari
window in 0.1ms and accepts every activation. The whole file passed in 0.79s
and asserted nothing about macOS — while its name, its docstring and its
``WindowServer`` skip gate all said "live". A probe that cannot fail is not
evidence, so the backend is now checked before anything else runs.

**Why it is opt-in.** Live means live: this takes the keyboard and moves the
front window. That is not something a plain ``pytest`` run should do to
whoever is at the machine, so it is enabled deliberately and skipped loudly
otherwise.

**Why it makes its own document.** The audit ends by selecting all and
deleting, which is only safe because the window it is aimed at is a scratch
file this test created. Run against "whatever TextEdit had open", that same
keystroke pair destroys the user's work.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from computeruse.orchestrator.client import ActuationClient
from computeruse.orchestrator.schemas import PressHotkey
from computeruse.repl.engine import CuaReplEngine

REPO_ROOT = Path(__file__).resolve().parents[2]
DRIVER_DIR = REPO_ROOT / "driver"
DEBUG_BIN = DRIVER_DIR / "target" / "debug" / "actuation-driver"
RELEASE_BIN = DRIVER_DIR / "target" / "release" / "actuation-driver"
TEST_SOCKET = REPO_ROOT / "target" / "cua-real-e2e.sock"


def live_driver_binary() -> Path:
    """The driver build this audit should drive, release first.

    macOS keys TCC consent to the *binary*, not to the project: Screen
    Recording granted to one build says nothing about another. Measured on this
    machine — the release binary captured a 3420x2224 frame while the debug
    binary was refused display 0 with "failed to capture display 0". The smoke
    suite standardises on the debug build because it only needs actuation; an
    audit that claims to see the screen has to run the build that is actually
    allowed to.
    """
    return RELEASE_BIN if RELEASE_BIN.exists() else DEBUG_BIN


def assert_driver_is_current(binary: Path) -> None:
    """Fail if the audited binary predates the Rust sources it stands for.

    A stale build turns a green audit into a statement about code that is no
    longer in the tree, which is the same class of lie as auditing the
    simulator.
    """
    sources = list((DRIVER_DIR / "src").rglob("*.rs"))
    newest = max((path.stat().st_mtime for path in sources), default=0.0)
    assert binary.stat().st_mtime >= newest, (
        f"{binary} is older than driver/src; rebuild it "
        f"({'cargo build --release' if binary == RELEASE_BIN else 'cargo build'}) "
        "before trusting this audit"
    )

#: Opt-in switch. Absent, the audit skips with an explanation rather than
#: quietly running against a simulator and reporting success.
LIVE_ENV_VAR = "COMPUTERUSE_LIVE_AUDIT"

SKIP_REASON = (
    f"live macOS audit is opt-in: set {LIVE_ENV_VAR}=1 to run it. It takes the "
    "keyboard, brings TextEdit to the front and types into a scratch document."
)


def _live_audit_requested() -> bool:
    return os.environ.get(LIVE_ENV_VAR, "").strip().lower() not in ("", "0", "false", "no")


def _await_socket(path: Path, proc: subprocess.Popen[str]) -> None:
    """Block until the driver binds its socket, or fail with its own output."""
    for _ in range(100):
        if path.exists():
            return
        if proc.poll() is not None:
            _out, err = proc.communicate(timeout=5)
            raise AssertionError(f"driver exited before binding {path}: {err}")
        time.sleep(0.05)
    raise AssertionError(f"driver never bound {path}")


def _run_script(engine: CuaReplEngine, code: str, title: str) -> str:
    """Execute one CUA REPL call and return its content, failing loudly."""
    result = engine.execute(code, title=title)
    assert result.status == "completed", f"{title!r} failed: {result.error}"
    return result.content


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only audit")
@pytest.mark.skipif(not _live_audit_requested(), reason=SKIP_REASON)
def test_real_life_cua_repl_textedit_e2e(tmp_path: Path) -> None:
    """Live macOS: drive TextEdit through the CUA REPL and check every contract."""
    driver_bin = live_driver_binary()
    assert driver_bin.exists(), (
        f"driver not built: {driver_bin}. Run `cargo build --release` in driver/."
    )
    assert_driver_is_current(driver_bin)

    # The document the audit owns. Everything destructive below is aimed here.
    scratch = tmp_path / "computeruse-live-audit.txt"
    scratch.write_text("audit fixture\n", encoding="utf-8")

    if TEST_SOCKET.exists():
        TEST_SOCKET.unlink()

    driver_proc = subprocess.Popen(
        [str(driver_bin), str(TEST_SOCKET), "--real"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "COMPUTERUSE_NO_STATUS": "1"},
    )

    client: ActuationClient | None = None
    try:
        _await_socket(TEST_SOCKET, driver_proc)
        client = ActuationClient(str(TEST_SOCKET), connect_retries=5)
        client.connect()

        # Contract zero: this is the real actuator. Asserted before any other
        # claim, because every assertion below is meaningless without it.
        health = client.health()
        assert health.get("backend") == "quartz/real", (
            f"audit is not live: driver reports backend={health.get('backend')!r}. "
            "The simulated backend answers every call from a fixture, so nothing "
            "below would be evidence about macOS."
        )
        assert health.get("trusted") is True, (
            "macOS Accessibility consent is not granted to the driver, so it "
            "cannot read AX trees or post events. Grant it in System Settings > "
            "Privacy & Security > Accessibility."
        )

        # Open the scratch document so TextEdit's front window is one this test
        # owns, then let the app settle before anything is aimed at it.
        subprocess.run(["open", "-a", "TextEdit", str(scratch)], check=True, timeout=30)
        for _ in range(60):
            if "TextEdit" in client.focused_window().app_name:
                break
            time.sleep(0.1)
        assert "TextEdit" in client.focused_window().app_name, (
            "TextEdit did not come to the front with the scratch document"
        )

        engine = CuaReplEngine(driver_client=client)

        # Call 1: initialise the app handle, and check the MCP envelope.
        call1 = 'var textEditApp = await cua.getApp("TextEdit");'
        result1 = engine.execute(call1, title="Inspect the TextEdit document")
        assert result1.status == "completed", f"call 1 failed: {result1.error}"
        assert "TextEdit" in result1.content
        mcp1 = result1.to_mcp_tool_call(
            call_id="call_sBq7TMnqcocA58PdHVS4mMAj",
            code=call1,
            title="Inspect the TextEdit document",
        )
        assert mcp1["type"] == "mcpToolCall"
        assert mcp1["tool"] == "js"
        assert mcp1["server"] == "cua_repl"
        assert mcp1["status"] == "completed"
        assert mcp1["arguments"]["code"] == call1

        # Call 2: a full AX tree of a live application.
        content2 = _run_script(
            engine,
            'var a = await cua.getApp("TextEdit");\n'
            "await a.getAXState({disableDiffing: true});",
            "Fetch the full AX tree",
        )
        assert "App: TextEdit" in content2
        assert "Window:" in content2

        # Call 3: type into the live document and read it back off the AX tree.
        # This is the assertion the simulated backend could never make, because
        # there was no document and no text — only an echo.
        typed = "Merhaba CUA REPL"
        content3 = _run_script(
            engine,
            'var a = await cua.getApp("TextEdit");\n'
            f'await a.typeText("{typed}");\n'
            "await a.getAXState({disableDiffing: true});",
            "Type into the document",
        )
        assert typed in content3, (
            f"typed text is absent from the live AX tree; TextEdit shows: {content3[:400]}"
        )

        # Call 4: a real screenshot of a real screen. The length is the
        # assertion that matters — the prefix alone used to be returned for a
        # *failed* capture, so checking for it proved only that the engine
        # knows how to spell a data URI.
        content4 = _run_script(
            engine,
            'var a = await cua.getApp("TextEdit");\n'
            "const uri = await a.getScreenshot();\n"
            "return uri.length;",
            "Capture the screen",
        )
        prefix_length = len("data:image/png;base64,")
        assert int(content4.strip()) > prefix_length * 10, (
            f"the screen capture carried no image ({content4.strip()} chars). "
            f"{driver_bin} needs Screen Recording consent in System Settings > "
            "Privacy & Security > Screen Recording (macOS grants it per binary, "
            "so a grant to another build does not carry over)."
        )

        # Call 5: photograph one element instead of the display. This is the
        # saving the visual-confirmation path exists for, measured rather than
        # asserted in the abstract.
        content5 = _run_script(
            engine,
            'var a = await cua.getApp("TextEdit");\n'
            "await a.getAXState({disableDiffing: true});\n"
            'var el = await a.find({role: "AXTextArea"});\n'
            "if (!el) return JSON.stringify({found: false});\n"
            "var uri = await el.crop();\n"
            "return JSON.stringify({found: true, uriLength: uri.length, "
            'prefix: uri.slice(0, 22)});',
            "Crop one element",
        )
        crop_report = json.loads(content5.strip())
        assert crop_report["found"], "no text area in the live TextEdit window"
        assert crop_report["prefix"] == "data:image/png;base64,"
        assert crop_report["uriLength"] < int(content4.strip()) / 10, (
            f"element crop is {crop_report['uriLength']} chars against a "
            f"{content4.strip()}-char frame; the saving is not there"
        )

        # Call 6: clear the scratch document. Safe only because the window is
        # the one this test opened — and because the engine now refuses to post
        # a keystroke into an application it cannot confirm is frontmost.
        _run_script(
            engine,
            'var a = await cua.getApp("TextEdit");\n'
            'await a.pressKey("Cmd+A");\n'
            'await a.pressKey("Delete");\n'
            "await a.getAXState();",
            "Clear the scratch document",
        )
        content6 = _run_script(
            engine,
            'var a = await cua.getApp("TextEdit");\n'
            "await a.getAXState({disableDiffing: true});",
            "Confirm the document is empty",
        )
        assert typed not in content6, "the text survived select-all + delete"
    finally:
        # Nested so that *nothing* in the desktop cleanup can skip the
        # termination below. Measured the hard way: an earlier version called
        # ``osascript`` here, it blocked on a TCC prompt and raised
        # TimeoutExpired straight out of this block — leaving a driver running
        # with ``--real`` on the user's machine for hours, holding live
        # actuation capability nobody was watching. Tidying the desktop is
        # best effort; putting the actuator down is not.
        try:
            if client is not None:
                # Close the scratch window through the driver rather than
                # AppleScript: ``osascript`` needs its own Automation consent,
                # and without it the call blocks on a prompt nobody answers.
                # The driver already holds Accessibility consent, so this costs
                # no extra permission and cannot block.
                try:
                    focused = client.focused_window()
                    if "TextEdit" not in focused.app_name or scratch.name not in focused.window_title:
                        raise RuntimeError("scratch document is not focused; refusing cleanup keystrokes")
                    client.send(
                        PressHotkey(type="press_hotkey", modifiers=["command"], key="s")
                    )
                    time.sleep(0.4)
                    client.send(
                        PressHotkey(type="press_hotkey", modifiers=["command"], key="w")
                    )
                    time.sleep(0.4)
                except (OSError, RuntimeError) as exc:
                    print(f"scratch document left open: {exc}", file=sys.stderr)
                try:
                    client.close()
                except OSError:
                    pass
        finally:
            driver_proc.terminate()
            try:
                driver_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                driver_proc.kill()
            if TEST_SOCKET.exists():
                TEST_SOCKET.unlink()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only audit")
def test_simulated_backend_is_distinguishable_from_the_real_one() -> None:
    """The simulator must announce itself, so an audit can refuse to trust it.

    Runs unconditionally and touches nothing: it is the guard that keeps the
    live audit above honest. If the simulated driver ever started reporting
    ``quartz/real``, the backend assertion up there would stop protecting
    anything and this test says so first.
    """
    assert DEBUG_BIN.exists(), f"driver not built: {DEBUG_BIN}"
    sock_path = REPO_ROOT / "target" / "cua-backend-identity.sock"
    if sock_path.exists():
        sock_path.unlink()
    proc = subprocess.Popen(
        [str(DEBUG_BIN), str(sock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _await_socket(sock_path, proc)
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(str(sock_path))
        try:
            conn.sendall(b'{"method":"health"}\n')
            buffer = b""
            while not buffer.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buffer += chunk
        finally:
            conn.close()
        health = json.loads(buffer.decode("utf-8"))
        assert health["backend"] == "simulated", (
            f"a driver spawned without --real reported {health['backend']!r}; the "
            "live audit's backend check can no longer tell the two apart"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if sock_path.exists():
            sock_path.unlink()
