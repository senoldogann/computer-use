from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from computeruse.memory.episodic import EpisodicStore
from computeruse.orchestrator.mission import MissionStore, new_mission
from computeruse.security.approvals import ApprovalQueue

ROOT: Path = Path(__file__).resolve().parents[2]
OUT: Path = ROOT / "target/system-audit-20260905"
BIN: Path = ROOT / "driver/target/debug/actuation-driver"


def await_socket(path: Path, process: subprocess.Popen[bytes]) -> None:
    for attempt in range(100):
        if process.poll() is not None:
            raise RuntimeError(f"Driver exited: status={process.returncode}, socket={path}")
        if path.exists():
            return
        time_to_wait = 0.03
        import time
        time.sleep(time_to_wait)
    raise TimeoutError(f"Driver did not bind socket={path}")


def run_cli(argv: list[str], label: str, env: dict[str, str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [str(ROOT / ".venv/bin/computeruse"), *argv],
        env=env,
        text=True,
        capture_output=True,
        timeout=65,
        check=False,
    )
    (OUT / f"{label}.log").write_text(result.stdout + result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"CLI probe {label} failed: status={result.returncode}, stderr={result.stderr}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    env: dict[str, str] = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parent) + os.pathsep + str(ROOT / "src")
    with tempfile.TemporaryDirectory(prefix="cu-cli-") as tmp:
        directory: Path = Path(tmp)
        socket_path: Path = directory / "driver.sock"
        driver: subprocess.Popen[bytes] = subprocess.Popen(
            [str(BIN), str(socket_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            await_socket(socket_path, driver)
            store: Path = directory / "approvals-case"
            missions: MissionStore = MissionStore(store / "missions")
            mission = new_mission(
                goal="remove the disposable placeholder",
                app="Safari",
                plan=None,
                now=datetime.now(UTC) - timedelta(hours=1),
            )
            missions.save(mission)
            argv: list[str] = [
                "--socket", str(socket_path),
                "--store", str(store),
                "--autonomous", "1",
                "--idle-seconds", "0",
                "--rest-seconds", "0",
                "--deadline-seconds", "60",
                "--no-vision",
                "--level", "3",
                "--provider", "audit_provider:propose_delete",
            ]
            run_cli(argv, "approval-first-run", env)
            queue: ApprovalQueue = ApprovalQueue(store / "approvals")
            first = queue.requests()[0]
            run_cli(["--store", str(store), "--approve", first.request_id], "approval-human-answer", env)
            run_cli(argv, "approval-second-run", env)
            print(
                json.dumps({
                    "probe": "approved_action_reparked",
                    "requests": [
                        {
                            "decision": request.decision,
                            "action": request.action if isinstance(request.action, dict) else request.action.model_dump(),
                            "mission": request.mission_id,
                        }
                        for request in queue.requests()
                    ],
                    "missions": [
                        {"id": item.mission_id, "status": item.status, "attempts": item.attempts}
                        for item in missions.missions()
                    ],
                }),
                flush=True,
            )

            failure_store: Path = directory / "failed-finish-case"
            failure_missions: MissionStore = MissionStore(failure_store / "missions")
            original = new_mission(
                goal="audit an explicitly failed workflow",
                app="Safari",
                plan=None,
                now=datetime.now(UTC) - timedelta(hours=1),
            )
            original = original.model_copy(update={"status": "failed", "attempts": 2})
            failure_missions.save(original)
            run_cli(
                [
                    "--socket", str(socket_path),
                    "--store", str(failure_store),
                    "--autonomous", "2",
                    "--idle-seconds", "0",
                    "--rest-seconds", "0",
                    "--deadline-seconds", "60",
                    "--no-vision",
                    "--level", "3",
                    "--provider", "audit_provider:finish_failure",
                ],
                "failed-finish-session",
                env,
            )
            print(
                json.dumps({
                    "probe": "failed_finish_and_mission_identity",
                    "original_mission": original.mission_id,
                    "missions": [
                        {"id": item.mission_id, "status": item.status, "attempts": item.attempts}
                        for item in failure_missions.missions()
                    ],
                    "episode_outcomes": [
                        episode.outcome for episode in EpisodicStore(failure_store / "episodes").episodes()
                    ],
                }),
                flush=True,
            )
        finally:
            driver.terminate()
            driver.wait(timeout=5)


if __name__ == "__main__":
    main()
