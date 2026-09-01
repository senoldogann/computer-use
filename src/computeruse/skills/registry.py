"""Two-stage skill registry (Law 3).

The registry never hands out full definitions during a scan. ``search`` returns
only summaries (Stage 1); ``load`` fetches the single full body for a chosen id
(Stage 2). Keeping ``search`` a free function over an index makes it pure and
testable, while :class:`SkillRegistry` is the imperative shell that owns the
on-disk store.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from computeruse.skills.schemas import SkillDefinition, SkillSummary, summary_of


@dataclass(frozen=True)
class RelevanceMatch:
    """A scored summary — the only thing search returns (Law 3 Stage 1)."""

    summary: SkillSummary
    score: int


_SEARCH_STOP_WORDS: frozenset[str] = frozenset(
    {"the", "a", "an", "in", "on", "at", "to", "for", "of", "and", "or", "is", "it", "with", "as", "by", "from", "into"}
)


def search(
    summaries: Iterable[SkillSummary],
    query: str,
    *,
    min_score: int = 1,
) -> list[RelevanceMatch]:
    """Rank summaries against a query (pure).

    A deliberately cheap scorer: every matched tag or app token bumps the score.
    Queries are lowercased and filtered against common stop-words. Matches below
    ``min_score`` are rejected to prevent low-quality drift.
    """
    tokens = {
        token
        for token in query.lower().split()
        if token and token not in _SEARCH_STOP_WORDS
    }
    matches: list[RelevanceMatch] = []
    for summary in summaries:
        score = 0
        for token in tokens:
            if token in (summary.app.lower(),):
                score += 2
            if any(token in tag.lower() for tag in summary.tags):
                score += 1
        if score >= min_score:
            matches.append(RelevanceMatch(summary=summary, score=score))
    # Deterministic ordering: score desc, then id asc (stability across runs).
    matches.sort(key=lambda m: (-m.score, m.summary.skill_id))
    return matches


class SkillRegistry:
    """Imperative shell over the on-disk skill store (Law 6: a connector).

    One JSON file per skill under ``store_dir``, named ``<skill_id>.json``.
    Indexing reads every file once and caches the parsed summaries; loading a
    definition re-reads the file (so other agents' edits are visible) and caches
    it for the session.
    """

    def __init__(self, store_dir: Path) -> None:
        self._store_dir = store_dir
        # Session cache of the summary index (Law 3 Stage 1, G6): built once,
        # then served until a ``save`` invalidates it — a RETRIEVE scan must
        # not re-read and re-parse every skill file per search.
        self._index_cache: tuple[SkillSummary, ...] | None = None

    def index(self) -> list[SkillSummary]:
        """Stage 1: return the summary index (cached per session)."""
        if self._index_cache is None:
            summaries: list[SkillSummary] = []
            for path in sorted(self._store_dir.glob("*.json")):
                summaries.append(summary_of(_read_definition(path)))
            self._index_cache = tuple(summaries)
        return list(self._index_cache)

    def search(self, query: str) -> list[RelevanceMatch]:
        """Convenience: index-then-search, still returning only summaries."""
        return search(self.index(), query)

    def load(self, skill_id: str) -> SkillDefinition:
        """Stage 2: fetch the full body for a single skill id."""
        path = self._store_dir / f"{skill_id}.json"
        if not path.is_file():
            raise KeyError(f"no skill with id {skill_id!r} in {self._store_dir}")
        return _read_definition(path)

    def save(self, definition: SkillDefinition) -> None:
        """Persist a skill definition as its id-named JSON file.

        Invalidates the session index cache so a subsequent ``search`` sees the
        new skill. ``load`` (Stage 2) deliberately re-reads the file each time,
        so fresh edits stay visible at the single-skill granularity.
        """
        self._store_dir.mkdir(parents=True, exist_ok=True)
        path = self._store_dir / f"{definition.skill_id}.json"
        path.write_text(
            definition.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        self._index_cache = None


def _read_definition(path: Path) -> SkillDefinition:
    """Parse one skill file; surfaces corrupt storage loudly (Law 6.3)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return SkillDefinition.model_validate(raw)