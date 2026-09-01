"""Skill distiller (Law 3.1, 3.3).

When a run succeeds, this module distill the *trace* down to a reusable skill
definition. It is intentionally pure: given a typed trajectory and outcome, it
produces either a :class:`SkillDefinition` or a rejection reason, with no I/O.
Persistence is the registry's job, not the distiller's.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from computeruse.orchestrator.schemas import Action
from computeruse.skills.schemas import SkillDefinition


@dataclass(frozen=True)
class Trajectory:
    """Immutable record of one successful run (feed of ordered actions)."""

    app: str
    description: str
    steps: tuple[Action, ...]
    tags: tuple[str, ...] = ()
    step_descriptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class DistillResult:
    """Outcome of attempting to distill a trajectory into a skill."""

    kind: Literal["skill", "too_short", "duplicate"]
    definition: SkillDefinition | None = None
    signature: str | None = None


# A "complex workflow" worth distilling needs more than a single trivial click;
# anything shorter is noise vs. signal for the skill store (Law 3.1).
_MIN_STEPS: int = 2


def signature_of(trajectory: Trajectory) -> str:
    """A stable content-hash describing the workflow's action sequence.

    Two runs of the *same* UI flow in the same app must produce the same
    signature (so the distiller can de-duplicate); two different flows must
    differ. We hash the ordered (action-type, semantic-params, intent) pairs
    plus the app. Including semantic parameters and step intent makes
    different click sequences in the same app distinct.

    Coordinates are deliberately *excluded*: pixel positions drift between
    runs of the same workflow, so including them would defeat de-dup.
    """
    flow: list[dict[str, str]] = []
    for i, step in enumerate(trajectory.steps):
        desc = trajectory.step_descriptions[i] if i < len(trajectory.step_descriptions) else ""
        flow.append(
            {
                "type": step.type,
                "params": _semantic_params(step),
                "intent": desc.strip().lower(),
            }
        )
    payload = json.dumps(
        {"app": trajectory.app, "flow": flow}, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    # First 16 hex chars: collision-improbable for a skill index, much shorter.
    return digest[:16]


def distill(trajectory: Trajectory, known_signatures: set[str]) -> DistillResult:
    """Turn a trajectory into a skill, unless it's trivial or already known.

    ``known_signatures`` is the set of signatures already in the store, so a
    re-run of a captured flow yields ``duplicate`` instead of a second copy.
    """
    if len(trajectory.steps) < _MIN_STEPS:
        return DistillResult(kind="too_short")

    signature = signature_of(trajectory)
    if signature in known_signatures:
        return DistillResult(kind="duplicate", signature=signature)

    steps_readable = tuple(
        f"{trajectory.step_descriptions[i]} -> {step.type}:{_compact_params(step)}"
        if i < len(trajectory.step_descriptions) and trajectory.step_descriptions[i]
        else f"{step.type}:{_compact_params(step)}"
        for i, step in enumerate(trajectory.steps)
    )
    definition = SkillDefinition(
        # Dot separator: ``:`` would break both the id regex and Windows
        # filenames (skill_id becomes the store's filename).
        skill_id=f"{_slug(trajectory.app)}.{signature}",
        description=trajectory.description,
        app=trajectory.app,
        tags=trajectory.tags,
        steps=steps_readable,
        signature=signature,
    )
    return DistillResult(kind="skill", definition=definition, signature=signature)


_COORDINATE_KEYS: frozenset[str] = frozenset({"x", "y", "start_x", "start_y", "end_x", "end_y"})
# Fields that carry *workflow meaning* and must feed the signature hash. The
# exact pixel coordinates are deliberately absent so UI drift doesn't break
# de-dup, but text/keys/buttons do distinguish otherwise-identical flows.
_SEMANTIC_KEYS: frozenset[str] = frozenset(
    {
        "text",
        "key",
        "modifiers",
        "button",
        "click_count",
        "duration_ms",
        "skill_id",
        "wpm",
        "app",
    }
)


def _semantic_params(action: Action) -> str:
    """Return a stable, coordinate-free summary of an action's params.

    Coordinates are dropped; everything else that defines the *meaning* of the
    step is kept, sorted for determinism. This is what makes the signature
    distinguish real workflow differences (F1) while staying insensitive to
    pixel drift between runs.
    """
    data = action.model_dump(exclude_none=True)
    data.pop("type", None)
    kept = {k: data[k] for k in _SEMANTIC_KEYS if k in data and k not in _COORDINATE_KEYS}
    return ",".join(f"{k}={kept[k]}" for k in sorted(kept))


def _compact_params(action: Action) -> str:
    """Summarize an action's params to a short stable string for the record."""
    data = action.model_dump(exclude_none=True)
    data.pop("type", None)
    return ",".join(f"{k}={data[k]}" for k in sorted(data))


def _slug(app: str) -> str:
    """Lowercase, hyphenate, strip non-alphanumerics for a filesystem-safe id."""
    return "".join(ch if ch.isalnum() else "-" for ch in app.lower()).strip("-")