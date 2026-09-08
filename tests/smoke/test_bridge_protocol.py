from __future__ import annotations

import json

import pytest

from computeruse.bridge.protocol import (
    MAX_REQUEST_BYTES,
    BridgeProtocolError,
    encode_error,
    encode_success,
    parse_request_line,
)


CAPABILITY = "a" * 64


def test_parse_request_line_accepts_one_versioned_authenticated_frame() -> None:
    request = parse_request_line(
        (
            json.dumps(
                {
                    "version": 1,
                    "capability": CAPABILITY,
                    "method": "health",
                    "params": {},
                }
            )
            + "\n"
        ).encode()
    )

    assert request.version == 1
    assert request.capability == CAPABILITY
    assert request.method == "health"
    assert request.params == {}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"version": 2, "capability": CAPABILITY, "method": "health", "params": {}},
        {"version": 1, "capability": "short", "method": "health", "params": {}},
        {"version": 1, "capability": CAPABILITY, "method": "", "params": {}},
        {"version": 1, "capability": CAPABILITY, "method": "health", "params": []},
    ],
)
def test_parse_request_line_rejects_invalid_contract(payload: object) -> None:
    with pytest.raises(BridgeProtocolError) as exc:
        parse_request_line((json.dumps(payload) + "\n").encode())
    assert exc.value.code == "BRIDGE_PROTOCOL_INVALID"


def test_parse_request_line_rejects_oversized_frame_before_json_parse() -> None:
    with pytest.raises(BridgeProtocolError) as exc:
        parse_request_line(b"{" + b"x" * MAX_REQUEST_BYTES + b"}\n")
    assert exc.value.code == "BRIDGE_PROTOCOL_INVALID"


def test_bridge_response_envelope_is_stable_and_newline_delimited() -> None:
    success = json.loads(encode_success({"ready": True}))
    failure = json.loads(encode_error("FOCUS_NOT_ACQUIRED", "target did not focus"))

    assert success == {"ok": True, "result": {"ready": True}}
    assert failure == {
        "ok": False,
        "error": {"code": "FOCUS_NOT_ACQUIRED", "message": "target did not focus"},
    }
    assert encode_success(None).endswith(b"\n")
    assert encode_error("POLICY_DENIED", "no").endswith(b"\n")
