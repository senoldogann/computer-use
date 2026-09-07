"""Preference-memory contract regressions (Law 4.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from computeruse.memory.preferences import (
    PreferenceEvidence,
    apply_preference_evidence,
    contains_sensitive_preference_material,
    make_preference_id,
)
from computeruse.memory.schemas import PreferenceRecord, PreferenceSource


def _now() -> datetime:
    return datetime(2026, 9, 7, 20, 0, tzinfo=UTC)


def _evidence(
    value: str,
    *,
    source: PreferenceSource = "explicit",
    source_id: str = "run-1",
    key: str = "response-style",
    seconds: int = 0,
) -> PreferenceEvidence:
    return PreferenceEvidence(
        domain="formatting",
        key=key,
        value=value,
        source=source,
        source_id=source_id,
        observed_at=_now() + timedelta(seconds=seconds),
    )


def _credential_fixtures() -> tuple[str, ...]:
    """Build credential-shaped values at runtime without committing token literals."""
    return (
        "password=hunter2",
        "api_key: " + "sk" + "-abcdefghijklmnopqrstuvwxyz",
        "token=abcdef0123456789",
        "Authorization: Bearer abc.def.ghi",
        "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz0123456789",
        "gl" + "pat-" + "abcdefghijklmnopqrstuvwxyz",
        "xox" + "b-" + "1234567890-abcdefghijklmnopqrstuvwxyz",
        "AK" + "IA" + "ABCDEFGHIJKLMNOP",
        "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "signature123",
        "-----BEGIN " + "PRIVATE KEY-----",
    )


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


@pytest.mark.parametrize("text", _credential_fixtures())
def test_sensitive_material_is_detected_before_preference_persistence(text: str) -> None:
    assert contains_sensitive_preference_material(text)


def test_ordinary_token_word_is_not_treated_as_a_secret() -> None:
    assert not contains_sensitive_preference_material("prefer token-efficient summaries")


def test_explicit_evidence_creates_immediately_active_max_confidence_record() -> None:
    write = apply_preference_evidence((), _evidence("compact summaries"))

    assert write.outcome == "created"
    assert write.record is not None
    assert write.record.confidence == 1.0
    assert write.record.evidence_count == 1
    assert write.record.source == "explicit"
    assert write.record.evidence_ids == ("run-1",)
    assert write.replaced_id is None


def test_first_repeated_behavior_is_pending_not_durable() -> None:
    write = apply_preference_evidence(
        (),
        _evidence("compact", source="repeated_behavior", source_id="episode-1"),
    )

    assert write.outcome == "pending"
    assert write.record is not None
    assert write.record.confidence == 0.45
    assert write.record.evidence_count == 1


def test_second_distinct_repeated_behavior_reinforces_same_record() -> None:
    first = apply_preference_evidence(
        (),
        _evidence("compact", source="repeated_behavior", source_id="episode-1"),
    )
    assert first.record is not None

    second = apply_preference_evidence(
        (first.record,),
        _evidence(
            "compact",
            source="repeated_behavior",
            source_id="episode-2",
            seconds=1,
        ),
    )

    assert second.outcome == "reinforced"
    assert second.record is not None
    assert second.record.preference_id == first.record.preference_id
    assert second.record.confidence == pytest.approx(0.65)
    assert second.record.evidence_count == 2
    assert second.record.evidence_ids == ("episode-1", "episode-2")


def test_duplicate_evidence_source_is_idempotent() -> None:
    first = apply_preference_evidence(
        (),
        _evidence("compact", source="repeated_behavior", source_id="episode-1"),
    )
    assert first.record is not None

    duplicate = apply_preference_evidence(
        (first.record,),
        _evidence("compact", source="repeated_behavior", source_id="episode-1"),
    )

    assert duplicate.record == first.record
    assert duplicate.outcome == "pending"


def test_explicit_contradiction_preserves_history_and_supersedes_incumbent() -> None:
    first = apply_preference_evidence((), _evidence("compact"))
    assert first.record is not None

    second = apply_preference_evidence(
        (first.record,),
        _evidence("detailed", source_id="run-2", seconds=1),
    )

    assert second.outcome == "superseded"
    assert second.record is not None
    assert second.record.value == "detailed"
    assert second.record.supersedes == first.record.preference_id
    assert second.replaced_id == first.record.preference_id


def test_repeated_behavior_never_supersedes_explicit_preference() -> None:
    explicit = apply_preference_evidence((), _evidence("compact"))
    assert explicit.record is not None
    records = (explicit.record,)
    challenger = None
    for index in range(1, 5):
        challenger = apply_preference_evidence(
            records,
            _evidence(
                "detailed",
                source="repeated_behavior",
                source_id=f"episode-{index}",
                seconds=index,
            ),
        )
        assert challenger.record is not None
        records = tuple(
            record
            for record in records
            if record.preference_id != challenger.record.preference_id
        ) + (challenger.record,)

    assert challenger is not None
    assert challenger.record is not None
    assert challenger.record.confidence == pytest.approx(0.85)
    assert challenger.record.supersedes is None
    assert challenger.replaced_id is None


def test_successful_correction_supersedes_lower_confidence_inferred_record() -> None:
    first = apply_preference_evidence(
        (),
        _evidence("compact", source="repeated_behavior", source_id="episode-1"),
    )
    assert first.record is not None
    inferred = apply_preference_evidence(
        (first.record,),
        _evidence(
            "compact",
            source="repeated_behavior",
            source_id="episode-2",
            seconds=1,
        ),
    )
    assert inferred.record is not None

    correction = apply_preference_evidence(
        (inferred.record,),
        _evidence(
            "detailed",
            source="successful_correction",
            source_id="run-correction",
            seconds=2,
        ),
    )

    assert correction.outcome == "superseded"
    assert correction.record is not None
    assert correction.record.confidence == 0.90
    assert correction.record.supersedes == inferred.record.preference_id


def test_sensitive_evidence_is_rejected_without_a_record() -> None:
    sensitive_value = "api_key=" + "sk" + "-abcdefghijklmnopqrstuvwxyz"
    write = apply_preference_evidence((), _evidence(sensitive_value))

    assert write.outcome == "rejected_sensitive"
    assert write.record is None
    assert write.replaced_id is None
    assert "sensitive" in write.safe_summary.lower()
