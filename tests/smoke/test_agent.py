"""Top-level agent composition + CLI entrypoint tests.

``Agent`` bolts every tier together — driver client, visual sensor, autonomy
guard, kill-switch, episodic memory, skill distillation. These tests drive the
real compiled driver (simulated backend) through the full product path, plus
one subprocess run of the actual CLI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Self

import pytest

from computeruse.agent import Agent, AgentConfig
from computeruse.cli import discover_app
from computeruse.orchestrator.client import ActuationClient, DriverRpcError
from computeruse.orchestrator.loop import MaxStepsError, StuckLoopError, WorkingState
from computeruse.orchestrator.schemas import AgentTurn, Finish, MouseClick
from computeruse.security.autonomy import AutonomyLevel, PermissionDeniedError
from computeruse.vision.ax import AXElement
from computeruse.vision.focus import FocusedWindow
from tests.smoke.conftest import DRIVER_BIN, REPO_ROOT, SOCKET_PATH


def _click(thought: str, sub_goal: str, x: int, y: int) -> AgentTurn:
    return AgentTurn(
        thought=thought,
        sub_goal=sub_goal,
        action=MouseClick(type="mouse_click", x=x, y=y),
    )


def _click_provider() -> Callable[[WorkingState], AgentTurn]:
    """Two clicks then finish — the distiller's minimum for a skill."""

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _click("first", "click the first thing", 100, 100)
        if state.step_index == 1:
            return _click("second", "click the second thing", 200, 200)
        return AgentTurn(
            thought="done",
            sub_goal="workflow complete",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    return provider


def _config(tmp_path: Path, *, goal: str = "open the menu", level: AutonomyLevel = AutonomyLevel.GUARDED) -> AgentConfig:
    return AgentConfig(
        goal=goal,
        app="Safari",
        provider=_click_provider(),
        socket_path=str(SOCKET_PATH),
        store_dir=tmp_path / "store",
        autonomy_level=level,
        enable_visual_verification=False,  # simulated driver never renders
        max_steps=10,
    )


def test_agent_runs_end_to_end_and_distills(tmp_path) -> None:
    result = Agent(_config(tmp_path)).run()
    assert [s.type for s in result.trajectory] == ["mouse_click", "mouse_click"]
    assert result.distilled is not None
    assert result.distilled.kind == "skill"
    assert result.distilled.definition is not None
    assert len(result.episodes) == 1
    assert len(result.skills) == 1
    assert result.state.last_error is None


def test_second_run_of_same_flow_is_duplicate(tmp_path) -> None:
    config = _config(tmp_path)
    first = Agent(config).run()
    second = Agent(config).run()
    assert first.distilled is not None and first.distilled.kind == "skill"
    assert second.distilled is not None and second.distilled.kind == "duplicate"
    # Memory accumulated: two episodes, but only one skill in the store.
    assert len(second.episodes) == 2
    assert len(second.skills) == 1


def test_guard_blocks_destructive_at_observer(tmp_path) -> None:
    def destructive_provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _click("cleanup", "delete the files", 10, 10)
        return AgentTurn(
            thought="done",
            sub_goal="workflow complete",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    config = AgentConfig(
        goal="cleanup",
        app="Finder",
        provider=destructive_provider,
        socket_path=str(SOCKET_PATH),
        store_dir=tmp_path / "store",
        autonomy_level=AutonomyLevel.OBSERVER,  # never acts
        enable_visual_verification=False,
        max_steps=5,
    )
    with pytest.raises(PermissionDeniedError):
        Agent(config).run()


def test_simulated_driver_verification_catches_miss(tmp_path) -> None:
    """A run against a driver that cannot render must fail loudly — either the
    pixels say the click never landed, or (first, under verify) the coordinate
    is rejected as outside the observed display. The honest answer is a
    failure, never a phantom success."""
    config = AgentConfig(
        goal="click",
        app="Safari",
        provider=_click_provider(),
        socket_path=str(SOCKET_PATH),
        store_dir=tmp_path / "store2",
        autonomy_level=AutonomyLevel.GUARDED,
        enable_visual_verification=True,
        max_steps=10,
    )
    result = Agent(config).run()
    assert result.state.last_error is not None
    assert (
        "VisualVerificationFailedError" in result.state.last_error
        or "outside the observed" in result.state.last_error
    )
    # Nothing executed, so nothing was learned from a phantom run.
    assert result.trajectory == ()
    assert result.distilled is None
    assert result.episodes == ()


class _SensorDeadClient:
    """Driver client whose screen sensor refuses (Screen Recording consent).

    Mimics the real driver's behaviour when consent is missing: perception
    probes (focused window, AX tree) work, but every capture is refused — the
    exact condition a user hits on first real run.
    """

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def focused_window(self) -> FocusedWindow:
        return FocusedWindow(
            pid=4242,
            app_name="Safari",
            window_title="GitHub",
            cursor_x=0.0,
            cursor_y=0.0,
        )

    def capture(self) -> None:
        raise DriverRpcError("screenshot", "Screen Recording consent required")

    def hotkey_state(self) -> bool:
        return False

    def ax_snapshot(self, pid: int, max_depth: int) -> AXElement:
        return AXElement(role="Window", title="GitHub")

    def send(self, _action: object) -> None:
        return None


def test_visual_verification_fails_fast_on_dead_sensor(monkeypatch, tmp_path) -> None:
    """With --verify, a sensor that cannot capture aborts BEFORE the loop runs.

    Without this, every click would grind into the same refusal again and
    again (noisy retry loop) instead of one actionable setup error.
    """
    fake = _SensorDeadClient()
    monkeypatch.setattr("computeruse.agent.ActuationClient", lambda *_a, **_k: fake)
    provider_calls: list[int] = []

    def provider(state: WorkingState) -> AgentTurn:
        provider_calls.append(state.step_index)
        return AgentTurn(
            thought="t",
            sub_goal="click",
            action=MouseClick(type="mouse_click", x=1, y=1),
        )

    config = AgentConfig(
        goal="click",
        app="Safari",
        provider=provider,
        socket_path="/unused",
        store_dir=tmp_path / "store",
        enable_visual_verification=True,
        max_steps=10,
    )
    with pytest.raises(RuntimeError, match="Screen Recording consent"):
        Agent(config).run()
    assert provider_calls == [], "the OODA loop must not run when the sensor is dead"


def test_verification_disabled_tolerates_dead_sensor(monkeypatch, tmp_path) -> None:
    """Without --verify the same dead sensor does not abort the run."""
    fake = _SensorDeadClient()
    monkeypatch.setattr("computeruse.agent.ActuationClient", lambda *_a, **_k: fake)

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click",
                action=MouseClick(type="mouse_click", x=10, y=10),
            )
        return AgentTurn(
            thought="done",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    config = AgentConfig(
        goal="click",
        app="Safari",
        provider=provider,
        socket_path="/unused",
        store_dir=tmp_path / "store",
        enable_visual_verification=False,
        enable_vision=False,
        max_steps=10,
    )
    result = Agent(config).run()
    assert result.state.last_error is None
    assert [a.type for a in result.trajectory] == ["mouse_click"]


def test_discover_app_warning_includes_reason(capsys) -> None:
    """App discovery degrades to 'unknown' but names the real reason."""
    app = discover_app("/nonexistent-computeruse-socket.sock")
    assert app == "unknown"
    warning = capsys.readouterr().err
    assert "cannot reach driver" in warning


def test_capture_surfaces_driver_message(monkeypatch) -> None:
    """A driver-side screenshot refusal must carry the driver's own message."""
    client = ActuationClient("/unused", connect_retries=1)
    monkeypatch.setattr(
        client,
        "request",
        lambda _method, _params: {"ok": "error", "message": "Screen Recording consent required"},
    )
    with pytest.raises(DriverRpcError, match="Screen Recording consent required"):
        client.capture()


def test_client_activate_app_surfaces_driver_message(monkeypatch) -> None:
    """An unresolvable app name must carry the driver's own message."""
    client = ActuationClient("/unused", connect_retries=1)
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_request(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        return {"ok": "error", "message": "cannot activate app 'Nope': `open -a` exited with Some(1)"}

    monkeypatch.setattr(client, "request", fake_request)
    with pytest.raises(DriverRpcError, match="open -a"):
        client.activate_app("Nope")
    # Wire shape: the method and the exact params key the driver protocol
    # expects (contract drift test enforces the Rust side).
    assert calls == [("activate_app", {"app": "Nope"})]


class _RecordingClient:
    """Driver client that records the order of perception/actuation calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def activate_app(self, app: str) -> None:
        self.calls.append(f"activate:{app}")

    def focused_window(self) -> FocusedWindow:
        self.calls.append("focused_window")
        return FocusedWindow(
            pid=4242,
            app_name="Safari",
            window_title="GitHub",
            cursor_x=0.0,
            cursor_y=0.0,
        )

    def capture(self) -> None:
        self.calls.append("capture")

    def hotkey_state(self) -> bool:
        self.calls.append("hotkey_state")
        return False

    def ax_snapshot(self, pid: int, max_depth: int) -> AXElement:
        self.calls.append(f"ax:{pid}")
        return AXElement(role="Window", title="GitHub")

    def send(self, _action: object) -> None:
        self.calls.append("send")


def _activate_config(tmp_path: Path, *, app: str, activate: bool) -> AgentConfig:
    return AgentConfig(
        goal="click",
        app=app,
        provider=_click_provider(),
        socket_path="/unused",
        store_dir=tmp_path / "store",
        enable_visual_verification=False,
        activate_app_on_start=activate,
        max_steps=10,
    )


def test_activate_app_on_start_brings_named_app_forward(monkeypatch, tmp_path) -> None:
    """With activation enabled, the named app is brought forward first."""
    fake = _RecordingClient()
    monkeypatch.setattr("computeruse.agent.ActuationClient", lambda *_a, **_k: fake)
    Agent(_activate_config(tmp_path, app="Google Chrome", activate=True)).run()
    # Activation must precede every perception probe: the focused window the
    # run grounds against is the app the caller asked for, not the launcher.
    assert fake.calls[0] == "activate:Google Chrome"
    assert "focused_window" in fake.calls


def test_activate_app_off_by_default(monkeypatch, tmp_path) -> None:
    """Default config never touches the host for activation (Law 1)."""
    fake = _RecordingClient()
    monkeypatch.setattr("computeruse.agent.ActuationClient", lambda *_a, **_k: fake)
    Agent(_activate_config(tmp_path, app="Safari", activate=False)).run()
    assert not any(call.startswith("activate:") for call in fake.calls)


def test_activate_failure_aborts_cleanly(monkeypatch, tmp_path) -> None:
    """An explicit app that cannot be activated aborts before the loop."""

    class _RefusingClient(_RecordingClient):
        def activate_app(self, app: str) -> None:
            raise DriverRpcError("activate_app", "`open -a` exited with Some(1)")

    fake = _RefusingClient()
    monkeypatch.setattr("computeruse.agent.ActuationClient", lambda *_a, **_k: fake)
    config = _activate_config(tmp_path, app="Nope", activate=True)
    with pytest.raises(RuntimeError, match="cannot activate app 'Nope'"):
        Agent(config).run()
    # The refusal is a setup error: the loop must never run blind.
    assert fake.calls == [], "no perception or actuation may run after a failed activation"


def test_cli_surfaces_stuck_loop_and_max_steps(monkeypatch, capsys) -> None:
    """CLI turns the loop's typed termination errors into one clean line."""
    from computeruse.cli import main

    calls = {"n": 0}

    def fake_agent(_config: object) -> object:
        class _Agent:
            def run(self) -> object:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise StuckLoopError(
                        action=MouseClick(type="mouse_click", x=1, y=1),
                        repeats=5,
                        goal="click",
                    )
                raise MaxStepsError(steps=10, goal="click")

        return _Agent()

    monkeypatch.setattr("computeruse.cli.Agent", fake_agent)
    assert main(["--goal", "click"]) == 1
    assert "stuck loop" in capsys.readouterr().err
    assert main(["--goal", "click"]) == 1
    assert "max steps" in capsys.readouterr().err


def test_cli_activates_named_app_only_with_real() -> None:
    """CLI wiring: activation is on only for a named app on a real backend."""
    from computeruse.cli import build_config, parse_args

    named_real = build_config(
        parse_args(["--goal", "click", "--app", "Google Chrome", "--real"]),
        goal="click",
        activate_named_app=True,
    )
    assert named_real.activate_app_on_start is True
    named_sim = build_config(
        parse_args(["--goal", "click", "--app", "Safari"]),
        goal="click",
        activate_named_app=False,
    )
    assert named_sim.activate_app_on_start is False
    # An auto-discovered app (args.app None) is never activated even when the
    # caller asks for activation: discovery already names the frontmost app.
    auto = build_config(
        parse_args(["--goal", "click"]),
        goal="click",
        activate_named_app=True,
    )
    assert auto.activate_app_on_start is False


def test_cli_runs_end_to_end(tmp_path) -> None:
    """The real CLI: spawns the driver, runs the demo, distills + remembers."""
    if not DRIVER_BIN.exists():
        pytest.skip("actuation-driver not built; run `cargo build` in driver/")
    # Unix socket paths are length-limited (~104 chars on macOS); pytest tmp
    # dirs are deep, so use a short fixed path under /tmp for the driver.
    socket_path = Path("/tmp/computeruse-cli-test.sock")
    store_dir = tmp_path / "store"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    run = subprocess.run(  # noqa: PLW1510 - manual returncode assert gives a richer failure message
        [
            sys.executable,
            "-m",
            "computeruse",
            "--goal",
            "demo task",
            "--driver",
            str(DRIVER_BIN),
            "--socket",
            str(socket_path),
            "--store",
            str(store_dir),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, f"CLI failed:\n{run.stdout}\n{run.stderr}"
    assert "distill     : skill" in run.stdout
    assert len(list((store_dir / "skills").glob("*.json"))) == 1
    assert len(list((store_dir / "episodes").glob("*.json"))) == 1
