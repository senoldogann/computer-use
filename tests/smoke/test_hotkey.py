"""Law 5.2 global kill-hotkey tests.

The driver owns the host-side listener (a CGEventTap behind the ADR-1 socket);
the orchestrator polls it via the ``hotkey_state`` RPC and composes it into the
effective kill-switch. The simulated driver reports False (there is no real
event stream to listen to — Law 1), so the RPC shape, the signal composition,
and the agent wiring are what get pinned here; the key-matching rule itself is
pinned by the Rust unit tests in ``hotkey.rs``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from computeruse.agent import Agent, AgentConfig
from computeruse.orchestrator.client import ActuationClient
from computeruse.orchestrator.loop import KillSwitchTripped, WorkingState
from computeruse.orchestrator.schemas import AgentTurn, Finish, MouseClick
from computeruse.security.autonomy import AutonomyLevel
from computeruse.security.killswitch import KillSwitch
from tests.smoke.conftest import SOCKET_PATH, rpc_call


def test_hotkey_state_wire_shape() -> None:
    payload = rpc_call({"method": "hotkey_state"})
    assert payload.get("ok") == "hotkey_state"
    assert payload.get("tripped") is False


def test_hotkey_state_via_client() -> None:
    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        assert client.hotkey_state() is False


def test_with_signal_predicate_ors_sources() -> None:
    base = KillSwitch(monitor=None, signal_predicate=lambda: False)
    combined = base.with_signal_predicate(lambda: True)
    assert combined.tripped() is True
    # Composition is non-mutating: the original switch is untouched.
    assert base.tripped() is False
    # OR semantics from an empty base.
    assert KillSwitch(monitor=None).with_signal_predicate(lambda: True).tripped() is True
    assert KillSwitch(monitor=None).with_signal_predicate(lambda: False).tripped() is False


def test_with_signal_predicate_refuses_static_trip() -> None:
    static = KillSwitch(monitor=None, signal_triggered=True)
    with pytest.raises(ValueError, match="statically tripped"):
        static.with_signal_predicate(lambda: False)


def _provider() -> Callable[[WorkingState], AgentTurn]:
    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click",
                action=MouseClick(type="mouse_click", x=1, y=1),
            )
        return AgentTurn(
            thought="done",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    return provider


def _config(tmp_path: Path, **overrides: object) -> AgentConfig:
    defaults: dict[str, object] = {
        "goal": "g",
        "app": "Safari",
        "provider": _provider(),
        "socket_path": str(SOCKET_PATH),
        "store_dir": tmp_path / "store",
        "autonomy_level": AutonomyLevel.GUARDED,
        "enable_visual_verification": False,
        "max_steps": 5,
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def test_agent_honors_config_killswitch_with_hotkey_composed(tmp_path: Path) -> None:
    """The configured kill-switch still trips through the agent even with the\n    driver hotkey poll composed in (OR semantics — neither channel shadows the\n    other, G2)."""
    config = _config(
        tmp_path,
        kill_switch=KillSwitch(monitor=None, signal_predicate=lambda: True),
    )
    with pytest.raises(KillSwitchTripped):
        Agent(config).run()


def test_agent_runs_with_default_hotkey_channel(tmp_path: Path) -> None:
    """The default wiring (hotkey poll on) completes normally against the\n    simulated driver, which always reports False."""
    result = Agent(_config(tmp_path)).run()
    assert result.state.last_error is None
    assert result.distilled is not None
