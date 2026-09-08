"""Owned-driver client with kernel-attested Unix-socket server identity."""

from __future__ import annotations

import os
import socket
import struct
import sys
from collections.abc import Callable

from computeruse.orchestrator.client import ActuationClient

DARWIN_SOL_LOCAL = 0
DARWIN_LOCAL_PEERPID = 2


def _peer_pid(sock: socket.socket) -> int | None:
    """Return the connected Unix peer PID on Linux/macOS, failing closed."""
    try:
        if sys.platform.startswith("linux"):
            peercred = getattr(socket, "SO_PEERCRED", None)
            if peercred is None:
                return None
            raw = sock.getsockopt(socket.SOL_SOCKET, peercred, struct.calcsize("3i"))
            pid, uid, _gid = struct.unpack("3i", raw)
            if uid != os.geteuid():
                return None
            return pid if pid > 0 else None

        if sys.platform == "darwin":
            raw = sock.getsockopt(
                DARWIN_SOL_LOCAL,
                DARWIN_LOCAL_PEERPID,
                struct.calcsize("i"),
            )
            (pid,) = struct.unpack("i", raw)
            return pid if pid > 0 else None
    except (OSError, struct.error, TypeError, ValueError):
        return None
    return None


class OwnedActuationClient(ActuationClient):
    """Actuation client pinned to the exact Rust child process it owns."""

    def __init__(
        self,
        socket_path: str,
        *,
        expected_server_pid: int,
        connect_retries: int = 3,
        retry_delay_seconds: float = 0.2,
        recv_timeout_seconds: float = 10.0,
        recover: Callable[[], None] | None = None,
        recover_unresponsive: Callable[[], None] | None = None,
    ) -> None:
        if isinstance(expected_server_pid, bool) or expected_server_pid <= 0:
            raise ValueError("expected_server_pid must be a positive integer")
        super().__init__(
            socket_path,
            connect_retries=connect_retries,
            retry_delay_seconds=retry_delay_seconds,
            recv_timeout_seconds=recv_timeout_seconds,
            recover=recover,
            recover_unresponsive=recover_unresponsive,
        )
        self._expected_server_pid = expected_server_pid

    def _connect_once(self) -> socket.socket:
        sock = super()._connect_once()
        try:
            peer_pid = _peer_pid(sock)
            if peer_pid != self._expected_server_pid:
                raise OSError("driver Unix socket peer PID did not match owned child")
            return sock
        except BaseException:
            sock.close()
            raise
