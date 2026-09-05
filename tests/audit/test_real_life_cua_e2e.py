"""Empirical real-life end-to-end test for CUA REPL on macOS.

Validates the full stack against live macOS applications:
1. Rust Micro-Driver IPC actuation (mouse, keyboard, AX snapshot)
2. Node.js bridge executing `globalThis.cua` JavaScript
3. AX tree diffing and element index mapping
4. Full MCP `mcpToolCall` JSON contract matching OpenAI CUA format.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from computeruse.orchestrator.client import ActuationClient
from computeruse.repl.engine import CuaReplEngine

REPO_ROOT = Path(__file__).resolve().parents[2]
DRIVER_BIN = REPO_ROOT / "driver" / "target" / "debug" / "actuation-driver"
TEST_SOCKET = REPO_ROOT / "target" / "cua-real-e2e.sock"


def is_macos_gui_available() -> bool:
    """Check whether a live macOS GUI session is available."""
    if sys.platform != "darwin":
        return False
    ret = os.system("pgrep WindowServer > /dev/null 2>&1")
    return ret == 0


@pytest.mark.skipif(
    not is_macos_gui_available(), reason="Requires live macOS GUI session"
)
def test_real_life_cua_repl_textedit_e2e(tmp_path: Path) -> None:
    """Live macOS test: launches TextEdit, executes sequential CUA JS scripts, verifies AX diff & MCP contract."""
    if TEST_SOCKET.exists():
        TEST_SOCKET.unlink()

    driver_proc = subprocess.Popen(
        [str(DRIVER_BIN), str(TEST_SOCKET)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    client = None
    try:
        for _ in range(30):
            if TEST_SOCKET.exists():
                try:
                    client = ActuationClient(str(TEST_SOCKET), connect_retries=1)
                    client.connect()
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(0.1)
            time.sleep(0.1)

        assert client is not None, "Driver failed to bind to socket within timeout"

        engine = CuaReplEngine(driver_client=client)

        # Call 1: Initialize App
        call1_code = 'var textEditApp = await cua.getApp("TextEdit");'
        res1 = engine.execute(
            call1_code, title="TextEdit test belgelerinin mevcut durumunu incele"
        )
        assert res1.status == "completed", f"Call 1 failed: {res1.error}"
        assert res1.duration_ms >= 0
        assert "TextEdit" in res1.content

        mcp1 = res1.to_mcp_tool_call(
            call_id="call_sBq7TMnqcocA58PdHVS4mMAj",
            code=call1_code,
            title="TextEdit test belgelerinin mevcut durumunu incele",
        )
        assert mcp1["type"] == "mcpToolCall"
        assert mcp1["id"] == "call_sBq7TMnqcocA58PdHVS4mMAj"
        assert mcp1["tool"] == "js"
        assert mcp1["server"] == "cua_repl"
        assert mcp1["status"] == "completed"
        assert mcp1["arguments"]["code"] == call1_code

        # Call 2: Press Escape and get AX diff
        call2_code = """
        var textEditApp = await cua.getApp("TextEdit");
        await textEditApp.pressKey("Escape");
        await textEditApp.getAXState();
        """
        res2 = engine.execute(
            call2_code, title="Durdurulan testin konum penceresini kapat"
        )
        assert res2.status == "completed", f"Call 2 failed: {res2.error}"
        mcp2 = res2.to_mcp_tool_call(
            call_id="call_tFJxeR4uQIYgSMgIpZX6Wik2",
            code=call2_code,
            title="Durdurulan testin konum penceresini kapat",
        )
        assert mcp2["status"] == "completed"
        assert len(mcp2["result"]["content"]) > 0

        # Call 3: Explicit disableDiffing for full tree inspection
        call3_code = """
        var textEditApp = await cua.getApp("TextEdit");
        await textEditApp.getAXState({disableDiffing: true});
        """
        res3 = engine.execute(call3_code, title="Aç penceresinin tam AX ağacını getir")
        assert res3.status == "completed"
        assert "Window:" in res3.content
        assert "App: TextEdit" in res3.content

        # Call 4: Type text and check AX state update
        call4_code = """
        var textEditApp = await cua.getApp("TextEdit");
        await textEditApp.typeText("Merhaba CUA REPL");
        await textEditApp.getAXState();
        """
        res4 = engine.execute(call4_code, title="Belgeye metin yaz")
        assert res4.status == "completed"
        assert res4.duration_ms >= 0
        assert "Merhaba CUA REPL" in res4.content

    finally:
        if client:
            try:
                client.close()
            except Exception:  # noqa: BLE001, S110
                pass
        driver_proc.terminate()
        try:
            driver_proc.wait(timeout=2.0)
        except Exception:  # noqa: BLE001
            driver_proc.kill()
        if TEST_SOCKET.exists():
            TEST_SOCKET.unlink()
