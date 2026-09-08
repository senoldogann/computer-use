"""Adversarial regressions for preference secret classification (#77)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import computeruse.memory.preferences as preferences_module
from computeruse.memory.preferences import (
    PreferenceEvidence,
    PreferenceStore,
    contains_sensitive_preference_material,
    extract_explicit_preference_evidence,
)


@pytest.mark.parametrize(
    "text",
    (
        "password hunter2",
        "password swordfish",
        "passwd correcthorsebatterystaple",
        "api key ABCDEFGHIJKLMNOP",
        "apikey abcdef0123456789",
        "api_key abcdef0123456789",
        "api-key abcdef0123456789",
        "api  key abcdef0123456789",
        "api\tkey abcdef0123456789",
        "token abcdefghijklmnop",
        "secret confidentialvalue",
        "preference: credentials=password swordfish",
    ),
)
def test_named_credential_values_are_sensitive(text: str) -> None:
    assert contains_sensitive_preference_material(text) is True


@pytest.mark.parametrize(
    "text",
    (
        "password is swordfish",
        "passwd is correcthorsebatterystaple",
        "api key is ABCDEFGHIJKLMNOP",
        "token is abcdefghijklmnop",
        "secret is confidentialvalue",
    ),
)
def test_copular_credential_assignments_fail_closed(text: str) -> None:
    assert contains_sensitive_preference_material(text) is True


@pytest.mark.parametrize(
    "text",
    (
        "password policy: swordfish",
        "passwd manager: correcthorsebatterystaple",
        "api key rotation: ABCDEFGHIJKLMNOP",
        "token budget: abcdefghijklmnop",
        "secret rotation-policy: confidentialvalue",
    ),
)
def test_safe_prose_follower_cannot_hide_trailing_assignment(text: str) -> None:
    assert contains_sensitive_preference_material(text) is True


@pytest.mark.parametrize(
    "text",
    (
        "token-efficient summaries",
        "keep the token budget small",
        "use the password manager workflow",
        "From now on use the password manager, when available.",
        "rotate the api key regularly",
        "document the secret rotation policy",
        "token budget-optimized",
        "document the secret rotation-policy",
        "never reveal a password in summaries",
    ),
)
def test_ordinary_security_language_is_not_treated_as_a_secret(text: str) -> None:
    assert contains_sensitive_preference_material(text) is False


def test_classifier_exposes_typed_secret_safe_decision() -> None:
    module_symbols = vars(preferences_module)
    classify = module_symbols["classify_sensitive_preference_material"]
    classification_type = module_symbols["SensitiveMaterialClassification"]
    classification = classify("password swordfish")

    assert isinstance(classification, classification_type)
    assert classification.sensitive is True
    assert classification.reason == "named_credential_value"
    assert classification.label == "password"
    assert "swordfish" not in repr(classification)


def test_natural_durable_clause_is_screened_before_truncation() -> None:
    padding = "compact summaries " * 20
    goal = f"From now on use {padding}password swordfish"

    evidence = extract_explicit_preference_evidence(
        goal,
        source_id="run-secret-after-bound",
        observed_at=datetime(2026, 9, 8, 7, 0, tzinfo=UTC),
    )

    assert evidence == ()


def test_structured_preference_discards_alphabetic_password_value() -> None:
    evidence = extract_explicit_preference_evidence(
        "preference: credentials=password swordfish",
        source_id="run-structured-secret",
        observed_at=datetime(2026, 9, 8, 7, 0, tzinfo=UTC),
    )

    assert evidence == ()


def test_structured_preference_discards_compact_apikey_value() -> None:
    evidence = extract_explicit_preference_evidence(
        "preference: credentials=apikey=swordfish",
        source_id="run-structured-compact-apikey",
        observed_at=datetime(2026, 9, 8, 7, 0, tzinfo=UTC),
    )

    assert evidence == ()


def test_structured_preference_discards_safe_follower_assignment() -> None:
    evidence = extract_explicit_preference_evidence(
        "preference: credentials=password policy: swordfish",
        source_id="run-structured-safe-follower-secret",
        observed_at=datetime(2026, 9, 8, 7, 0, tzinfo=UTC),
    )

    assert evidence == ()


def test_safe_durable_security_prose_survives_extraction() -> None:
    goal = "From now on use the password manager, when available."

    evidence = extract_explicit_preference_evidence(
        goal,
        source_id="run-safe-prose",
        observed_at=datetime(2026, 9, 8, 7, 0, tzinfo=UTC),
    )

    assert len(evidence) == 1
    assert evidence[0].value == "From now on use the password manager, when available"


def test_store_rejects_direct_secret_evidence_without_echoing_value(tmp_path) -> None:
    store = PreferenceStore(tmp_path)
    evidence = PreferenceEvidence(
        domain="general",
        key="credentials",
        value="password swordfish",
        source="explicit",
        source_id="run-direct-store",
        observed_at=datetime(2026, 9, 8, 7, 0, tzinfo=UTC),
    )

    write = store.record(evidence)

    assert write.outcome == "rejected_sensitive"
    assert write.record is None
    assert tuple(tmp_path.glob("*.json")) == ()
    assert "swordfish" not in write.safe_summary


def test_store_rejects_compact_apikey_evidence(tmp_path) -> None:
    store = PreferenceStore(tmp_path)
    evidence = PreferenceEvidence(
        domain="general",
        key="credentials",
        value="apikey=swordfish",
        source="explicit",
        source_id="run-direct-compact-apikey",
        observed_at=datetime(2026, 9, 8, 7, 0, tzinfo=UTC),
    )

    write = store.record(evidence)

    assert write.outcome == "rejected_sensitive"
    assert write.record is None
    assert tuple(tmp_path.glob("*.json")) == ()


def test_store_rejects_safe_follower_assignment(tmp_path) -> None:
    store = PreferenceStore(tmp_path)
    evidence = PreferenceEvidence(
        domain="general",
        key="credentials",
        value="password policy: swordfish",
        source="explicit",
        source_id="run-direct-safe-follower-secret",
        observed_at=datetime(2026, 9, 8, 7, 0, tzinfo=UTC),
    )

    write = store.record(evidence)

    assert write.outcome == "rejected_sensitive"
    assert write.record is None
    assert tuple(tmp_path.glob("*.json")) == ()
