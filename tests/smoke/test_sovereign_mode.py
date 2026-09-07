"""Sovereign autonomy contract.

These regressions keep Sovereign a distinct operator-delegated permission mode
without weakening the existing FULL safety boundary.
"""

from __future__ import annotations

import pytest

from computeruse import cli
from computeruse.agent import guarded
from computeruse.orchestrator.loop import EMPTY_OBSERVATION
from computeruse.orchestrator.schemas import AgentTurn, ClipboardPaste
from computeruse.security.autonomy import AutonomyLevel, Risk, decide_permission
from computeruse.security.permissions import PermissionDecision


def _destructive_turn() -> AgentTurn:
    return AgentTurn(
        thought="cleanup",
        sub_goal="remove temporary data",
        action=ClipboardPaste(type="clipboard_paste", text="rm -rf ~/temporary-data"),
    )


def test_sovereign_is_a_distinct_internal_level() -> None:
    assert AutonomyLevel.SOVEREIGN.value == 4
    assert AutonomyLevel.SOVEREIGN is not AutonomyLevel.FULL


def test_sovereign_allows_every_classified_risk() -> None:
    for risk in Risk:
        assert (
            decide_permission(AutonomyLevel.SOVEREIGN, risk)
            is PermissionDecision.ALLOW
        )


def test_full_still_confirms_destructive_actions() -> None:
    assert (
        decide_permission(AutonomyLevel.FULL, Risk.DESTRUCTIVE)
        is PermissionDecision.CONFIRM
    )


def test_sovereign_does_not_require_grant_lookup() -> None:
    def unexpected_authorize(_turn: AgentTurn, _label: str | None):
        raise AssertionError("sovereign must not consult grants for permission")

    guard = guarded(
        AutonomyLevel.SOVEREIGN,
        authorize=unexpected_authorize,
        auto_approve=False,
    )

    assert guard(_destructive_turn(), EMPTY_OBSERVATION) is PermissionDecision.ALLOW


def test_sovereign_does_not_consume_single_use_approval() -> None:
    def unexpected_approval(_turn: AgentTurn, _label: str | None):
        raise AssertionError("sovereign must not consume queued approval")

    guard = guarded(
        AutonomyLevel.SOVEREIGN,
        authorize=None,
        auto_approve=False,
        consume_approval=unexpected_approval,
    )

    assert guard(_destructive_turn(), EMPTY_OBSERVATION) is PermissionDecision.ALLOW


def test_cli_requires_explicit_sovereign_switch() -> None:
    ordinary = cli.parse_args(["--goal", "x"])
    sovereign = cli.parse_args(
        ["--goal", "x", "--sovereign", "--deadline-seconds", "60"]
    )

    assert cli.resolve_autonomy_level(ordinary) is AutonomyLevel.FULL
    assert cli.resolve_autonomy_level(sovereign) is AutonomyLevel.SOVEREIGN


def test_numeric_level_does_not_expose_sovereign() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["--goal", "x", "--level", "4"])
