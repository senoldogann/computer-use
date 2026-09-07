"""Adversarial preference-memory regressions found during PR C self-review."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from computeruse.memory.preferences import (
    PreferenceEvidence,
    PreferenceStore,
    extract_explicit_preference_evidence,
)


def _now() -> datetime:
    return datetime(2026, 9, 8, 0, 0, tzinfo=UTC)


def _explicit(value: str, *, source_id: str, seconds: int) -> PreferenceEvidence:
    return PreferenceEvidence(
        domain="formatting",
        key="response-style",
        value=value,
        source="explicit",
        source_id=source_id,
        observed_at=_now() + timedelta(seconds=seconds),
    )


def _extract(goal: str, *, source_id: str = "run-natural") -> tuple[PreferenceEvidence, ...]:
    return extract_explicit_preference_evidence(
        goal,
        source_id=source_id,
        observed_at=_now(),
    )


def test_returning_to_an_old_explicit_value_reactivates_it_without_losing_history(
    tmp_path: Path,
) -> None:
    """A -> B -> A must leave A active, not make a supersession cycle hide both."""
    store = PreferenceStore(tmp_path / "preferences")

    first = store.record(_explicit("compact", source_id="run-a1", seconds=0))
    second = store.record(_explicit("detailed", source_id="run-b", seconds=1))
    third = store.record(_explicit("compact", source_id="run-a2", seconds=2))

    assert first.record is not None
    assert second.record is not None
    assert third.record is not None
    assert len(store.records()) == 2
    assert third.record.preference_id == first.record.preference_id
    assert third.record.evidence_ids == ("run-a1", "run-a2")
    assert third.record.supersedes == second.record.preference_id
    assert store.active() == (third.record,)


def test_english_response_style_contradictions_share_one_canonical_key() -> None:
    concise = _extract("I prefer concise answers.", source_id="run-en-1")
    detailed = _extract("I prefer detailed answers.", source_id="run-en-2")

    assert len(concise) == 1
    assert len(detailed) == 1
    assert concise[0].key == "response-style"
    assert detailed[0].key == "response-style"
    assert concise[0].value != detailed[0].value


def test_turkish_and_english_response_style_share_the_same_canonical_key() -> None:
    turkish = _extract("Kısa cevapları tercih ederim.", source_id="run-tr")
    english = _extract("I prefer concise answers.", source_id="run-en")

    assert len(turkish) == 1
    assert len(english) == 1
    assert turkish[0].key == english[0].key == "response-style"


def test_common_durable_cues_map_to_stable_preference_families() -> None:
    summary = _extract("From now on use compact summaries.")
    response_format = _extract("Always answer with short bullet points.")
    verification = _extract("Her zaman önce doğrulama yap.")

    assert summary[0].key == "summary-style"
    assert response_format[0].key == "response-format"
    assert verification[0].key == "verification-policy"


def test_unknown_durable_instruction_keeps_a_specific_fallback_key() -> None:
    evidence = _extract("From now on open the sidebar before navigating.")

    assert len(evidence) == 1
    assert evidence[0].key.startswith("instruction.")
