"""Regressions for modal credential assignments after safe prose followers (#77)."""

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
    "modal",
    ("will", "would", "can", "could", "may", "might", "shall"),
)
def test_safe_prose_follower_cannot_hide_modal_assignment(modal: str) -> None:
    text = f"password manager entry {modal} be swordfish"

    assert contains_sensitive_preference_material(text) is True


def test_structured_preference_discards_modal_safe_follower_assignment() -> None:
    evidence = extract_explicit_preference_evidence(
        "preference: credentials=password manager entry will be swordfish",
        source_id="run-structured-safe-follower-modal-secret",
        observed_at=datetime(2026, 9, 8, 11, 58, tzinfo=UTC),
    )

    assert evidence == ()


def test_store_rejects_modal_safe_follower_assignment(tmp_path) -> None:
    store = PreferenceStore(tmp_path)
    evidence = PreferenceEvidence(
        domain="general",
        key="credentials",
        value="password manager entry will be swordfish",
        source="explicit",
        source_id="run-direct-safe-follower-modal-secret",
        observed_at=datetime(2026, 9, 8, 11, 58, tzinfo=UTC),
    )

    write = store.record(evidence)

    assert write.outcome == "rejected_sensitive"
    assert write.record is None
    assert tuple(tmp_path.glob("*.json")) == ()
