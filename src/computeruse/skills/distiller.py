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

from computeruse.orchestrator.schemas import (
    Action,
    ActivateApp,
    CallTool,
    ClickMark,
    Finish,
    MouseClick,
    PressHotkey,
    Wait,
)
from computeruse.skills.schemas import (
    UNINFORMATIVE_WORDS,
    FastPathActivateApp,
    FastPathAxPress,
    FastPathHotkey,
    FastPathInstruction,
    FastPathWait,
    SkillDefinition,
)
from computeruse.slug import ascii_slug


@dataclass(frozen=True)
class Trajectory:
    """Immutable record of one successful run (feed of ordered actions)."""

    app: str
    description: str
    steps: tuple[Action, ...]
    tags: tuple[str, ...] = ()
    step_descriptions: tuple[str, ...] = ()
    #: Accessibility identity of the element each step acted on, positionally
    #: aligned with ``steps``; ``""`` where the run could not tell. This is the
    #: *only* run-stable answer to "what did the click hit" — coordinates drift
    #: and the step description is the model's prose — so it is what keeps two
    #: different workflows from sharing one signature (see :func:`signature_of`).
    step_targets: tuple[str, ...] = ()


@dataclass(frozen=True)
class DistillResult:
    """Outcome of attempting to distill a trajectory into a skill."""

    kind: Literal["skill", "too_short", "duplicate"]
    definition: SkillDefinition | None = None
    signature: str | None = None


_MIN_STEPS: int = 2
APP_SLUG_MAX_CHARS: Final[int] = 60
_DYNAMIC_NUMBER: Final = re.compile(r"\d+(?:[.,]\d+)*")
_DYNAMIC_URL: Final = re.compile(r"https?://\S+", flags=re.IGNORECASE)


def _abstract_dynamic(text: str) -> str:
    """Template the dynamic fragments of a signature input (pure)."""
    templated = _DYNAMIC_URL.sub("<url>", text.lower())
    return _DYNAMIC_NUMBER.sub("<num>", templated)


def signature_of(trajectory: Trajectory) -> str:
    """A stable content-hash describing the workflow's action sequence.

    Coordinates are deliberately excluded. ``step_targets`` contributes the
    stable AX identity of positional actions so two same-shaped workflows that
    target different controls do not collapse into one skill.
    """
    targets = trajectory.step_targets
    flow: list[dict[str, str]] = []
    for index, step in enumerate(trajectory.steps):
        entry: dict[str, str] = {
            "type": step.type,
            "params": _semantic_params(step),
        }
        target = targets[index] if index < len(targets) else ""
        if target:
            entry["target"] = _abstract_dynamic(target)
        flow.append(entry)
    payload = json.dumps(
        {"app": trajectory.app, "flow": flow}, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest[:16]


def fast_path_from_trajectory(
    trajectory: Trajectory,
) -> tuple[FastPathInstruction, ...]:
    """Extract the replay-safe semantic subset of one verified trajectory.

    V1 is deliberately all-or-nothing. If any step needs a dynamic operand,
    pointer geometry, an unstable mark index, a tool result, or an unknown AX
    target, the whole fast path is refused. A partial recipe is worse than no
    recipe because it can leave the normal provider halfway through a workflow
    it did not choose.
    """
    instructions: list[FastPathInstruction] = []
    for index, action in enumerate(trajectory.steps):
        if isinstance(action, Finish):
            continue
        if isinstance(action, (MouseClick, ClickMark)):
            if action.button != "left" or action.click_count != 1:
                return ()
            target = (
                trajectory.step_targets[index]
                if index < len(trajectory.step_targets)
                else ""
            )
            target = " ".join(target.split())
            if not target:
                return ()
            instructions.append(FastPathAxPress(type="ax_press", target=target))
            continue
        if isinstance(action, PressHotkey):
            instructions.append(
                FastPathHotkey(
                    type="press_hotkey",
                    modifiers=tuple(action.modifiers),
                    key=action.key,
                )
            )
            continue
        if isinstance(action, ActivateApp):
            instructions.append(
                FastPathActivateApp(type="activate_app", app=action.app)
            )
            continue
        if isinstance(action, Wait):
            if action.duration_ms > 5000:
                return ()
            instructions.append(
                FastPathWait(
                    type="wait",
                    duration_ms=action.duration_ms,
                    reason=action.reason,
                )
            )
            continue
        return ()
    return tuple(instructions)


def distill(trajectory: Trajectory, known_signatures: set[str]) -> DistillResult:
    """Turn a trajectory into a skill, unless it's trivial or already known."""
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
        skill_id=f"{_slug(trajectory.app)}.{signature}",
        description=trajectory.description,
        app=trajectory.app,
        tags=trajectory.tags or derive_tags(trajectory),
        steps=steps_readable,
        fast_path=fast_path_from_trajectory(trajectory),
        signature=signature,
    )
    return DistillResult(kind="skill", definition=definition, signature=signature)


TAG_LIMIT: Final[int] = 12


def visited_apps(trajectory: Trajectory) -> tuple[str, ...]:
    """Every application the run touched, primary first (pure)."""
    apps: list[str] = []
    for name in (trajectory.app,) + tuple(
        step.app for step in trajectory.steps if isinstance(step, ActivateApp)
    ):
        if name not in apps:
            apps.append(name)
    return tuple(apps)


def _app_tag_words(app: str) -> tuple[str, ...]:
    """An app name as tag tokens (pure)."""
    cleaned = re.sub(r"[^\w]+", " ", app.lower(), flags=re.UNICODE)
    return tuple(
        token
        for token in cleaned.split()
        if len(token) >= 3 and token not in UNINFORMATIVE_WORDS
    )


def derive_tags(trajectory: Trajectory) -> tuple[str, ...]:
    """Search keywords for a skill, taken from what the run actually did (pure)."""

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


_COORDINATE_KEYS: frozenset[str] = frozenset(
    {"x", "y", "start_x", "start_y", "end_x", "end_y"}
)
_SEMANTIC_KEYS: frozenset[str] = frozenset(
    {
        "key",
        "modifiers",
        "button",
        "click_count",
        "skill_id",
        "app",
        "tool",
    }
)


def _semantic_params(action: Action) -> str:
    """Return a stable, coordinate-free summary of an action's params."""
    data = action.model_dump(exclude_none=True)
    data.pop("type", None)
    kept = {
        key: data[key]
        for key in _SEMANTIC_KEYS
        if key in data and key not in _COORDINATE_KEYS
    }
    rendered: list[str] = []
    if isinstance(action, CallTool):
        rendered.append(f"tool={action.tool}")
        if "code" in action.arguments:
            code = str(action.arguments["code"])
            calls = re.findall(
                r"\b(click|typeText|pressKey|drag|setValue|scroll|navigate)"
                r"\s*\(\s*['\"]?([^'\"\)\n]+)",
                code,
            )
            if calls:
                sig_calls = ";".join(
                    f"{method}:{_abstract_dynamic(target.strip())}"
                    for method, target in calls[:4]
                )
                rendered.append(f"calls={sig_calls}")
            else:
                rendered.append(f"code_len={len(code)}")
        elif action.arguments:
            sorted_args = sorted(
                (key, _abstract_dynamic(str(value)[:40]))
                for key, value in action.arguments.items()
                if key not in _COORDINATE_KEYS
            )
            rendered.append(f"args={sorted_args}")
    for key in sorted(kept):
        if key == "tool":
            continue
        value = kept[key]
        if isinstance(value, str):
            value = _abstract_dynamic(value)
        rendered.append(f"{key}={value}")
    return ",".join(rendered)


def _compact_params(action: Action) -> str:
    """Summarize an action's params without persisting screen coordinates."""
    data = action.model_dump(exclude_none=True)
    data.pop("type", None)
    for key in _COORDINATE_KEYS:
        data.pop(key, None)
    return ",".join(f"{key}={data[key]}" for key in sorted(data))


def _slug(app: str) -> str:
    """Lowercase ASCII id for an application name (pure)."""
    return ascii_slug(app, max_chars=APP_SLUG_MAX_CHARS)