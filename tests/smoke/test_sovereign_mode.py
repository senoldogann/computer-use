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


def test_build_config_propagates_sovereign_level() -> None:
    args = cli.parse_args(
        ["--goal", "x", "--sovereign", "--deadline-seconds", "60"]
    )
    config = cli.build_config(args, goal="x", activate_named_app=False)

    assert config.autonomy_level is AutonomyLevel.SOVEREIGN


def test_numeric_level_does_not_expose_sovereign() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["--goal", "x", "--level", "4"])


def test_sovereign_requires_a_hard_budget(capsys: pytest.CaptureFixture[str]) -> None:
    args = cli.parse_args(["--goal", "x", "--sovereign"])

    assert cli._reject_unusable_arguments(args) == 2
    assert "--sovereign requires at least one" in capsys.readouterr().err


@pytest.mark.parametrize(
    "budget_args",
    [
        ["--deadline-seconds", "60"],
        ["--max-tokens", "1000"],
        ["--max-cost", "1.0"],
    ],
)
def test_sovereign_accepts_any_hard_budget(budget_args: list[str]) -> None:
    args = cli.parse_args(["--goal", "x", "--sovereign", *budget_args])

    assert cli._reject_unusable_arguments(args) is None


def test_sovereign_rejects_yes(capsys: pytest.CaptureFixture[str]) -> None:
    args = cli.parse_args(
        ["--goal", "x", "--sovereign", "--yes", "--deadline-seconds", "60"]
    )

    assert cli._reject_unusable_arguments(args) == 2
    assert "--sovereign is separate from --yes" in capsys.readouterr().err


@pytest.mark.parametrize("level", [0, 1, 2])
def test_sovereign_rejects_conflicting_lower_level(
    level: int, capsys: pytest.CaptureFixture[str]
) -> None:
    args = cli.parse_args(
        [
            "--goal",
            "x",
            "--sovereign",
            "--level",
            str(level),
            "--deadline-seconds",
            "60",
        ]
    )

    assert cli._reject_unusable_arguments(args) == 2
    assert "--sovereign cannot be combined with --level 0/1/2" in capsys.readouterr().err


def test_full_single_run_still_needs_no_hard_budget() -> None:
    args = cli.parse_args(["--goal", "x"])

    assert cli._reject_unusable_arguments(args) is None
