"""Frozen benchmark manifest: scenarios, outcome vocabulary, drift gate.

Every scenario carries its exact prompt, required start state, expected
outcome drawn from a fixed vocabulary, and checkable success criteria. The
manifest hash covers (scenario_id, prompt) pairs: editing a prompt without
bumping ``MANIFEST_VERSION`` fails the freeze test, so a later "20/20" can
never silently mean an easier 20.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

#: Every benchmark outcome must be one of these. ``expected_pass`` means the
#: run's end state matched ``success_criteria``; ``interrupted`` and
#: ``confirmation_required`` are terminal non-successes and must never be
#: relabelled as passes in a report.
OUTCOME_VOCABULARY: Final[tuple[str, ...]] = (
    "expected_pass",
    "expected_fail",
    "interrupted",
    "confirmation_required",
    "blocked",
    "error",
)

MANIFEST_VERSION: Final[str] = "v1"


class BenchmarkDriftError(ValueError):
    """The manifest changed without a version bump (fail-closed)."""


@dataclass(frozen=True)
class Scenario:
    """One frozen benchmark scenario (pure data)."""

    scenario_id: str
    name: str
    category: str
    difficulty: str
    prompt: str
    start_state: str
    expected_outcome: str
    success_criteria: tuple[str, ...]
    live_only: bool = True
    notes: str = ""


def _scenario_from_dict(raw: dict[str, object]) -> Scenario:
    """Validate one manifest entry; raise on any contract breach (pure)."""
    scenario_id = raw.get("scenario_id")
    prompt = raw.get("prompt")
    expected = raw.get("expected_outcome")
    criteria_raw = raw.get("criteria")
    if not isinstance(scenario_id, str) or not scenario_id:
        raise BenchmarkDriftError("scenario without a scenario_id")
    if not isinstance(prompt, str) or not prompt.strip():
        raise BenchmarkDriftError(f"scenario {scenario_id} has an empty prompt")
    if expected not in OUTCOME_VOCABULARY:
        raise BenchmarkDriftError(
            f"scenario {scenario_id} has outcome {expected!r} outside the vocabulary"
        )
    if not isinstance(criteria_raw, list) or not criteria_raw:
        raise BenchmarkDriftError(f"scenario {scenario_id} has no checkable criteria")
    criteria: list[str] = []
    for entry in cast(list[object], criteria_raw):
        if not isinstance(entry, str) or not entry.strip():
            raise BenchmarkDriftError(
                f"scenario {scenario_id} has an uncheckable criterion"
            )
        criteria.append(entry)

    def text(key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    return Scenario(
        scenario_id=scenario_id,
        name=text("name"),
        category=text("category"),
        difficulty=text("difficulty"),
        prompt=prompt,
        start_state=text("start_state"),
        expected_outcome=str(expected),
        success_criteria=tuple(str(item) for item in criteria),
        live_only=bool(raw.get("live_only", True)),
        notes=text("notes"),
    )


def load_scenarios(path: Path) -> tuple[Scenario, ...]:
    """Load and validate the frozen scenario list (I/O shell)."""
    raw = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    entries = raw.get("scenarios")
    if not isinstance(entries, list):
        raise BenchmarkDriftError(f"{path} is not a scenario manifest")
    parsed: list[Scenario] = []
    for entry in cast(list[object], entries):
        if isinstance(entry, dict):
            parsed.append(_scenario_from_dict(cast(dict[str, object], entry)))
    scenarios = tuple(parsed)
    seen = [scenario.scenario_id for scenario in scenarios]
    if len(set(seen)) != len(seen):
        raise BenchmarkDriftError("duplicate scenario_id in manifest")
    return scenarios


def manifest_hash(scenarios: tuple[Scenario, ...]) -> str:
    """sha256 over the canonical (scenario_id, prompt) pairs (pure)."""
    canonical = json.dumps(
        [{"id": scenario.scenario_id, "prompt": scenario.prompt} for scenario in scenarios],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def assert_frozen(scenarios: tuple[Scenario, ...], expected_hash: str) -> None:
    """Fail-closed: any prompt edit without a version bump raises."""
    actual = manifest_hash(scenarios)
    if actual != expected_hash:
        raise BenchmarkDriftError(
            "benchmark manifest drifted: a scenario prompt changed without a "
            f"version bump (expected {expected_hash[:12]}…, got {actual[:12]}…). "
            "Bump MANIFEST_VERSION and record the change, or revert the edit."
        )
