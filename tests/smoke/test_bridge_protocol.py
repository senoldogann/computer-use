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


def test_parse_request_line_accepts_one_versioned_secret_free_frame() -> None:
    request = parse_request_line(
        (
            json.dumps(
                {
                    "version": 1,
                    "method": "health",
                    "params": {},
                }
            )
            + "\n"
        ).encode()
    )

    assert request.version == 1
    assert not hasattr(request, "capability")
    assert request.method == "health"
    assert request.params == {}


def test_parse_request_line_rejects_capability_bearing_frame() -> None:
    with pytest.raises(BridgeProtocolError) as exc:
        parse_request_line(
            (
                json.dumps(
                    {
                        "version": 1,
                        "capability": "a" * 64,
                        "method": "health",
                        "params": {},
                    }
                )
                + "\n"
            ).encode()
        )

    assert exc.value.code == "BRIDGE_PROTOCOL_INVALID"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"version": 2, "method": "health", "params": {}},
        {"version": 1, "method": "", "params": {}},
        {"version": 1, "method": "health", "params": []},
        {"version": 1, "method": "health", "params": {}, "extra": True},
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
