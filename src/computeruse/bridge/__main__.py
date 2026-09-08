"""Run the deterministic local computer-use bridge."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from computeruse.bridge.server import (
    MAX_STARTUP_BYTES,
    BridgeServer,
    BridgeServerError,
    OwnedDriver,
    install_termination_handler,
    parse_startup_line,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m computeruse.bridge")
    parser.add_argument("--socket", required=True, help="Absolute private bridge Unix socket")
    parser.add_argument("--driver", required=True, help="Absolute actuation-driver executable")
    parser.add_argument(
        "--real",
        action="store_true",
        help="Use the real macOS backend instead of the deterministic simulated backend",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    socket_path = Path(args.socket)
    driver_path = Path(args.driver)
    if not socket_path.is_absolute() or not driver_path.is_absolute():
        print("[computeruse-bridge] socket and driver paths must be absolute", file=sys.stderr)
        return 2

    try:
        startup_raw = sys.stdin.buffer.readline(MAX_STARTUP_BYTES + 1)
        startup = parse_startup_line(startup_raw)
        install_termination_handler()
        owned = OwnedDriver.start(
            driver_path=driver_path,
            runtime_parent=socket_path.parent / "driver-runtime",
            real=bool(args.real),
        )
    except BridgeServerError as exc:
        print(f"[computeruse-bridge] {exc.code}: {exc}", file=sys.stderr)
        return 2

    server = BridgeServer(socket_path, startup.capability, owned.controller)
    try:
        server.bind()
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except BridgeServerError as exc:
        print(f"[computeruse-bridge] {exc.code}: {exc}", file=sys.stderr)
        return 2
    finally:
        server.close()
        owned.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
