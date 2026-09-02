"""Contract-drift test: does what Python serializes actually satisfy Rust?

Python and Rust both hand-write the wire contract (the Pydantic action models
in ``schemas.py`` vs the enum in ``protocol.rs``). Without a guard they can
drift silently — a renamed or re-typed field compiles fine on either side but
breaks the RPC at runtime. These tests send the *verbatim* bytes the Python
``action_to_request`` would produce through the real compiled driver socket and
assert an ``ack`` comes back, forcing both implementations to agree on exact
shape at runtime (Law 3 code archaeology: the contract is the test).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from computeruse.orchestrator.client import ActuationClient, action_to_request
from computeruse.orchestrator.schemas import (
    AgentTurn,
    LoadSkill,
    MouseClick,
    MouseDrag,
    MouseMove,
    MouseScroll,
    PressHotkey,
    TypeText,
    Wait,
)
from tests.smoke.conftest import REPO_ROOT, SOCKET_PATH, rpc_call_raw


@pytest.mark.parametrize(
    "action",
    [
        MouseMove(type="mouse_move", x=640, y=480, duration_ms=80),
        MouseClick(type="mouse_click", x=100, y=100),
        MouseDrag(type="mouse_drag", start_x=0, start_y=0, end_x=50, end_y=50),
        MouseScroll(type="mouse_scroll", dx=0, dy=-40),
        TypeText(type="type_text", text="hello", wpm=40),
        PressHotkey(type="press_hotkey", modifiers=["command"], key="c"),
    ],
)
def test_python_serialization_accepted_by_rust_driver(action: object) -> None:
    """The exact bytes Python emits must parse and Ack in the real driver."""
    frame = AgentTurn.model_validate({"thought": "", "sub_goal": "", "action": action})
    wire = action_to_request(frame.action)
    response = rpc_call_raw(wire.encode("utf-8"))
    assert response.get("ok") == "ack", f"driver rejected {wire.strip()}: {response}"


def test_drag_wire_uses_method_names_rust_expects() -> None:
    """mouse_drag flows as ``mouse_drag`` with named start/end params."""
    frame = AgentTurn.model_validate(
        {
            "thought": "",
            "sub_goal": "",
            "action": MouseDrag(type="mouse_drag", start_x=0, start_y=0, end_x=50, end_y=50),
        }
    )
    payload = json.loads(action_to_request(frame.action))
    assert payload["method"] == "mouse_drag"
    assert payload["params"]["start_x"] == 0  # type: ignore[no-any-return]
    assert payload["params"]["end_y"] == 50  # type: ignore[no-any-return]


def test_client_send_ack_via_real_driver() -> None:
    """Through the typed client, a physical action round-trips as ack."""
    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        client.send(MouseClick(type="mouse_click", x=1, y=1))


def test_internal_actions_refuse_wire() -> None:
    """wait/load_skill are orchestrator-internal and must never reach the socket."""
    for internal in (
        Wait(type="wait", duration_ms=5, reason="nope"),
        LoadSkill(type="load_skill", skill_id="x"),
    ):
        frame = AgentTurn.model_validate({"thought": "", "sub_goal": "", "action": internal})
        with pytest.raises(ValueError):
            action_to_request(frame.action)


def test_menu_bridge_posts_json_strings() -> None:
    """The menu panel's JS must bridge JSON *strings*, never bare JS objects.

    The native handler (``driver/src/menu.rs`` ``handle_script_message``)
    downcasts the WKScriptMessage body to ``NSString`` and parses it as JSON.
    A bare JS object bridges to an ``NSDictionary``; the downcast fails, the
    message is silently dropped, and “Run agent” appears to do nothing while
    the ``▶ ...`` row still renders on the JS side. This guards the exact
    byte shape the JS sends (a reflection of the same drift concern caught for
    the RPC layer above).
    """
    html = (REPO_ROOT / "driver" / "assets" / "menu.html").read_text()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    match = re.search(r"function\s+post\s*\([^)]*\)\s*\{([^}]*)\}", script)
    assert match is not None, "menu.html must define a `post` bridge helper"
    body = match.group(1)
    assert "JSON.stringify" in body, "post() must stringify messages for the native bridge"
    assert "postMessage(m)" not in body, "post() must not send a bare JS object (bridges to NSDictionary)"


def test_panel_brand_is_left_aligned_without_traffic_light_lane() -> None:
    """The panel hides the native traffic lights, so the brand sits flush left.

    The toolbar used to reserve a 62px left lane for the window buttons and
    pushed the mark right with ``margin-left:auto``. With the lights hidden in
    ``menu.rs`` the lane is gone and the brand is the first element — keep the
    CSS and the native hiding decision from drifting apart.
    """
    html = (REPO_ROOT / "driver" / "assets" / "menu.html").read_text()
    assert "padding-left: 62px" not in html, "no reserved traffic-light lane in the toolbar"
    assert ".mark" in html
    mark_block = re.search(r"\.mark\{[^}]*\}", html)
    assert mark_block is not None
    assert "margin-left:auto" not in mark_block.group(0), "brand must not be pushed right"
    # The native side must keep hiding the three standard window buttons.
    menu_rs = (REPO_ROOT / "driver" / "src" / "menu.rs").read_text()
    for button in ["CloseButton", "MiniaturizeButton", "ZoomButton"]:
        assert button in menu_rs, f"menu.rs must hide the {button}"
    assert "setHidden: true" in menu_rs, "the hidden traffic lights must be hidden"

def test_every_module_imports_first(tmp_path: Path) -> None:
    """No module may require another to be imported before it.

    `security.autonomy` classifies actions, so it imports the orchestrator's
    schemas; the orchestrator's loop enforces the resulting verdict, so it
    imported `security.autonomy` back. The cycle was invisible because every
    entry point happened to reach the orchestrator first — but
    `import computeruse.security.autonomy` on its own raised ImportError, and
    so did anything a consumer imported before touching the orchestrator.

    A fresh interpreter per module is the only honest check: within one
    process the first successful import hides every ordering problem behind it.
    """
    modules = [
        "computeruse",
        "computeruse.agent",
        "computeruse.cli",
        "computeruse.memory",
        "computeruse.orchestrator",
        "computeruse.orchestrator.evidence",
        "computeruse.orchestrator.failures",
        "computeruse.orchestrator.loop",
        "computeruse.orchestrator.planner",
        "computeruse.orchestrator.prompts",
        "computeruse.security",
        "computeruse.security.autonomy",
        "computeruse.security.killswitch",
        "computeruse.security.permissions",
        "computeruse.skills.registry",
        "computeruse.vision",
        "computeruse.vision.ax",
    ]
    failures: list[str] = []
    for module in modules:
        result = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            failures.append(f"{module}: {result.stderr.strip().splitlines()[-1]}")
    assert not failures, "modules that cannot be imported first:\n" + "\n".join(failures)
