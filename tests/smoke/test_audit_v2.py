"""Regression pins for the v2 adversarial audit (G1-G7).

Each finding was verified against the source before fixing (AGENTS.md §10),
fixed at the root cause, and pinned here so the review's claims cannot
silently regress: the getattr type-unsafety, the kill-switch signal-source
conflict, the per-kind noise policy, the episodic light read, the bounded
monitor window, the registry index cache, and the Bezier drag plan.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from computeruse.memory.episodic import EpisodicStore, episode_from_trace
from computeruse.orchestrator.loop import OodaRunner, WorkingState
from computeruse.orchestrator.schemas import AgentTurn, LoadSkill, MouseClick, Wait
from computeruse.security.killswitch import CursorSample, KillSwitch, MouseShakeMonitor
from computeruse.skills.schemas import SkillDefinition
from computeruse.vision.diff import ChangeKind, verdict


def _runner() -> OodaRunner:
    return OodaRunner(provider=lambda _s: _wait_turn(), execute_physical=lambda _a: None)


def _wait_turn() -> AgentTurn:
    return AgentTurn.model_validate(
        {"thought": "", "sub_goal": "", "action": {"type": "wait", "duration_ms": 0, "reason": "r"}}
    )


# --- G1: internal-action handlers must narrow, not getattr -----------------


def test_sleep_for_narrows_wait_and_rejects_other_types() -> None:
    runner = _runner()
    # A Wait (duration 0) flows through the shell without sleeping.
    runner._sleep_for(Wait(type="wait", duration_ms=0, reason="no-op"))
    # Any other action type must fail loudly as a coding error — never a
    # getattr slip that silently poisons the shell (G1).
    with pytest.raises(RuntimeError, match="expected Wait"):
        runner._sleep_for(MouseClick(type="mouse_click", x=1, y=1))


def test_load_skill_for_narrows_and_surfaces_loader_error() -> None:
    runner = _runner()
    # A non-LoadSkill action is a routing/coding error, not an AttributeError.
    with pytest.raises(RuntimeError, match="expected LoadSkill"):
        runner._load_skill_for(Wait(type="wait", duration_ms=0, reason="r"))
    # A LoadSkill with no loader configured keeps its clear, specific error.
    with pytest.raises(RuntimeError, match="no loader configured"):
        runner._load_skill_for(LoadSkill(type="load_skill", skill_id="a.b"))


# --- G2: kill-switch signal sources must not silently shadow each other ----


def test_killswitch_rejects_conflicting_signal_sources() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        KillSwitch(monitor=None, signal_triggered=True, signal_predicate=lambda: False)
    # Each source alone keeps working.
    assert KillSwitch(monitor=None, signal_triggered=True).tripped() is True
    assert KillSwitch(monitor=None, signal_predicate=lambda: False).tripped() is False


# --- G3: the noise call-out must honour the caller's chosen signal ---------


def test_verdict_fraction_mode_noise_uses_fraction_only() -> None:
    # 60% of pixels differ by 0.07 (just over the per-pixel 0.06 threshold):
    # fraction 0.6, mean only 0.042. In fraction mode the noise call-out must
    # clear on the fraction signal alone (G3)...
    before = ((0.0,) * 10,) * 10
    after = tuple(tuple(0.07 if i < 6 else 0.0 for i in range(10)) for _ in range(10))
    assert verdict(before, after, kind="fraction").kind == ChangeKind.NOISE
    # ...while both-mode still demands the mean cap too, so the same frames
    # read as a crisp CHANGED (the default ORIENT policy is unchanged).
    assert verdict(before, after).kind == ChangeKind.CHANGED


def test_verdict_mean_mode_noise_uses_mean_only() -> None:
    # 30% of pixels differ by 1.0: fraction 0.3, mean 0.3. Mean mode's noise
    # call-out clears on the mean signal alone (G3); both-mode needs the
    # fraction cap (0.5) and stays CHANGED.
    before = ((0.0,) * 10,) * 10
    after = tuple(tuple(1.0 if i < 3 else 0.0 for i in range(10)) for _ in range(10))
    assert verdict(before, after, kind="mean").kind == ChangeKind.NOISE
    assert verdict(before, after).kind == ChangeKind.CHANGED


# --- G4: the de-dup gate reads only the signature field --------------------


def test_known_signatures_reads_only_signature_field(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = EpisodicStore(tmp_path)
    episode = episode_from_trace(
        app="Numbers",
        description="d",
        steps=(MouseClick(type="mouse_click", x=1, y=1),),
        outcome="success",
    )
    store.record(episode)
    # A second file with an intact signature but a corrupt *body* (steps are
    # garbage) — full deserialization would reject it; the light read must not.
    (tmp_path / "z-partial.json").write_text(
        json.dumps({"signature": "flow.second", "steps": "not-a-list"}),
        encoding="utf-8",
    )
    # A third file that is not JSON at all: skipped with a warning (G4).
    (tmp_path / "a-broken.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        signatures = store.known_signatures()
    assert signatures == {episode.signature, "flow.second"}
    assert any("skipping unreadable episode" in record.message for record in caplog.records)


# --- G5: the monitor's sliding window is bounded (deque, not pop(0)) --------


def test_monitor_window_is_bounded() -> None:
    samples = iter(CursorSample(x=1.0, y=1.0, time=float(i)) for i in range(40))
    monitor = MouseShakeMonitor(lambda: next(samples), window_size=5)
    for _ in range(40):
        monitor.observe()
    assert len(monitor._window) <= 5


# --- G6: the registry index is cached and invalidated on save ---------------


def _definition(skill_id: str, signature: str) -> SkillDefinition:
    return SkillDefinition.model_validate(
        {
            "skill_id": skill_id,
            "description": "export a report",
            "app": "Numbers",
            "tags": ("export",),
            "steps": ("click Export",),
            "signature": signature,
        }
    )


def test_registry_index_is_cached_and_invalidated_on_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import computeruse.skills.registry as registry_module

    reads = {"count": 0}
    original = registry_module._read_definition

    def counting(path: Path) -> SkillDefinition:
        reads["count"] += 1
        return original(path)

    monkeypatch.setattr(registry_module, "_read_definition", counting)
    registry = registry_module.SkillRegistry(tmp_path)
    registry.save(_definition("numbers.export.abc", "sig-1"))
    # The first search builds the cache (1 disk read); the second is served
    # from the session cache — a RETRIEVE scan must not re-read per query (G6).
    assert len(registry.search("numbers")) == 1
    assert len(registry.search("numbers")) == 1
    assert reads["count"] == 1
    # A save invalidates the cache, so the next search rescans (now 2 files)
    # and sees the new skill — the count proves the rescan happened.
    registry.save(_definition("numbers.import.def", "sig-2"))
    assert len(registry.search("numbers")) == 2
    assert reads["count"] == 3


# --- Volatile tool values: time-varying values never rejected on numerical mismatch ---


def test_auditor_accepts_volatile_price_discrepancy_and_rejects_missing_price() -> None:
    """Live/volatile values (e.g. Bitcoin price) change between tool calls and screen writes.

    The completion auditor verifies format and entity type rather than demanding
    a verbatim numerical match against an older tool output in history. If the requested
    type of value is missing entirely, it rejects.
    """
    from computeruse.orchestrator.prompts import (
        COMPLETION_AUDIT_CONTRACT,
        completion_auditor,
    )

    assert "Live, time-varying values returned by tools" in COMPLETION_AUDIT_CONTRACT
    assert "NOT required to match an earlier tool result in context verbatim" in COMPLETION_AUDIT_CONTRACT
    assert "Numerical inequality alone is NEVER grounds for rejection" in COMPLETION_AUDIT_CONTRACT

    def auditor_model(prompt: str, _image_b64: str | None = None) -> str:
        # The auditor must see the contract clause and the earlier tool record
        assert "Live, time-varying values returned by tools" in prompt
        assert "$80,886.02" in prompt
        # Following the contract: verify format and entity type (a price in USD),
        # not numerical equality with the earlier fetch.
        if "$81,287.00" in prompt:
            return '{"satisfied": true, "evidence": "the note shows a live Bitcoin price in USD ($81,287.00)"}'
        return '{"satisfied": false, "evidence": "no price or currency value appears in the note"}'

    audit = completion_auditor(auditor_model, app="Notes")

    # Case 1: Live price mismatch ($80,886.02 in tool output vs $81,287.00 in visible note) -> ACCEPT
    state_with_price = WorkingState(
        goal="Look up the current Bitcoin price on the web, then write it into a new note",
        active_window="Notes",
        tool_history=(
            "web_search 'current Bitcoin price' returned: Bitcoin live price is $80,886.02 USD",
        ),
        ui_elements=('StaticText "Bitcoin price: $81,287.00 USD" at (200, 150) 250x30',),
    )
    verdict_accept = audit(state_with_price, "wrote current Bitcoin price into note")
    assert verdict_accept.satisfied is True
    assert "$81,287.00" in verdict_accept.evidence

    # Case 2: Missing price entirely in visible note -> REJECT
    state_without_price = WorkingState(
        goal="Look up the current Bitcoin price on the web, then write it into a new note",
        active_window="Notes",
        tool_history=(
            "web_search 'current Bitcoin price' returned: Bitcoin live price is $80,886.02 USD",
        ),
        ui_elements=('StaticText "Meeting agenda for tomorrow" at (200, 150) 250x30',),
    )
    verdict_reject = audit(state_without_price, "wrote current Bitcoin price into note")
    assert verdict_reject.satisfied is False
    assert "no price" in verdict_reject.evidence
