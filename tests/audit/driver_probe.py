from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from computeruse.orchestrator.client import ActuationClient, DriverTimeoutError
from computeruse.orchestrator.supervisor import DriverSupervisor

ROOT: Path = Path(__file__).resolve().parents[2]
OUT: Path = ROOT / "target/system-audit-20260905"
BIN: Path = ROOT / "driver/target/debug/actuation-driver"


def await_socket(path: Path, process: subprocess.Popen[bytes]) -> None:
    for attempt in range(100):
        if process.poll() is not None:
            raise RuntimeError(f"Driver exited: status={process.returncode}, socket={path}")
        if path.exists():
            return
        time.sleep(0.03)
    raise TimeoutError(f"Driver did not bind socket={path}")


def frozen_probe(directory: Path) -> None:
    socket_path: Path = directory / "sim.sock"
    processes: list[subprocess.Popen[bytes]] = []

    def spawn() -> subprocess.Popen[bytes]:
        socket_path.unlink(missing_ok=True)
        process: subprocess.Popen[bytes] = subprocess.Popen(
            [str(BIN), str(socket_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(process)
        await_socket(socket_path, process)
        return process

    first: subprocess.Popen[bytes] = spawn()
    supervisor: DriverSupervisor = DriverSupervisor(
        spawn, max_restarts=3, backoff_base_seconds=0.0, sleep=time.sleep,
    )
    supervisor.adopt(first)
    client: ActuationClient = ActuationClient(
        str(socket_path),
        connect_retries=3,
        retry_delay_seconds=0.01,
        recv_timeout_seconds=0.15,
        recover=supervisor.restart_unresponsive,
    )
    try:
        client.connect()
        print(json.dumps({"probe": "before_freeze", "response": client.request("ping")}))
        os.kill(first.pid, signal.SIGSTOP)
        for attempt in range(4):
            try:
                response: dict[str, object] = client.request("ping")
                print(json.dumps({"probe": "frozen", "attempt": attempt + 1, "response": response}))
            except DriverTimeoutError as exc:
                print(
                    json.dumps({
                        "probe": "frozen",
                        "attempt": attempt + 1,
                        "error": type(exc).__name__,
                        "restarts": supervisor.restarts_used,
                        "driver_alive": first.poll() is None,
                    })
                )
        os.kill(first.pid, signal.SIGCONT)
        client.close()
        first.kill()
        first.wait(timeout=3)
        client.connect()
        print(
            json.dumps({
                "probe": "after_sigkill",
                "response": client.request("ping"),
                "restarts": supervisor.restarts_used,
            })
        )
    finally:
        client.close()
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


def real_startup_probe(directory: Path) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    socket_path: Path = directory / "real.sock"
    with (OUT / "real-driver-stderr.log").open("wb") as stderr:
        process: subprocess.Popen[bytes] = subprocess.Popen(
            [str(BIN), str(socket_path), "--real"],
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
        try:
            await_socket(socket_path, process)
            with ActuationClient(str(socket_path), recv_timeout_seconds=2.0) as client:
                time.sleep(0.3)
                print(json.dumps({"probe": "real_health", "response": client.health()}))
                print(json.dumps({"probe": "real_hotkey_state", "tripped": client.hotkey_state()}))
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cu-audit-") as tmp:
        directory: Path = Path(tmp)
        frozen_probe(directory)
        real_startup_probe(directory)


if __name__ == "__main__":
    main()
