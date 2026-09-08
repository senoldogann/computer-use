"""Versioned, bounded wire contract for the local computer-use bridge."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 65_536
CAPABILITY_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class BridgeProtocolError(ValueError):
    """A bridge frame could not be validated safely."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "BRIDGE_PROTOCOL_INVALID"


@dataclass(frozen=True)
class BridgeRequest:
    """One authenticated bridge request."""

    version: int
    capability: str
    method: str
    params: dict[str, object]


def parse_request_line(raw: bytes) -> BridgeRequest:
    """Parse exactly one bounded newline-delimited request frame."""
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise BridgeProtocolError("bridge request exceeds the frame limit")
    if not raw.endswith(b"\n"):
        raise BridgeProtocolError("bridge request must end with a newline")
    try:
        decoded = raw[:-1].decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeProtocolError("bridge request is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise BridgeProtocolError("bridge request must be a JSON object")

    version = payload.get("version")
    capability = payload.get("capability")
    method = payload.get("method")
    params = payload.get("params")
    if version != PROTOCOL_VERSION:
        raise BridgeProtocolError("unsupported bridge protocol version")
    if not isinstance(capability, str) or CAPABILITY_PATTERN.fullmatch(capability) is None:
        raise BridgeProtocolError("bridge capability must be 64 lowercase hex characters")
    if not isinstance(method, str) or not method.strip():
        raise BridgeProtocolError("bridge method must be a non-empty string")
    if not isinstance(params, dict) or not all(isinstance(key, str) for key in params):
        raise BridgeProtocolError("bridge params must be a string-keyed object")

    return BridgeRequest(
        version=PROTOCOL_VERSION,
        capability=capability,
        method=method,
        params=dict(params),
    )


def _encode(payload: object) -> bytes:
    return (json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def encode_success(result: object) -> bytes:
    """Encode one successful bridge response."""
    return _encode({"ok": True, "result": result})


def encode_error(code: str, message: str) -> bytes:
    """Encode one categorical bridge error without diagnostic internals."""
    return _encode({"ok": False, "error": {"code": code, "message": message}})
