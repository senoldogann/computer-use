"""Run the deterministic local computer-use bridge."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from computeruse.bridge.server import (
    BridgeServerError,
    BridgeStdioServer,
    OwnedDriver,
    install_termination_handler,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m computeruse.bridge")
    parser.add_argument("--driver", required=True, help="Absolute actuation-driver executable")
    parser.add_argument("--runtime", required=True, help="Absolute private driver runtime directory")
    parser.add_argument(
        "--real",
        action="store_true",
        help="Use the real macOS backend instead of the deterministic simulated backend",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    driver_path = Path(args.driver)
    runtime_path = Path(args.runtime)
    if not driver_path.is_absolute() or not runtime_path.is_absolute():
        print("[computeruse-bridge] driver and runtime paths must be absolute", file=sys.stderr)
        return 2

    try:
        install_termination_handler()
        owned = OwnedDriver.start(
            driver_path=driver_path,
            runtime_parent=runtime_path,
            real=bool(args.real),
        )
    except BridgeServerError as exc:
        print(f"[computeruse-bridge] {exc.code}: {exc}", file=sys.stderr)
        return 2

    server = BridgeStdioServer(sys.stdin.buffer, sys.stdout.buffer, owned.controller)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except BridgeServerError as exc:
        print(f"[computeruse-bridge] {exc.code}: {exc}", file=sys.stderr)
        return 2
    finally:
        owned.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
