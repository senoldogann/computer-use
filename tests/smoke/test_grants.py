"""Capability grants (Law 5.1): authority delegated in advance, in bounds.

The guard asks about every destructive action and the queue makes "asked"
survive nobody being there — but neither delegates anything, so Level 3 still
stopped at every deletion. Removing the guard is not how that ceiling lifts.
People delegate the way they always do: narrowly, for a while, up to a count.

These tests pin what a grant may do, and — more importantly — the four things
it may not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from computeruse.orchestrator.schemas import (
    AgentTurn,
    CallTool,
    MouseClick,
    TypeText,
    action_from_payload,
)
from computeruse.security.autonomy import (
    AutonomyLevel,
    Risk,
    classify_risk,
    decide_permission,
)
from computeruse.security.grants import (
    ANY,
    CapabilityGrant,
    GrantStore,
    action_verbs,
    active_grants,
    authorize,
    decide_with_grant,
    new_grant,
    scope_matches,
    spent,
)
from computeruse.security.permissions import PermissionDecision

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
LABEL = "Delete Permanently"


def _turn(sub_goal: str = "clear the old exports") -> AgentTurn:
    return AgentTurn(
        thought="t", sub_goal=sub_goal, action=MouseClick(type="mouse_click", x=1, y=1)
    )


def _grant(**overrides: object) -> CapabilityGrant:
    base: dict[str, object] = {
        "verb": "delete",
        "app": "Finder",
        "target_pattern": "Delete*",
        "max_invocations": 2,
        "expires_at": NOW + timedelta(hours=1),
        "note": "weekly cleanup",
        "now": NOW,
    }
    base.update(overrides)
    return new_grant(**base)  # type: ignore[arg-type]


def _decide(
    grants: tuple[CapabilityGrant, ...],
    *,
    app: str = "Finder",
    label: str | None = LABEL,
    at: datetime = NOW,
    level: AutonomyLevel = AutonomyLevel.FULL,
) -> tuple[str, PermissionDecision]:
    turn = _turn()
    verdict = authorize(
        turn.action,
        sub_goal=turn.sub_goal,
        target_label=label,
        app=app,
        grants=grants,
        now=at,
    )
    risk = classify_risk(turn, target_label=label)
    return verdict.outcome, decide_with_grant(level, risk, verdict)


# --- what a grant does ------------------------------------------------------


def test_without_a_grant_a_destructive_action_still_asks() -> None:
    """The ceiling, before anything is delegated."""
    assert _decide(()) == ("not_covered", PermissionDecision.CONFIRM)


def test_a_matching_grant_turns_the_question_into_permission() -> None:
    assert _decide((_grant(),)) == ("granted", PermissionDecision.ALLOW)


def test_a_grant_matches_the_verb_across_languages() -> None:
    """A permission written in English covers a Turkish button.

    The families exist for this: the user's locale must not decide whether the
    authority they delegated applies.
    """
    outcome, decision = _decide((_grant(target_pattern=ANY),), label="Kalıcı Olarak Sil")
    assert (outcome, decision) == ("granted", PermissionDecision.ALLOW)


# --- what a grant does NOT do -----------------------------------------------


def test_a_grant_does_not_reach_into_another_application() -> None:
    assert _decide((_grant(),), app="Mail") == (
        "not_covered",
        PermissionDecision.CONFIRM,
    )


def test_a_grant_does_not_reach_another_control() -> None:
    assert _decide((_grant(),), label="Send Invoice") == (
        "not_covered",
        PermissionDecision.CONFIRM,
    )


def test_an_expired_grant_authorises_nothing() -> None:
    outcome, decision = _decide((_grant(),), at=NOW + timedelta(hours=2))
    assert outcome == "expired"
    assert decision is PermissionDecision.CONFIRM


def test_a_spent_grant_authorises_nothing() -> None:
    """``max_invocations: 2`` means two, and the count is real."""
    twice_used = spent(spent(_grant()))
    outcome, decision = _decide((twice_used,))
    assert outcome == "exhausted"
    assert decision is PermissionDecision.CONFIRM


def test_a_grant_never_applies_at_the_levels_that_exist_to_ask() -> None:
    """Observer never acts; Supervised exists to confirm every step.

    Honouring a grant there would not be delegation, it would be a bypass of
    the level the user selected.
    """
    for level, expected in (
        (AutonomyLevel.OBSERVER, PermissionDecision.BLOCK),
        (AutonomyLevel.SUPERVISED, PermissionDecision.CONFIRM),
    ):
        assert _decide((_grant(),), level=level)[1] is expected


def test_a_grant_never_covers_a_merely_routine_action() -> None:
    """Save/close/confirm are not what anyone delegates in advance.

    Letting a grant cover them would make a "delete" grant quietly broader
    than the words it was written with.
    """
    assert (
        decide_with_grant(AutonomyLevel.GUARDED, Risk.ROUTINE, None)
        is decide_permission(AutonomyLevel.GUARDED, Risk.ROUTINE)
    )


def test_a_grant_cannot_widen_something_already_allowed_or_blocked() -> None:
    """A grant only ever turns CONFIRM into ALLOW; both other answers pass through."""
    for level in AutonomyLevel:
        for risk in Risk:
            base = decide_permission(level, risk)
            if base is PermissionDecision.CONFIRM:
                continue
            assert decide_with_grant(level, risk, None) is base


def test_a_grant_with_a_named_control_ignores_an_unreadable_one() -> None:
    """A grant that named specific buttons must not fire on an unnamed click."""
    assert not scope_matches(_grant(), app="Finder", target_label=None)
    assert scope_matches(_grant(target_pattern=ANY), app="Finder", target_label=None)


def test_the_narrowest_live_grant_is_the_one_spent() -> None:
    """So revoking the specific grant gives the behaviour a person expects."""
    broad = _grant(app=ANY, target_pattern=ANY, note="everything")
    narrow = _grant(note="just the exports")
    turn = _turn()
    verdict = authorize(
        turn.action,
        sub_goal=turn.sub_goal,
        target_label=LABEL,
        app="Finder",
        grants=(broad, narrow),
        now=NOW,
    )
    assert verdict.grant_id == narrow.grant_id


def test_the_verdict_says_which_of_the_three_failures_it_was() -> None:
    """Expired, spent and never-covered need three different fixes."""
    _, _ = _decide((_grant(),))
    turn = _turn()

    def reason(grants: tuple[CapabilityGrant, ...], at: datetime) -> str:
        return authorize(
            turn.action,
            sub_goal=turn.sub_goal,
            target_label=LABEL,
            app="Finder",
            grants=grants,
            now=at,
        ).reason

    assert "expired" in reason((_grant(),), NOW + timedelta(hours=2))
    assert "spent" in reason((spent(spent(_grant())),), NOW)
    assert "no grant covers" in reason((), NOW)


# --- verbs ------------------------------------------------------------------


def test_a_verb_the_classifier_does_not_know_is_refused_at_mint_time() -> None:
    """A grant that matches nothing is worse than none: its author thinks they
    delegated something."""
    with pytest.raises(ValueError, match="not a grantable verb"):
        _grant(verb="frobnicate")


def test_a_shell_payload_is_its_own_family() -> None:
    """Delegating "run shell commands" is a different decision from "delete"."""
    action = TypeText(type="type_text", text="rm -rf ~/Downloads/old")
    assert action_verbs(action, sub_goal="clean up", target_label=None) == frozenset(
        {"shell"}
    )


def test_a_tool_calls_arguments_reach_the_shell_family() -> None:
    action = CallTool(
        type="call_tool", tool="bash", arguments={"command": "rm -rf /tmp/x"}
    )
    assert "shell" in action_verbs(action, sub_goal="tidy", target_label=None)


# --- macOS vocabulary -------------------------------------------------------


def test_the_macos_trash_buttons_are_destructive() -> None:
    """The platform's delete verb is not "delete", it is the Trash.

    "Move to Trash" and "Empty Trash" are the two most common destructive
    buttons on macOS and both classified as Risk.NONE, so an unattended run
    could empty the Trash without asking anyone.
    """
    turn = _turn("continue with the flow")
    for label in ("Move to Trash", "Empty Trash", "Çöp Kutusuna Taşı"):
        assert classify_risk(turn, target_label=label) is Risk.DESTRUCTIVE, label


def test_discarding_work_is_destroying_it() -> None:
    turn = _turn("continue with the flow")
    for label in ("Discard Changes", "Revert", "Reset"):
        assert classify_risk(turn, target_label=label) is Risk.DESTRUCTIVE, label


def test_ordinary_controls_are_still_not_flagged() -> None:
    """The vocabulary grew; the false-positive budget did not.

    Deliberately excluded: "clear" (Clear search field), "restore" (Restore
    from Backup) and "order" (Sort order) are common on benign controls, so
    "Clear History" is a known gap rather than an oversight.
    """
    turn = _turn("continue with the flow")
    for label in ("Clear search field", "Sort order", "Restore from Backup", "Open"):
        assert classify_risk(turn, target_label=label) is Risk.NONE, label


# --- the store --------------------------------------------------------------


def test_grants_round_trip_and_the_count_is_persisted(tmp_path: Path) -> None:
    store = GrantStore(tmp_path)
    grant = _grant()
    store.save(grant)
    assert store.grants() == (grant,)

    after = store.consume(grant.grant_id)
    assert after.remaining == 1
    assert store.grants()[0].used == 1

    store.consume(grant.grant_id)
    assert store.grants()[0].remaining == 0
    assert active_grants(store.grants(), NOW) == ()


def test_consuming_or_revoking_a_grant_that_is_not_there_raises(tmp_path: Path) -> None:
    """Silently succeeding would let a typo read as "revoked" while the real
    permission stayed live."""
    store = GrantStore(tmp_path)
    with pytest.raises(KeyError):
        store.consume("nope")
    with pytest.raises(KeyError):
        store.revoke("nope")


def test_one_corrupt_grant_does_not_hide_the_others(tmp_path: Path) -> None:
    store = GrantStore(tmp_path)
    good = _grant()
    store.save(good)
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert store.grants() == (good,)


def test_active_grants_hides_expired_and_spent_ones(tmp_path: Path) -> None:
    live = _grant(note="live")
    stale = _grant(expires_at=NOW - timedelta(hours=1), note="stale")
    used_up = spent(spent(_grant(note="used up")))
    assert active_grants((live, stale, used_up), NOW) == (live,)


# --- reading a parked action back -------------------------------------------


def test_a_stored_action_can_be_read_back_to_decide_what_it_delegates() -> None:
    """``--approve --always`` needs to know what the parked action *is*."""
    original = MouseClick(type="mouse_click", x=120, y=90, button="left")
    restored = action_from_payload(original.model_dump(exclude_none=True))
    assert restored == original


def test_an_unreadable_stored_action_delegates_nothing() -> None:
    """A queue entry nothing can parse is the one nothing should be inferred from."""
    assert action_from_payload({"type": "not_a_real_action"}) is None
    assert action_from_payload({"type": "mouse_click", "x": -1, "y": 0}) is None


def test_grant_matches_localized_app_alias() -> None:
    """A grant for "Hesap Makinesi" covers Calculator without asking.

    The operator delegates in their own locale; the frontmost app arrives in
    the system's. Both resolve to one canonical name before comparing, in
    both directions — while an unrelated app still does not match.
    """
    grant = _grant(app="Hesap Makinesi", target_pattern=ANY)
    assert _decide((grant,), app="Calculator", label="Sil") == (
        "granted",
        PermissionDecision.ALLOW,
    )
    mirrored = _grant(app="Calculator", target_pattern=ANY)
    assert _decide((mirrored,), app="hesap makinesi")[0] == "granted"
    assert scope_matches(grant, app="Finder", target_label="Sil") is False
