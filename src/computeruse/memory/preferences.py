"""Typed user-preference memory core (Law 4.2).

Preference memory is deliberately stricter than generic semantic memory. A
preference changes future agent behavior, so evidence must keep provenance,
contradictions must preserve history, duplicate observations must be
idempotent, and credential-like material must be rejected before persistence.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from computeruse.memory.schemas import (
    PreferenceDomain,
    PreferenceRecord,
    PreferenceSource,
)
from computeruse.slug import ascii_slug

PreferenceWriteOutcome = Literal[
    "created",
    "reinforced",
    "pending",
    "superseded",
    "rejected_sensitive",
]

_SOURCE_STRENGTH: Final[dict[PreferenceSource, int]] = {
    "repeated_behavior": 1,
    "successful_correction": 2,
    "explicit": 3,
}

_SENSITIVE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"(?i)\b(?:password|passwd|api[_-]?key|token|secret)\s*[:=]\s*\S+"
    ),
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


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


@dataclass(frozen=True)
class PreferenceWrite:
    """Result of reconciling one evidence item with durable preference history."""

    outcome: PreferenceWriteOutcome
    record: PreferenceRecord | None
    replaced_id: str | None
    safe_summary: str


def contains_sensitive_preference_material(text: str) -> bool:
    """Whether text resembles credentials that preference memory must never keep."""
    return any(pattern.search(text) is not None for pattern in _SENSITIVE_PATTERNS)


def _initial_confidence(source: PreferenceSource) -> float:
    if source == "explicit":
        return 1.0
    if source == "successful_correction":
        return 0.90
    return 0.45


def _repeated_confidence(evidence_count: int) -> float:
    return min(0.85, 0.45 + 0.20 * max(0, evidence_count - 1))


def _is_active(record: PreferenceRecord) -> bool:
    """Whether evidence is strong enough to influence future behavior."""
    if record.source in {"explicit", "successful_correction"}:
        return True
    return record.evidence_count >= 2 and record.confidence >= 0.60


def _superseded_ids(records: tuple[PreferenceRecord, ...]) -> set[str]:
    return {record.supersedes for record in records if record.supersedes is not None}


def _active_incumbent(
    records: tuple[PreferenceRecord, ...],
    *,
    domain: PreferenceDomain,
    key: str,
    excluding_id: str,
) -> PreferenceRecord | None:
    """Strongest unsuperseded active value for one domain/key pair."""
    superseded = _superseded_ids(records)
    normalized_key = _normalize_identity_text(key)
    candidates = [
        record
        for record in records
        if record.preference_id != excluding_id
        and record.preference_id not in superseded
        and record.domain == domain
        and _normalize_identity_text(record.key) == normalized_key
        and _is_active(record)
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda record: (
            -record.confidence,
            -_SOURCE_STRENGTH[record.source],
            -record.last_seen.timestamp(),
            record.preference_id,
        )
    )
    return candidates[0]


def _merge_same_value(
    current: PreferenceRecord,
    evidence: PreferenceEvidence,
) -> PreferenceRecord:
    """Reinforce one exact value without letting weak evidence downgrade it."""
    if evidence.source_id in current.evidence_ids:
        return current
    evidence_ids = current.evidence_ids + (evidence.source_id,)
    evidence_count = len(evidence_ids)
    strongest_source = (
        evidence.source
        if _SOURCE_STRENGTH[evidence.source] > _SOURCE_STRENGTH[current.source]
        else current.source
    )
    if strongest_source == "explicit":
        confidence = 1.0
    elif strongest_source == "successful_correction":
        confidence = max(0.90, current.confidence)
    else:
        confidence = _repeated_confidence(evidence_count)
    return current.model_copy(
        update={
            "confidence": confidence,
            "evidence_count": evidence_count,
            "source": strongest_source,
            "evidence_ids": evidence_ids,
            "first_seen": min(current.first_seen, evidence.observed_at),
            "last_seen": max(current.last_seen, evidence.observed_at),
        }
    )


def apply_preference_evidence(
    records: tuple[PreferenceRecord, ...],
    evidence: PreferenceEvidence,
) -> PreferenceWrite:
    """Reconcile one observation without erasing contradictory history (pure)."""
    normalized_key = _normalize_identity_text(evidence.key)
    normalized_value = _normalize_identity_text(evidence.value)
    sensitive_text = f"{normalized_key}: {normalized_value}"
    if contains_sensitive_preference_material(sensitive_text):
        return PreferenceWrite(
            outcome="rejected_sensitive",
            record=None,
            replaced_id=None,
            safe_summary=f"rejected sensitive preference evidence for {evidence.domain}/{normalized_key}",
        )

    preference_id = make_preference_id(
        evidence.domain,
        normalized_key,
        normalized_value,
    )
    current = next(
        (record for record in records if record.preference_id == preference_id),
        None,
    )
    if current is None:
        candidate = PreferenceRecord(
            preference_id=preference_id,
            domain=evidence.domain,
            key=normalized_key,
            value=normalized_value,
            confidence=_initial_confidence(evidence.source),
            evidence_count=1,
            source=evidence.source,
            evidence_ids=(evidence.source_id,),
            first_seen=evidence.observed_at,
            last_seen=evidence.observed_at,
        )
    else:
        candidate = _merge_same_value(current, evidence)

    incumbent = _active_incumbent(
        records,
        domain=evidence.domain,
        key=normalized_key,
        excluding_id=preference_id,
    )
    replaces: PreferenceRecord | None = None
    if incumbent is not None and candidate.preference_id != incumbent.preference_id:
        may_replace = evidence.source in {"explicit", "successful_correction"} or (
            _is_active(candidate)
            and incumbent.source != "explicit"
            and candidate.confidence > incumbent.confidence
        )
        if may_replace:
            replaces = incumbent

    if replaces is not None and candidate.supersedes != replaces.preference_id:
        candidate = candidate.model_copy(update={"supersedes": replaces.preference_id})

    if replaces is not None:
        outcome: PreferenceWriteOutcome = "superseded"
    elif not _is_active(candidate):
        outcome = "pending"
    elif current is None:
        outcome = "created"
    else:
        outcome = "reinforced"

    return PreferenceWrite(
        outcome=outcome,
        record=candidate,
        replaced_id=replaces.preference_id if replaces is not None else None,
        safe_summary=f"{outcome} preference evidence for {evidence.domain}/{normalized_key}",
    )
