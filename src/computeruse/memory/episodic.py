"""Episodic memory tier (Law 4.1).

This is the *persistence* half of experiential continuity: every terminal run
leaves an :class:`Episode` on disk under ``store_dir/<episode_id>.json``. It
exists to answer two questions across sessions:

* "Have I seen this exact workflow before?" — via ``known_signatures()``, which
  feeds the distiller's de-dup gate (Law 3.3: a re-run is never re-distilled).
* "Why did a past run fail?" — via the stored retrospective, available later to
  the orchestrator or a human reviewing history.

The pure helpers (:func:`signature_of_episode`, :func:`signature_from_trace`)
share the *same* hash contract as the distiller, so the memory tier and skill
tier cannot drift into disagreeing about what two runs count as "the same".
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

LOGGER: Final = logging.getLogger(__name__)

from computeruse.memory.schemas import Episode, EpisodeOutcome
from computeruse.orchestrator.schemas import Action
from computeruse.skills.distiller import Trajectory, signature_of

# Episodic memory reuses the distiller's canonical flow-signature so de-dup is
# identical across tiers. Refactored to this alias for a single named source.
_flow_signature = signature_of


def signature_from_trace(
    app: str,
    steps: tuple[Action, ...],
    step_descriptions: tuple[str, ...] = (),
) -> str:
    """Compute the canonical flow signature from an app + action list."""
    return _flow_signature(
        Trajectory(
            app=app,
            description="",
            steps=steps,
            step_descriptions=step_descriptions,
        )
    )


def signature_of_episode(episode: Episode) -> str:
    """Recompute an episode's signature from its own fields (identity check)."""
    return signature_from_trace(
        episode.app, episode.steps, step_descriptions=episode.step_descriptions
    )


def episode_from_trace(
    *,
    app: str,
    description: str,
    steps: tuple[Action, ...],
    outcome: EpisodeOutcome,
    step_descriptions: tuple[str, ...] = (),
    retrospective: str | None = None,
    episode_id: str | None = None,
    # Optional (not required) so every existing caller keeps compiling: a run
    # that does not name itself leaves an episode that joins to no usage.
    run_id: str | None = None,
    forced_completion: bool = False,
) -> Episode:
    """Build a terminal-run Episode from the executed trace (pure factory).

    The signature is computed here from the same contract the distiller uses,
    so a persisted episode *always* carries the canonical flow identity — the
    OODA DISTILL step can hand this straight to ``EpisodicStore.record`` and
    the store's ``known_signatures()`` will feed skill de-dup correctly.
    """
    return Episode(
        episode_id=episode_id or _default_episode_id(app),
        app=app,
        description=description,
        steps=steps,
        step_descriptions=step_descriptions,
        outcome=outcome,
        retrospective=retrospective,
        run_id=run_id,
        forced_completion=forced_completion,
        signature=signature_from_trace(
            app, steps, step_descriptions=step_descriptions
        ),
    )


def _default_episode_id(app: str) -> str:
    """A filesystem-safe, sortable id: ``<slug(app)>.<utc-timestamp>``.

    Timestamps sort lexically, so ``episodes()`` oldest-first ordering (which
    relies on id order) stays correct across sessions.
    """
    slug = "".join(ch if ch.isalnum() else "-" for ch in app.lower()).strip("-")
    # Lowercased so the id matches the Episode id pattern ([a-z0-9._-]); the
    # "T" separator otherwise trips the validator.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f").lower()
    return f"{slug}.{stamp}" if slug else f"run.{stamp}"


class EpisodicStore:
    """Imperative shell: persist and query episodes on disk (Law 6 connector).

    One JSON file per episode. ``known_signatures`` is the cheapest possible
    query aimed at the distiller: it reads only each episode's signature field
    (not its full step logs), keeping the Law 4 working-context principle of not
    dragging heavy traces up until asked.
    """

    def __init__(self, store_dir: Path) -> None:
        self._store_dir = store_dir

    def record(self, episode: Episode) -> None:
        """Persist one episode; fails loudly if the id collides (Law 6.3)."""
        self._store_dir.mkdir(parents=True, exist_ok=True)
        path = self._store_dir / f"{episode.episode_id}.json"
        if path.exists():
            # Do not silently overwrite history — that would destroy learnings.
            raise FileExistsError(
                f"episode {episode.episode_id!r} already exists; refuse to clobber"
            )
        path.write_text(episode.model_dump_json(indent=2) + "\n", encoding="utf-8")

    def episodes(self) -> list[Episode]:
        """All episodes, oldest-first by id (ids sort lexically = insertion)."""
        episodes: list[Episode] = []
        for path in sorted(self._store_dir.glob("*.json")):
            episodes.append(Episode.model_validate(json.loads(path.read_text("utf-8"))))
        return episodes

    def known_signatures(self) -> set[str]:
        """The set of flow-signatures already in episodic memory.

        Feeds the distiller's ``known_signatures``; a flow with a matching
        signature here is treated as already-seen and skipped. This is the
        de-dup gate on the hot path of every new run, so it reads only each
        file's ``signature`` field — never full-deserializing the episode or
        dragging its step logs up (G4, Law 4: don't pull heavy traces until
        asked). A corrupt or unreadable file is skipped with a warning: one
        bad historical episode must not block learning from the rest.
        """
        signatures: set[str] = set()
        for path in self._store_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                LOGGER.warning("skipping unreadable episode %s: %s", path.name, exc)
                continue
            signature = raw.get("signature")
            if isinstance(signature, str):
                signatures.add(signature)
        return signatures
