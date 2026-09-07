"""Conservative explicit-preference extraction regressions (Law 4.2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from computeruse.memory.preferences import extract_explicit_preference_evidence


def _now() -> datetime:
    return datetime(2026, 9, 7, 21, 30, tzinfo=UTC)


def _extract(goal: str):
    return extract_explicit_preference_evidence(
        goal,
        source_id="run-extract-1",
        observed_at=_now(),
    )


@pytest.mark.parametrize(
    ("goal", "expected_text", "expected_key"),
    (
        ("I prefer concise answers.", "I prefer concise answers", "response-style"),
        (
            "From now on use compact summaries.",
            "From now on use compact summaries",
            "summary-style",
        ),
        (
            "Always answer with short bullet points.",
            "Always answer with short bullet points",
            "response-format",
        ),
        (
            "Tercihim kısa ve doğrudan cevaplar.",
            "Tercihim kısa ve doğrudan cevaplar",
            "response-style",
        ),
        (
            "Kısa cevapları tercih ederim.",
            "Kısa cevapları tercih ederim",
            "response-style",
        ),
        (
            "Bundan sonra kısa özetler kullan.",
            "Bundan sonra kısa özetler kullan",
            "summary-style",
        ),
        (
            "Her zaman önce doğrulama yap.",
            "Her zaman önce doğrulama yap",
            "verification-policy",
        ),
    ),
)
def test_natural_durable_cues_create_explicit_general_evidence(
    goal: str,
    expected_text: str,
    expected_key: str,
) -> None:
    evidence = _extract(goal)

    assert len(evidence) == 1
    item = evidence[0]
    assert item.domain == "general"
    assert item.source == "explicit"
    assert item.source_id == "run-extract-1"
    assert item.value == expected_text
    assert item.key == expected_key


def test_structured_english_preference_maps_key_directly() -> None:
    evidence = _extract("preference: response-style=compact summaries")

    assert len(evidence) == 1
    item = evidence[0]
    assert item.domain == "general"
    assert item.key == "response-style"
    assert item.value == "compact summaries"
    assert item.source == "explicit"


def test_structured_turkish_preference_maps_key_directly() -> None:
    evidence = _extract("tercih: cevap-stili=kısa özetler")

    assert len(evidence) == 1
    assert evidence[0].key == "cevap-stili"
    assert evidence[0].value == "kısa özetler"


def test_generic_task_wording_is_not_inferred_as_a_preference() -> None:
    assert _extract("Open Notes and write a compact summary of the current page.") == ()
    assert _extract("Chrome'u aç ve ilk sonucu incele.") == ()


def test_only_durable_clause_is_extracted_from_mixed_goal() -> None:
    evidence = _extract(
        "Open Notes and summarize the page. From now on use compact summaries. "
        "Then verify the note."
    )

    assert len(evidence) == 1
    assert evidence[0].value == "From now on use compact summaries"
    assert evidence[0].key == "summary-style"


def test_long_natural_clause_is_bounded_to_240_characters() -> None:
    evidence = _extract("From now on " + "a" * 400)

    assert len(evidence) == 1
    assert len(evidence[0].value) <= 240


def test_sensitive_durable_clause_is_discarded_before_evidence_creation() -> None:
    secret_like = "token=" + "abcdef0123456789"
    assert _extract("From now on always use " + secret_like) == ()


def test_structured_sensitive_preference_is_discarded() -> None:
    secret_like = "api_key=" + "sk" + "-abcdefghijklmnopqrstuvwxyz"
    assert _extract("preference: credentials=" + secret_like) == ()


def test_extraction_is_deterministic_and_deduplicates_equivalent_clauses() -> None:
    goal = "I prefer concise answers. I prefer   concise answers."
    evidence = _extract(goal)

    assert len(evidence) == 1
    assert evidence[0].value == "I prefer concise answers"
    assert evidence[0].key == "response-style"
