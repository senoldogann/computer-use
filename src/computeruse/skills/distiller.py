"""Skill distiller (Law 3.1, 3.3).

When a run succeeds, this module distill the *trace* down to a reusable skill
definition. It is intentionally pure: given a typed trajectory and outcome, it
produces either a :class:`SkillDefinition` or a rejection reason, with no I/O.
Persistence is the registry's job, not the distiller's.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final, Literal

from computeruse.orchestrator.schemas import Action, ActivateApp
from computeruse.skills.schemas import UNINFORMATIVE_WORDS, SkillDefinition
from computeruse.slug import ascii_slug


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


#: Enough for any application name; the signature carries the identity.
APP_SLUG_MAX_CHARS: Final[int] = 60


#: Dynamic fragments abstracted out of the string fields that still reach the
#: signature: digit runs (version numbers, counts) and URLs. Typed and pasted
#: text no longer reaches the hash at all — it left ``_SEMANTIC_KEYS`` — so
#: what these normalise now is naming: "Photoshop 2024" and "Photoshop 2025"
#: are one application to a workflow, not two.
_DYNAMIC_NUMBER: Final = re.compile(r"\d+(?:[.,]\d+)*")
_DYNAMIC_URL: Final = re.compile(r"https?://\S+", flags=re.IGNORECASE)


def _abstract_dynamic(text: str) -> str:
    """Template the dynamic fragments of a signature input (pure).

    Applied to the string fields that still feed the hash (``app``,
    ``skill_id``, ``key``, ``button``): case is a naming accident rather than
    intent, and a version number inside an application name is not what makes
    one workflow different from another. Operand text is no longer routed
    through here — it is excluded from the signature outright.
    """
    templated = _DYNAMIC_URL.sub("<url>", text.lower())
    return _DYNAMIC_NUMBER.sub("<num>", templated)


def signature_of(trajectory: Trajectory) -> str:
    """A stable content-hash describing the workflow's action sequence.

    Two runs of the *same* UI flow in the same app must produce the same
    signature (so the distiller can de-duplicate); two different flows must
    differ. We hash the ordered (action-type, semantic-params) pairs
    plus the app. The action sequence itself (type + key/modifiers/button/
    click_count + app) makes different click sequences and hotkey flows
    distinct without relying on run-to-run variations in natural language step
    descriptions.

    Coordinates are deliberately *excluded*: pixel positions drift between
    runs of the same workflow, so including them would defeat de-dup. Dynamic
    operands (typed or pasted text) and natural-language step descriptions
    (intent) vary across runs of one parametric workflow, so they are excluded
    from the hash.
    """
    flow: list[dict[str, str]] = []
    for step in trajectory.steps:
        flow.append(
            {
                "type": step.type,
                "params": _semantic_params(step),
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
        tags=trajectory.tags or derive_tags(trajectory),
        steps=steps_readable,
        signature=signature,
    )
    return DistillResult(kind="skill", definition=definition, signature=signature)


#: How many derived tags to keep. Enough to describe what a workflow did,
#: few enough that one verbose run cannot dominate the search index.
TAG_LIMIT: Final[int] = 12


def visited_apps(trajectory: Trajectory) -> tuple[str, ...]:
    """Every application the run touched, primary first (pure).

    A multi-app flow belongs to all of its apps, not just the one it started
    in: without this a Calculator-then-Notes chain is retrievable only as
    "Calculator", and a later run searching inside Notes never sees it.
    """
    apps: list[str] = []
    for name in (trajectory.app,) + tuple(
        step.app for step in trajectory.steps if isinstance(step, ActivateApp)
    ):
        if name not in apps:
            apps.append(name)
    return tuple(apps)


def _app_tag_words(app: str) -> tuple[str, ...]:
    """An app name as tag tokens (pure).

    Same token rules as content words, so "Google Chrome" becomes the
    retrievable "google" and "chrome" the registry's substring tag match
    already understands.
    """
    cleaned = re.sub(r"[^\w]+", " ", app.lower(), flags=re.UNICODE)
    return tuple(
        token
        for token in cleaned.split()
        if len(token) >= 3 and token not in UNINFORMATIVE_WORDS
    )


def derive_tags(trajectory: Trajectory) -> tuple[str, ...]:
    """Search keywords for a skill, taken from what the run actually did (pure).

    Distillation used to leave this empty, and the registry scored *only* app
    and tags — so a real store held twelve skills that no realistic query could
    reach. Scoring the description fixed the worst of that, but the description
    is only the goal as *asked*; the sub-goals record what the agent actually
    had to do to satisfy it, which is what a later run is really searching for.

    The visited applications lead: identity before content, so a flow spanning
    Calculator and Notes answers to both names. Deterministic and cheap, in
    first-seen order so two runs of the same flow produce the same tags and
    de-duplication still works.
    """

    def take(tags: list[str], token: str) -> bool:
        if token in tags:
            return False
        tags.append(token)
        return len(tags) >= TAG_LIMIT

    tags: list[str] = []
    for app in visited_apps(trajectory):
        for token in _app_tag_words(app):
            if take(tags, token):
                return tuple(tags)
    for description in trajectory.step_descriptions:
        cleaned = re.sub(r"[^\w]+", " ", description.lower(), flags=re.UNICODE)
        for token in cleaned.split():
            if len(token) < 3 or token in UNINFORMATIVE_WORDS:
                continue
            if take(tags, token):
                return tuple(tags)
    return tuple(tags)


_COORDINATE_KEYS: frozenset[str] = frozenset({"x", "y", "start_x", "start_y", "end_x", "end_y"})
# Fields that carry *workflow meaning* and must feed the signature hash. The
# exact pixel coordinates are deliberately absent so UI drift doesn't break
# de-dup, and so are the *pacing* fields (``duration_ms`` for waits/moves,
# ``wpm`` for typing): the same flow run at a different pace is still the same
# flow — hashing pacing would break de-dup across two runs of one workflow
# (L14). Typed/pasted text (``text``) is likewise excluded: written or pasted
# content is an operand, not workflow structure ("paste text into Notes" is
# one flow regardless of what text is pasted; hashing text would fork every
# payload into its own skill copy). Keys/modifiers/buttons/click_count/
# skill_id/app do distinguish otherwise-identical flows.
_SEMANTIC_KEYS: frozenset[str] = frozenset(
    {
        "key",
        "modifiers",
        "button",
        "click_count",
        "skill_id",
        "app",
    }
)


def _semantic_params(action: Action) -> str:
    """Return a stable, coordinate-free summary of an action's params.

    Coordinates are dropped; everything else that defines the *meaning* of the
    step is kept, sorted for determinism. This is what makes the signature
    distinguish real workflow differences while staying insensitive to
    pixel drift between runs. The string fields that remain are normalised
    through :func:`_abstract_dynamic`, so an application's version number does
    not fork one workflow into two.
    """
    data = action.model_dump(exclude_none=True)
    data.pop("type", None)
    kept = {k: data[k] for k in _SEMANTIC_KEYS if k in data and k not in _COORDINATE_KEYS}
    rendered: list[str] = []
    for key in sorted(kept):
        value = kept[key]
        if isinstance(value, str):
            value = _abstract_dynamic(value)
        rendered.append(f"{key}={value}")
    return ",".join(rendered)


def _compact_params(action: Action) -> str:
    """Summarize an action's params to a short stable string for the record.

    Screen coordinates are deliberately omitted. They are only meaningful on
    the screen that produced them: the window that was at (404, 227) yesterday
    is a different link today, and a stored skill that names one invites the
    model to click it again. Measured: replaying a distilled skill took 18
    steps where the cold run took 10, and the skill's own text told the agent
    to click a coordinate belonging to a story that had since moved. The
    sub-goal preceding each step already says *what* was being clicked, which
    is the part that transfers.

    They were already excluded from the de-duplication signature for a related
    reason — UI drift must not fork one workflow into many skills.
    """
    data = action.model_dump(exclude_none=True)
    data.pop("type", None)
    for key in _COORDINATE_KEYS:
        data.pop(key, None)
    return ",".join(f"{k}={data[k]}" for k in sorted(data))


def _slug(app: str) -> str:
    """Lowercase ASCII id for an application name (pure)."""
    return ascii_slug(app, max_chars=APP_SLUG_MAX_CHARS)
