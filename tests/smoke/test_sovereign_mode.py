"""Sovereign autonomy contract.

These regressions keep Sovereign a distinct operator-delegated permission mode
without weakening the existing FULL safety boundary.
"""

from __future__ import annotations

from computeruse.security.autonomy import AutonomyLevel, Risk, decide_permission
from computeruse.security.permissions import PermissionDecision


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
