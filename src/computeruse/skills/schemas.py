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

#: What a skill id may be. A skill id becomes a filename in the store, so this
#: is a security boundary and not merely a naming convention: no separators, no
#: dot-dot, nothing that could climb out of the directory. Named here — rather
#: than repeated inline — because the *model* also emits skill ids
#: (``LoadSkill``), and the two definitions drifting apart is exactly how
#: ``load_skill`` with ``"../../etc/passwd"`` once passed validation.
SKILL_ID_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9._-]*$"


#: Longest description the Stage-1 index may carry (Law 3.2, zero context
#: bloat): every summary is in the agent's context on every turn, so this is a
#: hard budget rather than a formatting preference.
SUMMARY_DESCRIPTION_MAX: Final[int] = 200


def condense_description(description: str) -> str:
    """Fit a description into the Stage-1 budget (pure).

    Two-stage retrieval says the *summary* is small and the *definition* is
    allowed to be long — that is the whole point of loading a skill on demand.
    :func:`summary_of` used to copy the description across verbatim anyway, so
    a definition longer than the budget produced a summary that failed its own
    validation.

    That failure was silent in the worst way: distillation wrote the skill,
    the run reported ``distill: skill (textedit.abae...)`` as a success, and
    the registry then skipped the file on every subsequent read. Measured on a
    live two-application run, where goals are naturally long: both skills the
    session distilled were 265 and 320 characters, so the whole of Law 3 —
    learn from a successful run, reuse it later — was a no-op for that
    session and every session after it.

    Truncation is at a word boundary where one is available, because a
    summary is read by a model deciding whether to load the skill, and a
    description cut mid-word reads as corruption.
    """
    collapsed = " ".join(description.split())
    if len(collapsed) <= SUMMARY_DESCRIPTION_MAX:
        return collapsed
    # One character of the budget belongs to the ellipsis.
    head = collapsed[: SUMMARY_DESCRIPTION_MAX - 1]
    cut = head.rfind(" ")
    # Only honour a word boundary that is not so early it throws the summary
    # away; otherwise a single long token would leave a two-word description.
    if cut >= SUMMARY_DESCRIPTION_MAX // 2:
        head = head[:cut]
    return head.rstrip() + "\u2026"


class SkillSummary(BaseModel):
    """Stage-1 payload: the only thing that lives in the agent context."""

    skill_id: str = Field(pattern=SKILL_ID_PATTERN)
    description: str = Field(min_length=1, max_length=SUMMARY_DESCRIPTION_MAX)
    app: str
    uses: int = Field(default=0, ge=0)
    wins: int = Field(default=0, ge=0)
    tags: tuple[str, ...] = Field(default=(), description="Search keywords.")
    parameters: tuple[str, ...] = Field(
        default=(), description="Parameter slot names (e.g. ('query', 'url'))."
    )
    version: int = Field(default=1, ge=1)


class SkillDefinition(BaseModel):
    """Stage-2 payload: full body loaded on demand into active context."""

    skill_id: str = Field(pattern=SKILL_ID_PATTERN)
    description: str
    app: str
    tags: tuple[str, ...] = Field(default=(), description="Search keywords.")
    parameters: tuple[str, ...] = Field(
        default=(), description="Parameter slot names (e.g. ('query', 'url'))."
    )
    version: int = Field(default=1, ge=1)
    #: How the skill has actually fared when reused. Distillation used to be
    #: the end of a skill's story: it was written once and never judged again,
    #: so a recipe that led three runs astray was offered to a fourth with the
    #: same confidence as one that had worked every time. A skill is a claim
    #: about how to do something, and a claim that keeps failing should stop
    #: being made.
    uses: int = Field(default=0, ge=0, description="Runs that mounted this skill.")
    wins: int = Field(default=0, ge=0, description="Those runs that succeeded.")
    steps: tuple[str, ...] = Field(description="Human-readable ordered steps.")
    # Canonical signature makes the distiller's novelty check cheap: identical
    # action sequences collapse to the same signature without re-analysis.
    signature: str
    phase: Literal["proven", "draft"] = "draft"


def summary_of(definition: SkillDefinition) -> SkillSummary:
    """Derive the Stage-1 summary from a definition (pure projection).

    The description is condensed rather than copied: the definition may be as
    long as it needs to be, the summary may not (see
    :func:`condense_description`).
    """
    return SkillSummary(
        skill_id=definition.skill_id,
        description=condense_description(definition.description),
        app=definition.app,
        # Tags are the search surface for the Stage-1 scan, so they must be
        # projected into the summary or tag-matching would be dead code.
        tags=definition.tags,
        parameters=definition.parameters,
        version=definition.version,
        # The track record is projected too: ranking happens over summaries,
        # so a skill's history has to travel with the thing being ranked.
        uses=definition.uses,
        wins=definition.wins,
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
        uses=definition.uses,
        wins=definition.wins,
        steps=tuple(new_steps),
        signature=definition.signature,
        phase=definition.phase,
    )