"""Two-stage skill registry (Law 3).

The registry never hands out full definitions during a scan. ``search`` returns
only summaries (Stage 1); ``load`` fetches the single full body for a chosen id
(Stage 2). Keeping ``search`` a free function over an index makes it pure and
testable, while :class:`SkillRegistry` is the imperative shell that owns the
on-disk store.
"""

from __future__ import annotations

import logging

import re

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from computeruse.skills.schemas import UNINFORMATIVE_WORDS, SkillDefinition, SkillSummary, summary_of


@dataclass(frozen=True)
class RelevanceMatch:
    """A scored summary — the only thing search returns (Law 3 Stage 1)."""

    summary: SkillSummary
    score: int


#: Three is deliberate: one failure is easily the screen's fault rather than
#: the recipe's, two could be coincidence, and a recipe that has now sent three
#: runs down the wrong path is worse than no recipe at all.
LOGGER: Final = logging.getLogger(__name__)

#: Failures with nothing to show for them, after which a skill is withheld.
DEMOTE_AFTER_FAILURES: Final[int] = 3


def is_demoted(summary: SkillSummary) -> bool:
    """Has this skill earned its way out of the store (pure)?

    Only a record of pure failure demotes. A skill that has worked even once
    keeps being offered however often it has since missed — the failures are
    then far more likely to be about the screen it met than the route it
    describes.
    """
    return summary.wins == 0 and summary.uses >= DEMOTE_AFTER_FAILURES


def track_record_bonus(summary: SkillSummary) -> int:
    """How much a skill's history moves it up the ranking (pure).

    Deliberately small, and capped. A proven skill should win a tie against an
    unproven one; it should not beat a skill that actually matches the query,
    or the store would ossify around whatever happened to be tried first.
    """
    if summary.uses == 0:
        return 0
    return 1 if summary.wins > 0 else -1


def skill_for_goal(
    summaries: Iterable[SkillSummary], *, app: str, description: str
) -> SkillSummary | None:
    """The skill already covering this exact goal in this app (pure).

    De-duplication by action-sequence signature only ever catches trivial
    workflows. Measured on the real store: of four goals run more than once,
    three produced a fresh skill every time, because the routes genuinely
    differed — two presses one run and four the next, eight actions one run and
    twelve the next. A signature over the exact sequence is doing its job when
    it calls those different; the mistake is asking it to answer "have I
    learned this goal?", which is a question about the goal.

    So the goal is the key. Matching is exact after case-folding, which is
    enough for the case that matters: a repeat run is the same goal text again,
    verbatim. Two phrasings of one intent stay two skills, and that is the
    honest answer — nothing here can tell that they meant the same thing.

    A store written before this rule can hold several skills for one goal, so
    the best-established one is returned rather than whichever file sorts
    first: that is where the track record already is, and refining it is what
    lets the leftovers fade instead of competing.
    """
    wanted = description.strip().casefold()
    candidates = [
        summary
        for summary in summaries
        if summary.app == app and summary.description.strip().casefold() == wanted
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda s: (-s.wins, -s.uses, s.skill_id))


def refined_route(
    fresh: SkillDefinition, stored: SkillDefinition
) -> SkillDefinition | None:
    """``stored`` rewritten to a shorter route, or None to leave it alone (pure).

    A repeat run used to write a second skill for the same goal, starting at
    zero uses, while the skill it had just mounted kept the credit for a run
    the new one would answer next time. The store filled with near-duplicates
    that split the evidence between them, so no route ever accumulated a track
    record worth trusting — the opposite of learning from repetition.

    Identity and track record stay with the goal; only the route is replaced,
    and only by a shorter one. A longer route is a worse answer to a question
    already answered.
    """
    if len(fresh.steps) >= len(stored.steps):
        return None
    return stored.model_copy(
        update={
            "steps": fresh.steps,
            "signature": fresh.signature,
            "tags": fresh.tags,
            "parameters": fresh.parameters,
            "version": stored.version + 1,
        }
    )


def _content_tokens(text: str) -> frozenset[str]:
    """Lowercased content words of a phrase, punctuation stripped (pure)."""
    cleaned = re.sub(r"[^\w]+", " ", text.lower(), flags=re.UNICODE)
    return frozenset(
        token
        for token in cleaned.split()
        if len(token) > 1 and token not in UNINFORMATIVE_WORDS
    )


def search(
    summaries: Iterable[SkillSummary],
    query: str,
    *,
    min_score: int = 1,
) -> list[RelevanceMatch]:
    """Rank summaries against a query (pure).

    A deliberately cheap scorer: every matched app, tag or description token
    bumps the score. Queries are lowercased and filtered against common
    stop-words. Matches below ``min_score`` are rejected to prevent low-quality
    drift.

    The description is scored because it is the only field guaranteed to have
    content — it *is* the goal the skill was distilled from. Scoring app and
    tags alone made the store write-only: measured on a real store, 12 skills
    were indexed and every realistic query returned nothing, because the
    distiller left tags empty and a run's query is its goal text, which rarely
    repeats the application's name verbatim. A skill nobody can retrieve is a
    skill nobody learns from.

    Weights rank rather than gate: an app match (2) outranks a word match (1),
    so a same-app skill sorts above a coincidental phrase match from another
    application, and ``min_score`` still filters noise.
    """
    tokens = {
        token
        for token in query.lower().split()
        if token and token not in UNINFORMATIVE_WORDS
    }
    matches: list[RelevanceMatch] = []
    for summary in summaries:
        score = 0
        description_tokens = _content_tokens(summary.description)
        for token in tokens:
            if token in (summary.app.lower(),):
                score += 2
            if any(token in tag.lower() for tag in summary.tags):
                score += 1
            if token in description_tokens:
                score += 1
        if is_demoted(summary):
            # Withheld entirely rather than ranked last: an actively harmful
            # recipe offered as a fallback is still offered.
            continue
        score += track_record_bonus(summary)
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

    def record_outcome(self, skill_id: str, *, succeeded: bool) -> None:
        """Remember how a mounted skill fared on the run that used it.

        Without this the counters stay at zero and the ranking that reads them
        is dead code — which is exactly what distillation was before: a skill
        was written once and never judged again.

        Missing or unreadable skills are ignored rather than raised on: a run
        has already finished by the time this is called, and failing its
        bookkeeping would turn a completed task into an error. A skill can be
        genuinely gone — deleted between mounting and finishing — which is why
        a missing key is caught alongside a broken file.
        """
        try:
            definition = self.load(skill_id)
        except (KeyError, OSError, ValueError) as exc:
            LOGGER.debug("cannot record outcome for skill %r: %s", skill_id, exc)
            return
        self.save(
            definition.model_copy(
                update={
                    "uses": definition.uses + 1,
                    "wins": definition.wins + (1 if succeeded else 0),
                }
            )
        )

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