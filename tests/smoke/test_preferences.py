"""Preference-memory contract regressions (Law 4.2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from computeruse.memory.preferences import PreferenceEvidence, make_preference_id
from computeruse.memory.schemas import PreferenceRecord


def _now() -> datetime:
    return datetime(2026, 9, 7, 20, 0, tzinfo=UTC)


def test_preference_record_carries_typed_provenance() -> None:
    record = PreferenceRecord(
        preference_id="formatting.response-style.abcdef123456",
        domain="formatting",
        key="response-style",
        value="compact summaries",
        confidence=1.0,
        evidence_count=1,
        source="explicit",
        evidence_ids=("run-1",),
        first_seen=_now(),
        last_seen=_now(),
    )

    assert record.domain == "formatting"
    assert record.key == "response-style"
    assert record.value == "compact summaries"
    assert record.source == "explicit"
    assert record.evidence_ids == ("run-1",)
    assert record.supersedes is None


def test_preference_record_rejects_invalid_confidence() -> None:
    with pytest.raises(ValidationError):
        PreferenceRecord(
            preference_id="general.bad.abcdef123456",
            domain="general",
            key="bad",
            value="bad",
            confidence=1.1,
            evidence_count=1,
            source="explicit",
            evidence_ids=("run-1",),
            first_seen=_now(),
            last_seen=_now(),
        )


def test_preference_evidence_requires_non_empty_identity_fields() -> None:
    with pytest.raises(ValueError, match="key"):
        PreferenceEvidence(
            domain="general",
            key="   ",
            value="compact",
            source="explicit",
            source_id="run-1",
            observed_at=_now(),
        )
    with pytest.raises(ValueError, match="value"):
        PreferenceEvidence(
            domain="general",
            key="style",
            value="   ",
            source="explicit",
            source_id="run-1",
            observed_at=_now(),
        )
    with pytest.raises(ValueError, match="source_id"):
        PreferenceEvidence(
            domain="general",
            key="style",
            value="compact",
            source="explicit",
            source_id="   ",
            observed_at=_now(),
        )


def test_preference_identity_is_stable_for_whitespace_equivalent_values() -> None:
    first = make_preference_id("formatting", " response style ", "compact   summaries")
    second = make_preference_id("formatting", "response style", "compact summaries")
    assert first == second


def test_contradictory_value_has_distinct_preference_identity() -> None:
    compact = make_preference_id("formatting", "response-style", "compact")
    detailed = make_preference_id("formatting", "response-style", "detailed")
    assert compact != detailed
