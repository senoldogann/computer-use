"""Typed user-preference memory core (Law 4.2).

This module starts deliberately small: it defines the evidence boundary and a
stable identity for one preference. Reconciliation, persistence and extraction
are added in later TDD steps so each behavior is pinned independently.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from computeruse.memory.schemas import PreferenceDomain, PreferenceSource
from computeruse.slug import ascii_slug


def _normalize_identity_text(value: str) -> str:
    """Collapse surrounding/repeated whitespace without changing content case."""
    return " ".join(value.split())


def make_preference_id(domain: PreferenceDomain, key: str, value: str) -> str:
    """Return a stable, readable id for one exact preference value."""
    normalized_key = _normalize_identity_text(key)
    normalized_value = _normalize_identity_text(value)
    if not normalized_key:
        raise ValueError("preference key must be non-empty")
    if not normalized_value:
        raise ValueError("preference value must be non-empty")
    digest = hashlib.sha256(
        f"{domain}\0{normalized_key}\0{normalized_value}".encode()
    ).hexdigest()[:12]
    key_slug = ascii_slug(normalized_key, max_chars=48) or "preference"
    return f"{domain}.{key_slug}.{digest}"


@dataclass(frozen=True)
class PreferenceEvidence:
    """One observation offered to the preference reconciler."""

    domain: PreferenceDomain
    key: str
    value: str
    source: PreferenceSource
    source_id: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("preference evidence key must be non-empty")
        if not self.value.strip():
            raise ValueError("preference evidence value must be non-empty")
        if not self.source_id.strip():
            raise ValueError("preference evidence source_id must be non-empty")
