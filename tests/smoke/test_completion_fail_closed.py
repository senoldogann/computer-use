"""P0 regressions: a configured completion gate must fail closed.

A completion auditor is useful only if its absence cannot become a green
result. These tests separate headless/test mode (no auditor configured) from a
configured auditor that fails, and require a non-empty evidence statement for
a positive verdict.
"""

from __future__ import annotations

import pytest

from computeruse.orchestrator.evidence import CompletionVerdict
from computeruse.orchestrator.loop import OodaRunner, WorkingState
from computeruse.orchestrator.prompts import InvalidDecisionError, parse_completion
from computeruse.orchestrator.schemas import AgentTurn, Finish, Wait
from computeruse.vision import ScreenCapture

_ONE_BY_ONE = ScreenCapture(
    display_id=0, width=1, height=1, scale=1.0, data=b"\x00\x00\x00\x00"
)


def _turn(action: object) -> AgentTurn:
    return AgentTurn.model_validate({"thought": "audit", "sub_goal": "audit", "action": action})


def _provider(state: WorkingState) -> AgentTurn:
    # Keep one real trajectory item so terminal outcome reaches on_complete.
    if state.step_index == 0:
        return _turn(Wait(type="wait", duration_ms=1, reason="settle"))
    return _turn(Finish(type="finish", status="success", summary="claimed done"))


def _run_with_auditor(
    auditor: object,
) -> tuple[list[tuple[str, bool, str | None]], OodaRunner]:
    received: list[tuple[str, bool, str | None]] = []
    runner = OodaRunner(
        provider=_provider,
        execute_physical=lambda _action: None,
        sensor=lambda: _ONE_BY_ONE,
        completion_check=auditor,  # type: ignore[arg-type]
        on_complete=lambda _t, outcome, retrospective, _skill, forced: received.append(
            (outcome, forced, retrospective)
        ),
        max_steps=8,
    )
    runner.run("do the thing")
    return received, runner


def test_configured_auditor_outage_cannot_accept_success() -> None:
    """Transport/parser failure is 'unverified', never a successful finish."""

    def unavailable(_state: WorkingState, _claim: str) -> CompletionVerdict:
        raise RuntimeError("audit transport unavailable")

    received, runner = _run_with_auditor(unavailable)

    assert len(received) == 1
    outcome, forced, retrospective = received[0]
    assert outcome == "failure"
    assert forced is True
    assert retrospective is not None
    assert "doğrulan" in retrospective.casefold() or "audit" in retrospective.casefold()
    assert runner._forced_finish is True


def test_blank_positive_evidence_cannot_verify_success() -> None:
    """A bare boolean from an injected checker is not completion evidence."""

    def empty_evidence(_state: WorkingState, _claim: str) -> CompletionVerdict:
        return CompletionVerdict(satisfied=True, evidence="   ")

    received, runner = _run_with_auditor(empty_evidence)

    assert len(received) == 1
    assert received[0][0] == "failure"
    assert received[0][1] is True
    assert runner._forced_finish is True


@pytest.mark.parametrize(
    "raw",
    (
        '{"satisfied": true}',
        '{"satisfied": false}',
        '{"satisfied": true, "evidence": ""}',
        '{"satisfied": true, "evidence": "   "}',
    ),
)
def test_completion_parser_requires_non_empty_evidence(raw: str) -> None:
    """Malformed evidence cannot be replaced with a synthetic placeholder."""
    with pytest.raises(InvalidDecisionError):
        parse_completion(raw)
