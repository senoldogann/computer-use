"""Adversarial regressions for whitespace-delimited preference secrets (#72)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from computeruse.memory.preferences import (
    contains_sensitive_preference_material,
    extract_explicit_preference_evidence,
)


@pytest.mark.parametrize(
    "text",
    (
        "password hunter2",
        "passwd p@ssw0rd",
        "api key abcdef0123456789",
        "api key: abcdef0123456789",
        "api key = abcdef0123456789",
        "token abcdef0123456789",
        "secret 3f8a9c71d204",
    ),
)
def test_whitespace_delimited_credential_values_are_sensitive(text: str) -> None:
    assert contains_sensitive_preference_material(text) is True


@pytest.mark.parametrize(
    "text",
    (
        "token-efficient summaries",
        "keep the token budget small",
        "use the password manager workflow",
        "rotate the api key regularly",
        "document the secret rotation policy",
        "never reveal a password in summaries",
    ),
)
def test_ordinary_security_language_is_not_treated_as_a_secret(text: str) -> None:
    assert contains_sensitive_preference_material(text) is False


def test_natural_durable_clause_is_screened_before_truncation() -> None:
    padding = "compact summaries " * 20
    goal = f"From now on use {padding}password hunter2"

    evidence = extract_explicit_preference_evidence(
        goal,
        source_id="run-secret-after-bound",
        observed_at=datetime(2026, 9, 8, 7, 0, tzinfo=UTC),
    )

    assert evidence == ()


def test_structured_preference_discards_whitespace_token_value() -> None:
    evidence = extract_explicit_preference_evidence(
        "preference: credentials=token abcdef0123456789",
        source_id="run-structured-secret",
        observed_at=datetime(2026, 9, 8, 7, 0, tzinfo=UTC),
    )

    assert evidence == ()
