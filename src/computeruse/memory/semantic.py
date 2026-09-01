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
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from computeruse.orchestrator.schemas import Action

EntryKind = Literal["pattern", "preference", "shortcut", "coordinate"]


class SemanticEntry(BaseModel):
    """One unit of app-specific knowledge (typed, disk-round-trippable)."""

    entry_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    app: str
    key: str
    value: str
    kind: EntryKind = "preference"
    tags: tuple[str, ...] = Field(default=(), description="Search keywords.")
    description: str | None = Field(default=None)


def search_entries(
    entries: tuple[SemanticEntry, ...],
    query: str,
    *,
    app: str | None = None,
) -> tuple[SemanticEntry, ...]:
    """Score entries against a query, optionally scoped to one app (pure).

    Tokens may match the key, value, app, or tags — a shortcut's *meaning*
    (``key``/``tags``) and its *answer* (``value``) are both searchable. An
    empty query returns everything for the app (sorted by id): the RETRIEVE
    step asks "what do we know about this app?" with no specific question.

    Deterministic ordering: score desc, then id asc (stable across runs).
    """
    tokens = {token for token in query.lower().split() if token}
    scored: list[tuple[int, SemanticEntry]] = []
    for entry in entries:
        if app is not None and entry.app != app:
            continue
        if not tokens:
            scored.append((0, entry))
            continue
        haystack = " ".join(
            (entry.key, entry.value, entry.app, *entry.tags)
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
        path.write_text(entry.model_dump_json(indent=2) + "\n", encoding="utf-8")

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
        """All entries, sorted by id (ids sort lexically = insertion order)."""
        entries: list[SemanticEntry] = []
        for path in sorted(self._store_dir.glob("*.json")):
            entries.append(
                SemanticEntry.model_validate(json.loads(path.read_text(encoding="utf-8")))
            )
        return tuple(entries)

    def upsert(self, entry: SemanticEntry) -> None:
        """Write or overwrite an entry (for learning evolving UI facts)."""
        self._store_dir.mkdir(parents=True, exist_ok=True)
        path = self._store_dir / f"{entry.entry_id}.json"
        path.write_text(entry.model_dump_json(indent=2) + "\n", encoding="utf-8")

    def search(self, query: str, *, app: str | None = None) -> tuple[SemanticEntry, ...]:
        """Convenience: query the on-disk index (pure scoring underneath)."""
        return search_entries(self.entries(), query, app=app)


def extract_facts_from_run(
    app: str,
    steps: tuple[Action, ...],
    step_descriptions: tuple[str, ...] = (),
) -> tuple[SemanticEntry, ...]:
    """Derive stable UI patterns/shortcuts from an executed trajectory (pure).

    Extracts sub_goal -> action associations so the agent automatically builds
    semantic memory across runs (Law 4.2). Typed on :class:`Action` (the
    discriminated union), never ``object``: the union's fields are read through
    ``model_dump`` so every attribute access is type-safe (Law 6.2).
    """
    facts: list[SemanticEntry] = []
    app_slug = "".join(ch if ch.isalnum() else "-" for ch in app.lower()).strip("-") or "app"
    for i, action in enumerate(steps):
        desc = step_descriptions[i] if i < len(step_descriptions) else ""
        if not desc or len(desc) < 3:
            continue
        slug_desc = "".join(ch if ch.isalnum() else "-" for ch in desc.lower()).strip("-")[:40]
        if not slug_desc:
            continue
        entry_id = f"{app_slug}.{slug_desc}"

        payload = action.model_dump(exclude_none=True)
        action_type = payload.get("type", "action")
        val = f"{action_type}"
        if "x" in payload and "y" in payload:
            val += f" at ({payload['x']}, {payload['y']})"
        if "text" in payload:
            val += f" text={payload['text']!r}"
        if "key" in payload:
            val += f" key={payload['key']!r}"

        facts.append(
            SemanticEntry(
                entry_id=entry_id,
                app=app,
                key=desc,
                value=val,
                kind="pattern",
                tags=tuple(token for token in desc.lower().split() if len(token) > 2),
            )
        )
    return tuple(facts)
