from __future__ import annotations

import urllib.error
import urllib.request
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from computeruse.orchestrator.client import ActuationClient, DriverTimeoutError
from computeruse.orchestrator.loop import CredentialEntryRefused
from computeruse.orchestrator.schemas import (
    AgentTurn,
    CallTool,
    Finish,
    MouseClick,
    PressHotkey,
    TypeText,
)
from computeruse.repl.engine import CuaReplEngine, WindowBoundsUnavailableError
from computeruse.security.approvals import ApprovalQueue, ApprovalRequest, consumed
from computeruse.security.autonomy import (
    AutonomyLevel,
    is_command_payload,
)
from computeruse.security.grants import action_verbs
from computeruse.security.permissions import PermissionDeniedError
from computeruse.skills.distiller import Trajectory, signature_of
from computeruse.tools.web import SafeRedirectHandler, WebError
from computeruse.vision.ax import AXElement, asks_for_a_credential, is_secure_field
from computeruse.vision.ax_diff import AXStateTracker


def test_r1_raw_coordinate_risk_floors_at_routine() -> None:
    driver = MagicMock()
    runtime = CuaReplEngine(driver_client=driver, autonomy_level=AutonomyLevel.OBSERVER)
    # Under OBSERVE autonomy, ROUTINE actions are refused (requiring approval/permission)
    with pytest.raises(PermissionDeniedError, match="blocked under autonomy level OBSERVER"):
        runtime._check_security(MouseClick(type="mouse_click", x=50, y=50), "Notes", target_label=None)


def test_r1_find_element_at() -> None:
    tracker = AXStateTracker(app_name="App")
    root = AXElement(
        role="AXWindow",
        title="App",
        x=0,
        y=0,
        width=500,
        height=500,
        children=[
            AXElement(role="AXButton", title="Submit", x=100, y=100, width=80, height=30),
            AXElement(role="AXButton", title="Cancel", x=200, y=100, width=80, height=30),
        ],
    )
    tracker.render_state(root, "App")
    matched = tracker.find_element_at(120, 110)
    assert matched is not None
    assert matched.title == "Submit"
    assert matched.role == "AXButton"


def test_r2_ax_subrole_and_credential_detection() -> None:
    field = AXElement(
        role="AXTextField",
        subrole="AXSecureTextField",
        title="Password",
        x=10,
        y=10,
        width=100,
        height=20,
    )
    assert is_secure_field(field)
    root = AXElement(role="AXWindow", title="Login", children=[field])
    assert asks_for_a_credential(root)


def test_r2_cua_credential_guard_blocks_typing() -> None:
    driver = MagicMock()
    runtime = CuaReplEngine(driver_client=driver)
    secure_snap = AXElement(
        role="AXWindow",
        title="Login",
        children=[
            AXElement(role="AXTextField", subrole="AXSecureTextField", title="Password")
        ],
    )
    runtime.snapshot_provider = lambda _app: (secure_snap, "Login")
    with pytest.raises(CredentialEntryRefused, match="Agent never types credentials"):
        runtime._check_security(TypeText(type="type_text", text="secret123"), "Login")


def test_r4_compound_command_detection() -> None:
    assert is_command_payload("echo ok && rm -rf /tmp/test")
    assert is_command_payload("ls -la; sudo reboot")
    assert is_command_payload("cat /dev/null | dd of=/dev/sda")


def test_r4_pointer_actions_ignore_freeform_subgoal_for_destructive_grants() -> None:
    click = MouseClick(type="mouse_click", x=10, y=10)
    verbs = action_verbs(click, sub_goal="delete the file immediately", target_label="Open Folder")
    assert "delete" not in verbs


def test_r4_destructive_hotkey_verbs() -> None:
    hotkey = PressHotkey(type="press_hotkey", key="backspace", modifiers=("command",))
    verbs = action_verbs(hotkey, sub_goal="remove item", target_label="")
    assert "delete" in verbs


def test_r6_r7_approval_consumed() -> None:
    req = ApprovalRequest(
        request_id="req-123",
        mission_id="m-1",
        goal="test",
        sub_goal="click",
        action_type="mouse_click",
        action={"type": "mouse_click", "x": 10, "y": 10},
        target_label="Button",
        risk="routine",
        created_at=datetime.now(UTC),
        decision="approved",
    )
    used = consumed(req, now=datetime.now(UTC))
    assert used.decision == "consumed"


def test_r8_actuation_client_unresponsive_recovery() -> None:
    recovered = False

    def on_unresponsive() -> None:
        nonlocal recovered
        recovered = True

    client = ActuationClient(
        socket_path="/tmp/nonexistent_socket.sock",
        recover_unresponsive=on_unresponsive,
        recv_timeout_seconds=0.05,
    )
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = TimeoutError("driver did not reply")
    client._sock = mock_sock

    with pytest.raises(DriverTimeoutError):
        client.focused_window()

    assert recovered, "recover_unresponsive should have been triggered on deadline expiry"


def test_r9_window_bounds_unavailable_error() -> None:
    engine = CuaReplEngine(driver_client=None)
    with pytest.raises(WindowBoundsUnavailableError):
        engine._get_app_snapshot("UnopenedApp")


def test_r10_cua_signatures_distinct() -> None:
    t1 = Trajectory(
        description="save draft",
        app="Notes",
        steps=(CallTool(type="call_tool", tool="cua_repl", arguments={"code": "await app.click('Save')"}),),
        step_targets=("",),
    )
    t2 = Trajectory(
        description="delete draft",
        app="Notes",
        steps=(CallTool(type="call_tool", tool="cua_repl", arguments={"code": "await app.click('Delete')"}),),
        step_targets=("",),
    )
    sig1 = signature_of(t1)
    sig2 = signature_of(t2)
    assert sig1 != sig2


def test_web_redirect_ssrf_protection() -> None:
    handler = SafeRedirectHandler()
    req = urllib.request.Request("http://example.com")
    with pytest.raises(WebError, match="forbidden destination"):
        handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1:8080/admin")


def test_r1_container_filtering_returns_none_for_empty_window() -> None:
    tracker = AXStateTracker(app_name="App")
    root = AXElement(
        role="AXWindow",
        title="App",
        x=0,
        y=0,
        width=500,
        height=500,
        children=[],
    )
    tracker.render_state(root, "App")
    # Clicking empty window space should return None, not the Window container
    assert tracker.find_element_at(250, 250) is None


def test_r2_nssecure_textfield_detection() -> None:
    elem = AXElement(
        role="AXTextField",
        subrole="NSSecureTextField",
        title="PIN",
        x=0,
        y=0,
        width=100,
        height=20,
    )
    assert is_secure_field(elem)


def test_r6_failed_finish_without_prior_error_records_summary() -> None:
    from computeruse.orchestrator.loop import OodaRunner, WorkingState

    def provider(state: WorkingState) -> AgentTurn:
        return AgentTurn(
            thought="cannot complete",
            sub_goal="give up",
            action=Finish(type="finish", status="failed", summary="unable to find target item"),
        )

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda _a: None,
        verify_enabled=False,
        max_steps=5,
    )
    final = runner.run(goal="test task")
    assert final.last_error == "unable to find target item", "Failed finish must record honest failure outcome"


def test_r7_approval_queue_consume_disk(tmp_path) -> None:
    queue = ApprovalQueue(tmp_path)
    now = datetime.now(UTC)
    req = ApprovalRequest(
        request_id="req-disk-1",
        mission_id="m-1",
        goal="test",
        sub_goal="click",
        action_type="mouse_click",
        action={"type": "mouse_click", "x": 10, "y": 10},
        target_label="Button",
        risk="routine",
        created_at=now,
        decision="approved",
    )
    queue.submit(req)
    consumed_req = queue.consume("req-disk-1", now=now)
    assert consumed_req.decision == "consumed"
    reqs = {r.request_id: r for r in queue.requests()}; assert reqs["req-disk-1"].decision == "consumed"

    # Repeated consumption must fail
    with pytest.raises(ValueError, match="cannot consume approval request"):
        queue.consume("req-disk-1", now=now)


def test_web_ipv4_mapped_ipv6_ssrf() -> None:
    from computeruse.tools.web import _is_fetchable_url
    assert not _is_fetchable_url("http://[::ffff:127.0.0.1]/")
    assert not _is_fetchable_url("http://[::ffff:169.254.169.254]/")


#: Payload size that reproduces the QuickJS teardown crash. The WASM heap
#: starts at 16MB and the bridge only breaks once an evaluation grows it:
#: measured against this bridge, a 3MB RPC result tore down cleanly and 4MB
#: aborted the worker on
#: ``Assertion failed: list_empty(&rt->gc_obj_list)``. 8MB sits far enough
#: above the threshold that the test is not measuring the boundary itself.
_CRASHING_PAYLOAD_BYTES = 8 * 1024 * 1024

#: How many screenshot/crop/follow-up rounds one worker must survive. The
#: crash was never visible in the call that caused it — that call published
#: ``completed`` and *then* the process died — so a single round proves
#: nothing. The failure always surfaced on the following evaluation.
_TRANSPORT_ROUNDS = 3


def test_r5_large_rpc_results_do_not_poison_the_next_evaluation() -> None:
    """A screenshot-sized RPC result must not kill the sandbox worker.

    Regression for the crash that ended the real TextEdit end-to-end run twice
    at ``Crop one element``: a multi-megabyte result grew the emscripten
    linear memory while the QuickJS runtime was live, which corrupted its GC
    list and aborted the process inside ``JS_FreeRuntime``. The evaluation that
    triggered it had already reported success, so the damage was only ever
    observable one call later.

    Deliberately exercises the *transport*, not the screen: the payloads are
    synthetic, so the test needs neither Screen Recording consent nor a
    driver, and it fails for exactly one reason.
    """
    engine = CuaReplEngine()
    big_uri = "data:image/png;base64," + "x" * _CRASHING_PAYLOAD_BYTES
    original = engine._dispatch_js_call

    def dispatch(method: str, params: dict[str, object]) -> object:
        if method in {"getScreenshot", "cropScreenshot"}:
            return big_uri
        return original(method, params)

    try:
        with patch.object(engine, "_dispatch_js_call", side_effect=dispatch):
            for round_index in range(_TRANSPORT_ROUNDS):
                captured = engine.execute(
                    'const a = await cua.getApp("Probe");'
                    " const shot = await a.getScreenshot();"
                    " const crop = await a.cropScreenshot({x:0,y:0,width:10,height:10});"
                    " return String(shot.length + crop.length);",
                    timeout_s=30,
                )
                assert not captured.is_error, (
                    f"round {round_index}: large-payload call failed: {captured.error}"
                )
                # The real assertion. The crashing build also reported
                # ``completed`` here and died during teardown, so the worker's
                # health is only provable by asking it something afterwards.
                follow_up = engine.execute("return 42", timeout_s=10)
                assert not follow_up.is_error, (
                    f"round {round_index}: worker died after a large payload: "
                    f"{follow_up.error}"
                )
                assert follow_up.content.strip() == "42"
    finally:
        engine.stop()
