"""Typed user-preference memory core (Law 4.2).

Preference memory is deliberately stricter than generic semantic memory. A
preference changes future agent behavior, so evidence must keep provenance,
contradictions must preserve history, duplicate observations must be
idempotent, and credential-like material must be rejected before persistence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from computeruse.atomic import write_atomic
from computeruse.memory.schemas import (
    PreferenceDomain,
    PreferenceRecord,
    PreferenceSource,
)
from computeruse.slug import ascii_slug

LOGGER: Final = logging.getLogger(__name__)
ACTIVE_PREFERENCE_LIMIT: Final[int] = 16
MAX_EXPLICIT_PREFERENCE_CHARS: Final[int] = 240

PreferenceWriteOutcome = Literal[
    "created",
    "reinforced",
    "pending",
    "superseded",
    "rejected_sensitive",
]
SensitiveMaterialReason = Literal[
    "none",
    "named_credential_value",
    "opaque_credential_shape",
]

_SOURCE_STRENGTH: Final[dict[PreferenceSource, int]] = {
    "repeated_behavior": 1,
    "successful_correction": 2,
    "explicit": 3,
}

# Named credential labels need semantic handling rather than a token-complexity
# regex. Alphabetic values are legitimate secrets, while punctuation after an
# ordinary word is not evidence of a credential. Spaces/tabs inside ``api key``
# are intentionally bounded so this never becomes an unbounded cross-clause
# pattern.
_CREDENTIAL_LABEL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"(?P<label>password|passwd|api(?:[ \t]{1,3}|[_-])?key|token|secret)"
    r"(?![A-Za-z0-9_])"
)
_DELIMITED_CREDENTIAL_VALUE_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*[:=][ \t]*(?P<value>\S+)"
)
_WHITESPACE_CREDENTIAL_VALUE_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]+(?P<value>\S+)"
)

# Positive prose evidence. These words describe the credential *concept* rather
# than supplying its value. Anything else after a whitespace-delimited label is
# deliberately fail-closed: preference loss is visible and recoverable; secret
# persistence is not. Trailing punctuation is stripped only for lexical
# comparison and is never treated as credential complexity.
_SAFE_CREDENTIAL_PROSE_FOLLOWERS: Final[dict[str, frozenset[str]]] = {
    "password": frozenset(
        {
            "manager",
            "managers",
            "management",
            "workflow",
            "policy",
            "policies",
            "hygiene",
            "rotation",
            "rotation-policy",
            "rules",
            "requirements",
            "in",
        }
    ),
    "passwd": frozenset(
        {
            "manager",
            "managers",
            "management",
            "workflow",
            "policy",
            "policies",
            "hygiene",
            "rotation",
            "rotation-policy",
            "rules",
            "requirements",
            "in",
        }
    ),
    "api key": frozenset(
        {
            "manager",
            "managers",
            "management",
            "regularly",
            "rotation",
            "rotation-policy",
            "policy",
            "policies",
            "lifecycle",
            "hygiene",
        }
    ),
    "token": frozenset(
        {
            "budget",
            "budget-optimized",
            "efficient",
            "efficiency",
            "usage",
            "limit",
            "limits",
            "window",
            "windows",
            "count",
            "counts",
        }
    ),
    "secret": frozenset(
        {
            "rotation",
            "rotation-policy",
            "management",
            "manager",
            "policy",
            "policies",
            "storage",
            "handling",
            "scanner",
            "scanning",
        }
    ),
}

# Provider-specific opaque shapes remain pattern-based because their shape is
# itself the evidence; unlike prose punctuation, these prefixes/structures are
# credential formats by definition.
_OPAQUE_SENSITIVE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)

_STRUCTURED_PREFERENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)^(?:preference|tercih)\s*:\s*([^=]{1,80}?)\s*=\s*(.+)$"
)
_CLAUSE_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"[.!?;\n]+")

_VERIFICATION_MARKERS: Final[tuple[str, ...]] = (
    "verify",
    "verification",
    "doğrula",
    "doğrulama",
    "dogrula",
    "dogrulama",
)
_SUMMARY_MARKERS: Final[tuple[str, ...]] = (
    "summary",
    "summaries",
    "özet",
    "özetler",
    "ozet",
    "ozetler",
)
_RESPONSE_FORMAT_MARKERS: Final[tuple[str, ...]] = (
    "bullet",
    "bullets",
    "bullet point",
    "bullet points",
    "markdown",
    "json",
    "table",
    "tablo",
    "madde",
    "maddeler",
    "liste",
)
_RESPONSE_STYLE_MARKERS: Final[tuple[str, ...]] = (
    "answer",
    "answers",
    "response",
    "responses",
    "reply",
    "replies",
    "cevap",
    "cevaplar",
    "cevapları",
    "cevaplari",
    "yanıt",
    "yanıtlar",
    "yanit",
    "yanitlar",
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
class SensitiveMaterialClassification:
    """Secret-screening verdict that deliberately never retains the candidate value."""

    sensitive: bool
    reason: SensitiveMaterialReason
    label: str | None = None


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


def _canonical_credential_label(raw: str) -> str:
    """Normalize accepted label spelling without retaining any adjacent value."""
    normalized = " ".join(
        raw.casefold().replace("_", " ").replace("-", " ").split()
    )
    return "api key" if normalized == "apikey" else normalized


def _prose_follower(raw: str) -> str:
    """Normalize one following prose token; punctuation has no security meaning."""
    return raw.strip(".,;:!?()[]{}\"'").casefold()


def classify_sensitive_preference_material(text: str) -> SensitiveMaterialClassification:
    """Classify credential-like text at the single preference-memory boundary.

    Named labels are handled structurally. ``:`` / ``=`` assignments are
    unambiguously sensitive. Whitespace assignments accept alphabetic values
    and fail closed unless the following word is positive evidence that the
    phrase is ordinary credential-related prose. Opaque provider credentials
    are then checked by their format-specific patterns.
    """
    for match in _CREDENTIAL_LABEL_RE.finditer(text):
        label = _canonical_credential_label(match.group("label"))
        remainder = text[match.end() :]
        if _DELIMITED_CREDENTIAL_VALUE_RE.match(remainder) is not None:
            return SensitiveMaterialClassification(
                sensitive=True,
                reason="named_credential_value",
                label=label,
            )

        whitespace_value = _WHITESPACE_CREDENTIAL_VALUE_RE.match(remainder)
        if whitespace_value is None:
            continue
        raw_follower = whitespace_value.group("value")
        follower = _prose_follower(raw_follower)
        trailing = remainder[whitespace_value.end("value") :]
        introduces_assignment = (
            raw_follower.endswith((":", "=")) and bool(trailing.strip())
        ) or _DELIMITED_CREDENTIAL_VALUE_RE.match(trailing) is not None
        if (
            follower
            and follower in _SAFE_CREDENTIAL_PROSE_FOLLOWERS[label]
            and not introduces_assignment
        ):
            continue
        return SensitiveMaterialClassification(
            sensitive=True,
            reason="named_credential_value",
            label=label,
        )

    if any(pattern.search(text) is not None for pattern in _OPAQUE_SENSITIVE_PATTERNS):
        return SensitiveMaterialClassification(
            sensitive=True,
            reason="opaque_credential_shape",
        )
    return SensitiveMaterialClassification(sensitive=False, reason="none")


def contains_sensitive_preference_material(text: str) -> bool:
    """Compatibility predicate backed by the typed secret classifier."""
    return classify_sensitive_preference_material(text).sensitive


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


def _preference_rank(record: PreferenceRecord) -> tuple[int, float, float, str]:
    """Authority order for one active value, strongest and freshest first."""
    return (
        -_SOURCE_STRENGTH[record.source],
        -record.confidence,
        -record.last_seen.timestamp(),
        record.preference_id,
    )


def _active_incumbent(
    records: tuple[PreferenceRecord, ...],
    *,
    domain: PreferenceDomain,
    key: str,
    excluding_id: str,
) -> PreferenceRecord | None:
    """Strongest other active value for one domain/key pair.

    ``supersedes`` is provenance, not an active-state graph. A user can choose
    A, then B, then A again; treating those history edges as tombstones creates
    an A <-> B cycle and hides both values. Authority is instead resolved from
    typed evidence strength, confidence, and freshness every time.
    """
    normalized_key = _normalize_identity_text(key)
    candidates = [
        record
        for record in records
        if record.preference_id != excluding_id
        and record.domain == domain
        and _normalize_identity_text(record.key) == normalized_key
        and _is_active(record)
    ]
    if not candidates:
        return None
    candidates.sort(key=_preference_rank)
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
    classification = classify_sensitive_preference_material(sensitive_text)
    if classification.sensitive:
        return PreferenceWrite(
            outcome="rejected_sensitive",
            record=None,
            replaced_id=None,
            safe_summary=f"rejected sensitive preference evidence for {evidence.domain}",
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


def active_preferences(
    records: tuple[PreferenceRecord, ...],
    *,
    domain: PreferenceDomain | None = None,
    limit: int = ACTIVE_PREFERENCE_LIMIT,
) -> tuple[PreferenceRecord, ...]:
    """Return deterministic, contradiction-free preferences safe to stage.

    History is append-preserving, but prompt context is not a history dump.
    Every active record participates in its ``(domain, key)`` election and one
    winner is chosen by evidence authority, confidence, then freshness.
    ``supersedes`` remains explanatory provenance only, which is what lets a
    user safely return to a value they preferred before.
    """
    if limit < 0:
        raise ValueError("preference limit must be non-negative")

    grouped: dict[tuple[PreferenceDomain, str], list[PreferenceRecord]] = {}
    for record in records:
        if domain is not None and record.domain != domain:
            continue
        if not _is_active(record):
            continue
        group_key = (record.domain, _normalize_identity_text(record.key))
        grouped.setdefault(group_key, []).append(record)

    winners: list[PreferenceRecord] = []
    for candidates in grouped.values():
        candidates.sort(key=_preference_rank)
        winners.append(candidates[0])

    winners.sort(
        key=lambda record: (
            record.domain,
            _normalize_identity_text(record.key),
            -record.confidence,
            record.preference_id,
        )
    )
    return tuple(winners[:limit])


def _natural_clause_is_durable(clause: str) -> bool:
    """Recognize only explicit durable-language cues, not generic task prose."""
    lowered = clause.casefold()
    prefixes = (
        "i prefer ",
        "always ",
        "from now on ",
        "tercihim ",
        "her zaman ",
        "bundan sonra ",
    )
    return lowered.startswith(prefixes) or " tercih ederim" in lowered


def _instruction_key(value: str) -> str:
    slug = ascii_slug(value, max_chars=56)
    if slug:
        return f"instruction.{slug}"
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    return f"instruction.{digest}"


def _contains_marker(value: str, markers: tuple[str, ...]) -> bool:
    lowered = value.casefold()
    return any(marker in lowered for marker in markers)


def _natural_preference_key(value: str) -> str:
    """Map common durable EN/TR statements onto stable contradiction keys.

    This is intentionally a tiny taxonomy rather than open-ended NLP. It runs
    only after the durable-intent gate has accepted the clause, and canonicalizes
    the few families where contradictory wording must meet the same stored key.
    Unknown standing instructions keep a content-specific fallback key instead
    of being guessed into a broad category.
    """
    if _contains_marker(value, _VERIFICATION_MARKERS):
        return "verification-policy"
    if _contains_marker(value, _SUMMARY_MARKERS):
        return "summary-style"
    if _contains_marker(value, _RESPONSE_FORMAT_MARKERS):
        return "response-format"
    if _contains_marker(value, _RESPONSE_STYLE_MARKERS):
        return "response-style"
    return _instruction_key(value)


def extract_explicit_preference_evidence(
    goal: str,
    *,
    source_id: str,
    observed_at: datetime,
) -> tuple[PreferenceEvidence, ...]:
    """Extract only preferences the user explicitly framed as durable intent.

    Structured ``preference: key=value`` / ``tercih: key=value`` lines map the
    key directly. Natural language is accepted only when one of the approved
    English/Turkish durable cues is present. Common response/summary/format/
    verification statements are canonicalized so contradictions reconcile
    under one key. The original clause is screened for secrets before the
    bounded value is built, so truncation can never hide a credential that
    appeared later in the user's text.
    """
    if not source_id.strip():
        raise ValueError("preference source_id must be non-empty")

    evidence: list[PreferenceEvidence] = []
    seen: set[tuple[str, str]] = set()

    for raw_line in goal.splitlines():
        line = _normalize_identity_text(raw_line)
        if not line:
            continue
        structured = _STRUCTURED_PREFERENCE_RE.fullmatch(line)
        if structured is None:
            continue
        key = _normalize_identity_text(structured.group(1))
        value = _normalize_identity_text(structured.group(2))
        if not key or not value:
            continue
        if classify_sensitive_preference_material(f"{key}: {value}").sensitive:
            continue
        bounded = value[:MAX_EXPLICIT_PREFERENCE_CHARS].rstrip()
        identity = (key, bounded)
        if identity in seen:
            continue
        seen.add(identity)
        evidence.append(
            PreferenceEvidence(
                domain="general",
                key=key,
                value=bounded,
                source="explicit",
                source_id=source_id,
                observed_at=observed_at,
            )
        )

    for raw_clause in _CLAUSE_SPLIT_RE.split(goal):
        clause = _normalize_identity_text(raw_clause)
        if not clause or _STRUCTURED_PREFERENCE_RE.fullmatch(clause) is not None:
            continue
        if not _natural_clause_is_durable(clause):
            continue
        if classify_sensitive_preference_material(clause).sensitive:
            continue
        bounded = clause[:MAX_EXPLICIT_PREFERENCE_CHARS].rstrip()
        key = _natural_preference_key(bounded)
        identity = (key, bounded)
        if identity in seen:
            continue
        seen.add(identity)
        evidence.append(
            PreferenceEvidence(
                domain="general",
                key=key,
                value=bounded,
                source="explicit",
                source_id=source_id,
                observed_at=observed_at,
            )
        )

    return tuple(evidence)


class PreferenceStore:
    """Atomic, append-history persistence for adaptive preference records."""

    def __init__(self, store_dir: Path) -> None:
        self._store_dir = store_dir

    def records(self) -> tuple[PreferenceRecord, ...]:
        """Read every healthy record, skipping isolated corrupt files."""
        if not self._store_dir.is_dir():
            return ()
        records: list[PreferenceRecord] = []
        for path in sorted(self._store_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                records.append(PreferenceRecord.model_validate(payload))
            except (OSError, ValueError) as exc:
                LOGGER.warning("unreadable preference %s: %s", path, exc)
        records.sort(key=lambda record: record.preference_id)
        return tuple(records)

    def record(self, evidence: PreferenceEvidence) -> PreferenceWrite:
        """Reconcile and atomically persist one safe evidence observation."""
        write = apply_preference_evidence(self.records(), evidence)
        if write.record is None:
            return write
        self._store_dir.mkdir(parents=True, exist_ok=True)
        path = self._store_dir / f"{write.record.preference_id}.json"
        write_atomic(path, write.record.model_dump_json(indent=2) + "\n")
        return write

    def active(
        self,
        *,
        domain: PreferenceDomain | None = None,
        limit: int = ACTIVE_PREFERENCE_LIMIT,
    ) -> tuple[PreferenceRecord, ...]:
        """Return bounded active preferences without exposing contradictions."""
        return active_preferences(self.records(), domain=domain, limit=limit)
