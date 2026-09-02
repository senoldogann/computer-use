"""Skill schemas for the two-stage retrieval store (Law 3).

Law 3 forbids context bloat: the working context must contain only lightweight
*summaries*, and the full skill body loads on demand. These two models are the
physical embodiment of that split — :class:`SkillSummary` is the Stage-1 index
entry (a few fields, cheap to scan), :class:`SkillDefinition` is the Stage-2
full body (loaded per skill id when the orchestrator decides a skill applies).
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, Field


#: Grammatical filler, carrying no topic at all. Split from the workflow noise
#: below because they are different kinds of uselessness: these are noise in
#: any text, those are noise only in this domain.
STOP_WORDS: Final[frozenset[str]] = frozenset(
    {
        "the", "a", "an", "in", "on", "at", "to", "for", "of", "and", "or",
        "is", "it", "with", "as", "by", "from", "into", "its", "be", "was",
    }
)

#: Words that describe *any* agent workflow and so distinguish none of them.
#: Shared by tagging and search because they must agree: tagging every skill
#: with "click" or "open" and then scoring queries on those words makes the
#: whole store match every query equally, which is the same as matching none.
#: Distinct from grammatical stop-words ("the", "and") — these are domain noise.
WORKFLOW_NOISE_WORDS: Final[frozenset[str]] = frozenset(
    {
        "click", "clicks", "clicked", "open", "opens", "opened", "press",
        "type", "typed", "enter", "select", "go", "goes", "navigate", "use",
        "then", "next", "step", "this", "that", "app", "application",
        "window", "button", "page", "screen", "current", "visible", "make",
        "sure", "confirm", "read", "find", "get", "set", "show",
    }
)

#: Everything a keyword must not be, for tagging and for search alike.
UNINFORMATIVE_WORDS: Final[frozenset[str]] = STOP_WORDS | WORKFLOW_NOISE_WORDS


class SkillSummary(BaseModel):
    """Stage-1 payload: the only thing that lives in the agent context."""

    skill_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    description: str = Field(min_length=1, max_length=200)
    app: str
    tags: tuple[str, ...] = Field(default=(), description="Search keywords.")
    parameters: tuple[str, ...] = Field(
        default=(), description="Parameter slot names (e.g. ('query', 'url'))."
    )
    version: int = Field(default=1, ge=1)


class SkillDefinition(BaseModel):
    """Stage-2 payload: full body loaded on demand into active context."""

    skill_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    description: str
    app: str
    tags: tuple[str, ...] = Field(default=(), description="Search keywords.")
    parameters: tuple[str, ...] = Field(
        default=(), description="Parameter slot names (e.g. ('query', 'url'))."
    )
    version: int = Field(default=1, ge=1)
    steps: tuple[str, ...] = Field(description="Human-readable ordered steps.")
    # Canonical signature makes the distiller's novelty check cheap: identical
    # action sequences collapse to the same signature without re-analysis.
    signature: str
    phase: Literal["proven", "draft"] = "draft"


def summary_of(definition: SkillDefinition) -> SkillSummary:
    """Derive the Stage-1 summary from a definition (pure projection)."""
    return SkillSummary(
        skill_id=definition.skill_id,
        description=definition.description,
        app=definition.app,
        # Tags are the search surface for the Stage-1 scan, so they must be
        # projected into the summary or tag-matching would be dead code.
        tags=definition.tags,
        parameters=definition.parameters,
        version=definition.version,
    )


def instantiate_skill(
    definition: SkillDefinition, values: dict[str, str]
) -> SkillDefinition:
    """Substitute {{slot}} placeholders in skill description and steps (pure)."""
    desc = definition.description
    for key, val in values.items():
        desc = desc.replace(f"{{{{{key}}}}}", val)
    new_steps: list[str] = []
    for step in definition.steps:
        s = step
        for key, val in values.items():
            s = s.replace(f"{{{{{key}}}}}", val)
        new_steps.append(s)
    return SkillDefinition(
        skill_id=definition.skill_id,
        description=desc,
        app=definition.app,
        tags=definition.tags,
        parameters=definition.parameters,
        version=definition.version,
        steps=tuple(new_steps),
        signature=definition.signature,
        phase=definition.phase,
    )