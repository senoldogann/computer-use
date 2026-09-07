"""Semantic memory tier (Law 4.2).

The episodic tier remembers *what happened* (trajectories); this tier remembers
*what is known* about an application: UI patterns, user preferences, coordinate
maps, and shortcut behaviors — the stable facts the agent should consult while
working in an app, independent of any single run.

Per Law 6 the retrieval is pure: :func:`search_entries` scores entries against
a query with no I/O, and :class:`SemanticStore` is the imperative shell that
persists one JSON file per entry (same layout as the episodic store, so the two
tiers share one mental model and one on-disk convention).

The constitution names *both* vector and key-value storage as acceptable; this
v1 is key-value with token-based retrieval — deliberately the cheapest thing
that works, and the interface the heavier semantic/vector matching (which
``skills/registry.py`` defers here) can slot into later.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, Field

from computeruse.atomic import write_atomic
from computeruse.orchestrator.schemas import Action
from computeruse.skills.registry import routes_disagree_on_site, site_markers
from computeruse.slug import ascii_slug

LOGGER: Final = logging.getLogger(__name__)

EntryKind = Literal["pattern", "preference", "shortcut", "coordinate"]


#: How much of a step's description survives into its entry id. Long enough
#: to tell two patterns apart, short enough to keep a filename sane.
DESC_SLUG_MAX_CHARS: Final[int] = 40


class SemanticEntry(BaseModel):
    """One unit of app-specific knowledge (typed, disk-round-trippable)."""

    entry_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    app: str
    key: str
    value: str
    kind: EntryKind = "preference"
    tags: tuple[str, ...] = Field(default=(), description="Search keywords.")
    description: str | None = Field(default=None)
    #: The web property this fact was learned on, when the run's goal named
    #: one (e.g. "x" for an X.com task, "hacker news" for a HN task). A
    #: browser holds facts from many sites, and a fact learned on one site is
    #: not a fact about another — this field is what lets retrieval tell them
    #: apart instead of poisoning an X.com run with Hacker News patterns.
    #: ``None`` for non-web goals and for entries written before the field
    #: existed (schema vow: old records keep validating).
    site: str | None = Field(default=None)


def _fact_text(entry: SemanticEntry) -> str:
    """The full searchable text of one entry (pure)."""
    return " ".join(
        (entry.key, entry.value, entry.app, *entry.tags)
    ).lower()


def site_of_goal(goal: str) -> str | None:
    """The single canonical web property a goal names, or None (pure).

    ``None`` for goals naming no site and for goals naming several (a
    comparison task cannot claim one learning site, so its facts stay
    site-agnostic rather than guessing).
    """
    marked = site_markers(goal)
    if len(marked) == 1:
        return next(iter(marked))
    return None


def entry_aligned_with_goal(entry: SemanticEntry, goal: str) -> bool:
    """Is a fact learned somewhere compatible with the goal's site (pure)?

    The domain-isolation rule behind Law 4.2 for browsers: a Chrome store
    holds facts learned on X.com, Hacker News, Reddit and GitHub under one
    app name, and staging all of them for an X.com goal replayed other sites'
    patterns into the prompt. An entry is refused when the goal names at
    least one known site AND the entry is provably about a different one —
    either it was stamped with a site at learning time (``entry.site``) or its
    own text names one (the legacy check, so entries written before the field
    existed still cannot poison a run). A goal naming no site refuses nothing,
    and an entry naming no site (or the goal's own site) always passes: there
    is no disagreement to protect against.
    """
    goal_sites = site_markers(goal)
    if not goal_sites:
        return True
    if entry.site is not None and goal_sites.isdisjoint({entry.site}):
        return False
    return not routes_disagree_on_site(goal, _fact_text(entry))


def search_entries(
    entries: tuple[SemanticEntry, ...],
    query: str,
    *,
    app: str | None = None,
    goal: str | None = None,
) -> tuple[SemanticEntry, ...]:
    """Score entries against a query, optionally scoped to one app (pure).

    Tokens may match the key, value, app, site, or tags — a shortcut's
    *meaning* (``key``/``tags``) and its *answer* (``value``) are both
    searchable. An empty query returns everything for the app (sorted by id):
    the RETRIEVE step asks "what do we know about this app?" with no specific
    question.

    ``goal`` applies the domain-isolation rule: entries provably learned on a
    web property the goal does not name are dropped before scoring (see
    :func:`entry_aligned_with_goal`). Deterministic ordering: score desc,
    then id asc (stable across runs).
    """
    tokens = {token for token in query.lower().split() if token}
    scored: list[tuple[int, SemanticEntry]] = []
    for entry in entries:
        if app is not None and entry.app != app:
            continue
        if goal is not None and not entry_aligned_with_goal(entry, goal):
            continue
        if not tokens:
            scored.append((0, entry))
            continue
        site_words = (entry.site,) if entry.site else ()
        haystack = " ".join(
            (entry.key, entry.value, entry.app, *entry.tags, *site_words)
        ).lower()
        score = sum(1 for token in tokens if token in haystack)
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda pair: (-pair[0], pair[1].entry_id))
    return tuple(entry for _, entry in scored)


class SemanticStore:
    """Imperative shell over the on-disk semantic store (Law 6 connector).

    One JSON file per entry, named ``<entry_id>.json`` under ``store_dir``.
    ``put`` refuses to clobber an existing id (Law 6.3: never silently destroy
    knowledge); updating a fact is an explicit delete-then-put.
    """

    def __init__(self, store_dir: Path) -> None:
        self._store_dir = store_dir

    def put(self, entry: SemanticEntry) -> None:
        self._store_dir.mkdir(parents=True, exist_ok=True)
        path = self._store_dir / f"{entry.entry_id}.json"
        if path.exists():
            raise FileExistsError(
                f"semantic entry {entry.entry_id!r} already exists; "
                "delete it explicitly before updating"
            )
        write_atomic(path, entry.model_dump_json(indent=2) + "\n")

    def get(self, entry_id: str) -> SemanticEntry:
        path = self._store_dir / f"{entry_id}.json"
        if not path.is_file():
            raise KeyError(f"no semantic entry {entry_id!r} in {self._store_dir}")
        return SemanticEntry.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def delete(self, entry_id: str) -> None:
        """Remove one entry (explicit management of evolving knowledge)."""
        path = self._store_dir / f"{entry_id}.json"
        if not path.is_file():
            raise KeyError(f"no semantic entry {entry_id!r} in {self._store_dir}")
        path.unlink()

    def entries(self) -> tuple[SemanticEntry, ...]:
        """All entries, sorted by id (ids sort lexically = insertion order).

        Corrupt files are skipped with a warning, matching MissionStore: one
        bad record must not hide every other fact the agent learned.
        """
        entries: list[SemanticEntry] = []
        if not self._store_dir.is_dir():
            return ()
        for path in sorted(self._store_dir.glob("*.json")):
            try:
                entries.append(
                    SemanticEntry.model_validate(json.loads(path.read_text(encoding="utf-8")))
                )
            except (OSError, ValueError) as exc:
                LOGGER.warning("unreadable semantic entry %s: %s", path, exc)
        return tuple(entries)

    def upsert(self, entry: SemanticEntry) -> None:
        """Write or overwrite an entry (for learning evolving UI facts)."""
        self._store_dir.mkdir(parents=True, exist_ok=True)
        path = self._store_dir / f"{entry.entry_id}.json"
        write_atomic(path, entry.model_dump_json(indent=2) + "\n")

    def search(
        self,
        query: str,
        *,
        app: str | None = None,
        goal: str | None = None,
    ) -> tuple[SemanticEntry, ...]:
        """Convenience: query the on-disk index (pure scoring underneath)."""
        return search_entries(self.entries(), query, app=app, goal=goal)

    def prune_corrupt(self) -> tuple[str, ...]:
        """Delete entries that no longer parse as :class:`SemanticEntry`.

        ``entries()`` only skips a corrupt file with a warning, which keeps one
        bad record from hiding every other fact — but it also leaves the bad
        record on disk forever, re-warned on every read. This is the explicit
        cleanup half: a file that cannot be parsed as a typed semantic entry
        (broken JSON, or a shape the model rejects) is removed and its name
        returned, so the caller can report what was swept. Parsing failures
        for a single file never abort the sweep of the rest.
        """
        removed: list[str] = []
        if not self._store_dir.is_dir():
            return ()
        for path in sorted(self._store_dir.glob("*.json")):
            try:
                SemanticEntry.model_validate(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError) as exc:
                LOGGER.warning("pruning corrupt semantic entry %s: %s", path.name, exc)
                try:
                    path.unlink()
                except OSError as exc2:  # pragma: no cover - race with the filesystem
                    LOGGER.warning("could not remove corrupt entry %s: %s", path, exc2)
                    continue
                removed.append(path.name)
        return tuple(removed)


def extract_facts_from_run(
    app: str,
    steps: tuple[Action, ...],
    step_descriptions: tuple[str, ...] = (),
    site: str | None = None,
) -> tuple[SemanticEntry, ...]:
    """Derive stable UI patterns/shortcuts from an executed trajectory (pure).

    Extracts sub_goal -> action associations so the agent automatically builds
    semantic memory across runs (Law 4.2). Typed on :class:`Action` (the
    discriminated union), never ``object``: the union's fields are read through
    ``model_dump`` so every attribute access is type-safe (Law 6.2).

    ``site`` stamps each fact with the web property the run was about (see
    :func:`site_of_goal`), so facts learned under one browser site are never
    staged for a goal on a different one.

    Coordinates are deliberately excluded: a point that was correct on this
    display is stale on the next, and replaying it replays yesterday's
    layout. Typed/pasted text is stored as a length, never verbatim, so a
    password or personal note the agent typed cannot be read back out of
    the store later.
    """
    facts: list[SemanticEntry] = []
    # ascii_slug, not isalnum: isalnum keeps non-ASCII letters the entry_id
    # pattern rejects (see computeruse.slug).
    app_slug = ascii_slug(app, max_chars=60) or "app"
    for i, action in enumerate(steps):
        desc = step_descriptions[i] if i < len(step_descriptions) else ""
        if not desc or len(desc) < 3:
            continue
        slug_desc = ascii_slug(desc, max_chars=DESC_SLUG_MAX_CHARS)
        if not slug_desc:
            continue
        entry_id = f"{app_slug}.{slug_desc}"

        payload = action.model_dump(exclude_none=True)
        action_type = str(payload.get("type", "action"))
        val = f"{action_type}"
        text_value = payload.get("text")
        if isinstance(text_value, str):
            val += f" text=<{len(text_value)} chars>"
        key_value = payload.get("key")
        if isinstance(key_value, str):
            val += f" key={key_value!r}"

        facts.append(
            SemanticEntry(
                entry_id=entry_id,
                app=app,
                key=desc,
                value=val,
                kind="pattern",
                tags=tuple(token for token in desc.lower().split() if len(token) > 2),
                site=site,
            )
        )
    return tuple(facts)
