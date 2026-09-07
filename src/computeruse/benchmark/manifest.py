"""Frozen benchmark manifest: scenarios, outcome vocabulary, drift gate.

Every scenario carries its exact prompt, required start state, expected
outcome drawn from a fixed vocabulary, and checkable success criteria. The
manifest hash covers the complete :class:`Scenario` contract. Editing any
field that can change what a run means without deliberately updating the
frozen pin is rejected, so a later "20/20" cannot quietly mean an easier 20.
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
    """The frozen manifest contract is malformed or changed unexpectedly."""


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
    if not isinstance(scenario_id, str) or not scenario_id.strip():
        raise BenchmarkDriftError("scenario without a scenario_id")

    def required_text(key: str) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise BenchmarkDriftError(f"scenario {scenario_id} has an empty {key}")
        return value

    name = required_text("name")
    category = required_text("category")
    difficulty = required_text("difficulty")
    prompt = required_text("prompt")
    start_state = required_text("start_state")

    expected = raw.get("expected_outcome")
    if expected not in OUTCOME_VOCABULARY:
        raise BenchmarkDriftError(
            f"scenario {scenario_id} has outcome {expected!r} outside the vocabulary"
        )

    criteria_raw = raw.get("criteria")
    if not isinstance(criteria_raw, list) or not criteria_raw:
        raise BenchmarkDriftError(f"scenario {scenario_id} has no checkable criteria")
    criteria: list[str] = []
    for entry in cast(list[object], criteria_raw):
        if not isinstance(entry, str) or not entry.strip():
            raise BenchmarkDriftError(
                f"scenario {scenario_id} has an uncheckable criterion"
            )
        criteria.append(entry)

    live_only = raw.get("live_only", True)
    if not isinstance(live_only, bool):
        raise BenchmarkDriftError(f"scenario {scenario_id} has a non-boolean live_only")

    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        raise BenchmarkDriftError(f"scenario {scenario_id} has non-text notes")

    return Scenario(
        scenario_id=scenario_id,
        name=name,
        category=category,
        difficulty=difficulty,
        prompt=prompt,
        start_state=start_state,
        expected_outcome=cast(str, expected),
        success_criteria=tuple(criteria),
        live_only=live_only,
        notes=notes,
    )


def load_scenarios(path: Path) -> tuple[Scenario, ...]:
    """Load and validate the frozen scenario list (I/O shell)."""
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise BenchmarkDriftError(f"{path} is not a scenario manifest object")
    raw = cast(dict[str, object], decoded)

    version = raw.get("manifest_version")
    if version != MANIFEST_VERSION:
        raise BenchmarkDriftError(
            f"manifest_version mismatch: expected {MANIFEST_VERSION!r}, got {version!r}"
        )

    entries = raw.get("scenarios")
    if not isinstance(entries, list):
        raise BenchmarkDriftError(f"{path} is not a scenario manifest")
    parsed: list[Scenario] = []
    for index, entry in enumerate(cast(list[object], entries)):
        if not isinstance(entry, dict):
            raise BenchmarkDriftError(f"scenario entry {index} is not an object")
        parsed.append(_scenario_from_dict(cast(dict[str, object], entry)))

    scenarios = tuple(parsed)
    seen = [scenario.scenario_id for scenario in scenarios]
    if len(set(seen)) != len(seen):
        raise BenchmarkDriftError("duplicate scenario_id in manifest")
    return scenarios


def manifest_hash(scenarios: tuple[Scenario, ...]) -> str:
    """sha256 over the complete canonical scenario semantics (pure)."""
    canonical = json.dumps(
        [
            {
                "scenario_id": scenario.scenario_id,
                "name": scenario.name,
                "category": scenario.category,
                "difficulty": scenario.difficulty,
                "prompt": scenario.prompt,
                "start_state": scenario.start_state,
                "expected_outcome": scenario.expected_outcome,
                "success_criteria": list(scenario.success_criteria),
                "live_only": scenario.live_only,
                "notes": scenario.notes,
            }
            for scenario in scenarios
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def assert_frozen(scenarios: tuple[Scenario, ...], expected_hash: str) -> None:
    """Fail closed when frozen scenario semantics no longer match the pin."""
    actual = manifest_hash(scenarios)
    if actual != expected_hash:
        raise BenchmarkDriftError(
            "benchmark manifest drifted: frozen scenario semantics changed "
            f"(expected {expected_hash[:12]}…, got {actual[:12]}…). "
            "Review the semantic change and update the version/hash deliberately, "
            "or revert the edit."
        )
