"""Two-stage skill registry (Law 3).

The registry never hands out full definitions during a scan. ``search`` returns
only summaries (Stage 1); ``load`` fetches the single full body for a chosen id
(Stage 2). Keeping ``search`` a free function over an index makes it pure and
testable, while :class:`SkillRegistry` is the imperative shell that owns the
on-disk store.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from computeruse.atomic import write_atomic
from computeruse.skills.schemas import (
    SKILL_DIAGNOSTIC_MAX,
    SKILL_ID_PATTERN,
    UNINFORMATIVE_WORDS,
    SkillDefinition,
    SkillSummary,
    summary_of,
)


@dataclass(frozen=True)
class RelevanceMatch:
    """A scored summary — the only thing search returns (Law 3 Stage 1)."""

    summary: SkillSummary
    score: int


LOGGER: Final = logging.getLogger(__name__)

#: Failures with nothing to show for them, after which a skill is withheld.
DEMOTE_AFTER_FAILURES: Final[int] = 3

#: A fast path is authority to skip model deliberation. Two clean verified
#: reuses are the minimum evidence; one success is still an anecdote.
FAST_PATH_MIN_SUCCESSES: Final[int] = 2


def is_demoted(summary: SkillSummary) -> bool:
    """Has this skill earned its way out of the store (pure)?

    Only a record of pure failure demotes. A skill that has worked even once
    keeps being offered however often it has since missed — the failures are
    then far more likely to be about the screen it met than the route it
    describes.
    """
    return summary.wins == 0 and summary.uses >= DEMOTE_AFTER_FAILURES


def track_record_bonus(summary: SkillSummary) -> int:
    """How much a skill's history moves it up the ranking (pure)."""
    if summary.uses == 0:
        return 0
    return 1 if summary.wins > 0 else -1


def skill_for_goal(
    summaries: Iterable[SkillSummary], *, app: str, description: str
) -> SkillSummary | None:
    """The skill already covering this exact goal in this app (pure)."""
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
    """``stored`` rewritten to a shorter route, or None to leave it alone (pure)."""
    if len(fresh.steps) >= len(stored.steps):
        return None
    return stored.model_copy(
        update={
            "steps": fresh.steps,
            "fast_path": fresh.fast_path,
            "signature": fresh.signature,
            "tags": fresh.tags,
            "parameters": fresh.parameters,
            "version": stored.version + 1,
        }
    )


def content_tokens(text: str) -> frozenset[str]:
    """Lowercased content words of a phrase, punctuation stripped (pure)."""
    cleaned = re.sub(r"[^\w]+", " ", text.lower(), flags=re.UNICODE)
    return frozenset(
        token
        for token in cleaned.split()
        if len(token) > 1 and token not in UNINFORMATIVE_WORDS
    )


#: (aliases, canonical site) pairs naming web properties whose workflows are
#: site-specific. A route learned on one named property must never be replayed
#: on a different one merely because both live in the same browser.
_SITE_ALIASES: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("x.com", "twitter"), "x"),
    (("hacker news", "hnews", "news.ycombinator", "ycombinator"), "hacker news"),
    (("reddit",), "reddit"),
    (("youtube", "youtu.be"), "youtube"),
    (("github",), "github"),
    (("linkedin",), "linkedin"),
    (("instagram",), "instagram"),
    (("facebook", "fb.com"), "facebook"),
    (("medium.com",), "medium"),
    (("wikipedia",), "wikipedia"),
    (("stackoverflow", "stack overflow"), "stack overflow"),
    (("amazon",), "amazon"),
)


def site_markers(text: str) -> frozenset[str]:
    """Canonical web properties named by a piece of text (pure)."""
    lowered = text.casefold()
    marked = {
        canonical
        for aliases, canonical in _SITE_ALIASES
        if any(alias in lowered for alias in aliases)
    }
    return frozenset(marked)


def environment_fingerprint(app: str, goal: str) -> str:
    """Stable, non-secret environment identity used by fast-path eligibility.

    V1 deliberately fingerprints only facts already explicit in the run
    contract: application name and any known web property named by the goal.
    Raw screen text, URLs, coordinates and machine identifiers are excluded.
    """
    normalized_app = " ".join(app.split()).casefold()
    sites = ",".join(sorted(site_markers(goal))) or "none"
    return f"{normalized_app}|site:{sites}"


def fast_path_eligible(summary: SkillSummary, *, app: str, goal: str) -> bool:
    """Whether one summary has earned deterministic execution authority.

    This gate is intentionally stricter than ordinary skill retrieval. A skill
    may still be useful context after a failure; it may not skip model
    deliberation. V1 therefore requires a perfect observed record, a current
    consecutive-success streak, an exact goal/app match and an exact environment
    fingerprint.
    """
    return (
        summary.fast_path_ready
        and summary.app == app
        and summary.description.strip().casefold() == goal.strip().casefold()
        and summary.uses >= FAST_PATH_MIN_SUCCESSES
        and summary.wins >= FAST_PATH_MIN_SUCCESSES
        and summary.wins == summary.uses
        and summary.consecutive_successes >= FAST_PATH_MIN_SUCCESSES
        and summary.last_environment == environment_fingerprint(app, goal)
    )


def routes_disagree_on_site(goal: str, route_text: str) -> bool:
    """Does ``route_text`` describe work on a site the goal does not name (pure)?"""
    goal_sites = site_markers(goal)
    route_sites = site_markers(route_text)
    if not goal_sites or not route_sites:
        return False
    return goal_sites.isdisjoint(route_sites)


def search(
    summaries: Iterable[SkillSummary],
    query: str,
    *,
    min_score: int = 1,
) -> list[RelevanceMatch]:
    """Rank summaries against a query (pure)."""
    tokens = {
        token
        for token in query.lower().split()
        if token and token not in UNINFORMATIVE_WORDS
    }
    matches: list[RelevanceMatch] = []
    for summary in summaries:
        score = 0
        description_tokens = content_tokens(summary.description)
        for token in tokens:
            if token in (summary.app.lower(),):
                score += 2
            if any(token in tag.lower() for tag in summary.tags):
                score += 1
            if token in description_tokens:
                score += 1
        if is_demoted(summary):
            continue
        score += track_record_bonus(summary)
        if score >= min_score:
            matches.append(RelevanceMatch(summary=summary, score=score))
    matches.sort(key=lambda match: (-match.score, match.summary.skill_id))
    return matches


def _bounded_diagnostic(value: str | None) -> str | None:
    """Collapse and bound stored operational diagnostics (pure)."""
    if value is None:
        return None
    collapsed = " ".join(value.split())
    if not collapsed:
        return None
    return collapsed[:SKILL_DIAGNOSTIC_MAX]


class SkillRegistry:
    """Imperative shell over the on-disk skill store (Law 6: a connector)."""

    def __init__(self, store_dir: Path) -> None:
        self._store_dir = store_dir
        self._index_cache: tuple[SkillSummary, ...] | None = None

    def index(self) -> list[SkillSummary]:
        """Stage 1: return the summary index (cached per session)."""
        if self._index_cache is None:
            summaries: list[SkillSummary] = []
            for path in sorted(self._store_dir.glob("*.json")):
                try:
                    summaries.append(summary_of(_read_definition(path)))
                except (OSError, json.JSONDecodeError, ValidationError) as exc:
                    LOGGER.warning("skipping unreadable skill %s: %s", path.name, exc)
                    continue
            self._index_cache = tuple(summaries)
        return list(self._index_cache)

    def search(self, query: str) -> list[RelevanceMatch]:
        """Convenience: index-then-search, still returning only summaries."""
        return search(self.index(), query)

    def load(self, skill_id: str) -> SkillDefinition:
        """Stage 2: fetch the full body for a single validated skill id."""
        if not re.fullmatch(SKILL_ID_PATTERN, skill_id):
            raise ValueError(
                f"skill id {skill_id!r} is not a valid store id (expected "
                f"{SKILL_ID_PATTERN}); refusing to resolve it to a path"
            )
        path = self._store_dir / f"{skill_id}.json"
        if not path.is_file():
            raise KeyError(f"no skill with id {skill_id!r} in {self._store_dir}")
        return _read_definition(path)

    def record_outcome(
        self,
        skill_id: str,
        *,
        succeeded: bool,
        environment: str | None = None,
        failure_reason: str | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        """Persist how a mounted skill fared, including fast-path confidence.

        A success extends the consecutive-success streak and remembers the
        environment in which it was independently verified. Any failure resets
        the streak immediately. The historical win remains useful for ordinary
        ranking, but the perfect-record fast-path gate will refuse that skill.
        """
        try:
            definition = self.load(skill_id)
        except (KeyError, OSError, ValueError) as exc:
            LOGGER.debug("cannot record outcome for skill %r: %s", skill_id, exc)
            return

        now = observed_at or datetime.now(UTC)
        if succeeded:
            update: dict[str, object] = {
                "uses": definition.uses + 1,
                "wins": definition.wins + 1,
                "consecutive_successes": definition.consecutive_successes + 1,
                "last_successful_at": now,
                "last_failure_reason": None,
            }
            if environment is not None:
                update["last_environment"] = _bounded_diagnostic(environment)
        else:
            update = {
                "uses": definition.uses + 1,
                "consecutive_successes": 0,
                "last_failure_reason": _bounded_diagnostic(failure_reason),
            }
        self.save(definition.model_copy(update=update))

    def save(self, definition: SkillDefinition) -> None:
        """Persist a skill definition as its id-named JSON file."""
        self._store_dir.mkdir(parents=True, exist_ok=True)
        path = self._store_dir / f"{definition.skill_id}.json"
        write_atomic(path, definition.model_dump_json(indent=2) + "\n")
        self._index_cache = None


def _read_definition(path: Path) -> SkillDefinition:
    """Parse one skill file; surfaces corrupt storage loudly (Law 6.3)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return SkillDefinition.model_validate(raw)