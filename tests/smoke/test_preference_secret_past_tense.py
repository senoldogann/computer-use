"""Past-tense credential assignment regressions for preference memory (#77)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from computeruse.memory.preferences import (
    PreferenceEvidence,
    PreferenceStore,
    contains_sensitive_preference_material,
    extract_explicit_preference_evidence,
)


@pytest.mark.parametrize(
    "text",
    (
        "password manager entry was swordfish",
        "api key rotation value was ABCDEFGHIJKLMNOP",
        "token budget value was abcdefghijklmnop",
        "secret manager credentials were confidentialvalue",
    ),
)
def test_safe_prose_follower_cannot_hide_past_tense_assignment(text: str) -> None:
    assert contains_sensitive_preference_material(text) is True


def test_structured_preference_discards_past_tense_safe_follower_assignment() -> None:
    evidence = extract_explicit_preference_evidence(
        "preference: credentials=password manager entry was swordfish",
        source_id="run-structured-safe-follower-past-secret",
        observed_at=datetime(2026, 9, 8, 11, 34, tzinfo=UTC),
    )

    assert evidence == ()


def test_store_rejects_past_tense_safe_follower_assignment(tmp_path) -> None:
    store = PreferenceStore(tmp_path)
    evidence = PreferenceEvidence(
        domain="general",
        key="credentials",
        value="secret manager credentials were confidentialvalue",
        source="explicit",
        source_id="run-direct-safe-follower-past-secret",
        observed_at=datetime(2026, 9, 8, 11, 34, tzinfo=UTC),
    )

    write = store.record(evidence)

    assert write.outcome == "rejected_sensitive"
    assert write.record is None
    assert tuple(tmp_path.glob("*.json")) == ()
