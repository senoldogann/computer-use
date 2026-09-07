"""P0 safety regression: trust mode must not erase destructive boundaries.

`--yes` is useful for routine confirmations, but a global unattended switch
must not silently become a standing grant for deletion, shell destruction,
purchases, sends, installs, or other destructive actions. Scoped grants and
single-use approvals already exist for those cases.
"""

from __future__ import annotations

from computeruse.agent import guarded
from computeruse.orchestrator.loop import EMPTY_OBSERVATION
from computeruse.orchestrator.schemas import AgentTurn, ClipboardPaste, PressHotkey
from computeruse.security.autonomy import AutonomyLevel
from computeruse.security.grants import GrantVerdict
from computeruse.security.permissions import PermissionDecision


def _destructive_paste() -> AgentTurn:
    return AgentTurn(
        thought="cleanup",
        sub_goal="clean up temporary files",
        action=ClipboardPaste(type="clipboard_paste", text="rm -rf ~/important-data"),
    )


def test_trust_mode_cannot_auto_approve_destructive_action() -> None:
    """FULL + --yes still needs a human/scoped grant for destructive work."""
    guard = guarded(AutonomyLevel.FULL, authorize=None, auto_approve=True)

    assert guard(_destructive_paste(), EMPTY_OBSERVATION) is PermissionDecision.CONFIRM


def test_trust_mode_still_auto_approves_routine_confirmation() -> None:
    """The fix must preserve the useful meaning of --yes for routine work."""
    guard = guarded(AutonomyLevel.GUARDED, authorize=None, auto_approve=True)
    turn = AgentTurn(
        thought="confirm",
        sub_goal="confirm dialog",
        action=PressHotkey(type="press_hotkey", modifiers=[], key="enter"),
    )

    assert guard(turn, EMPTY_OBSERVATION) is PermissionDecision.ALLOW


def test_scoped_grant_still_authorizes_destructive_action() -> None:
    """The boundary does not invalidate authority the user delegated explicitly."""
    verdict = GrantVerdict(
        outcome="granted",
        grant_id="delete-temp-once",
        reason="explicit scoped capability grant",
    )
    guard = guarded(
        AutonomyLevel.FULL,
        authorize=lambda _turn, _label: verdict,
        auto_approve=True,
    )

    assert guard(_destructive_paste(), EMPTY_OBSERVATION) is PermissionDecision.ALLOW


def test_observer_remains_blocked_even_in_trust_mode() -> None:
    """Trust mode can never turn a hard BLOCK into permission."""
    guard = guarded(AutonomyLevel.OBSERVER, authorize=None, auto_approve=True)

    assert guard(_destructive_paste(), EMPTY_OBSERVATION) is PermissionDecision.BLOCK
