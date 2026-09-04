"""Strictly-typed action contracts for the agent orchestration layer.

Every payload the orchestrator exchanges with the actuation micro-driver is
validated here. Pydantic v2 discriminated unions let weak LLMs be guided back
to a *single* valid shape at parse time (Law 2): an invalid action fails the
gate before it can reach the physical layer.

These models are pure data transformers and never perform OS I/O (Law 6).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from computeruse.skills.schemas import SKILL_ID_PATTERN

Modifier = Literal["command", "shift", "alt", "control"]


def empty_modifiers() -> list[Modifier]:
    """Return an empty modifier list (typed factory for Pydantic defaults)."""
    return []


class MouseMove(BaseModel):
    type: Literal["mouse_move"]
    x: int = Field(ge=0, description="Target virtual-pixel X coordinate.")
    y: int = Field(ge=0, description="Target virtual-pixel Y coordinate.")
    duration_ms: int = Field(ge=0, default=180, description="Trajectory duration.")


class MouseClick(BaseModel):
    type: Literal["mouse_click"]
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    button: Literal["left", "right", "middle"] = "left"
    click_count: Literal[1, 2] = 1


class ClickMark(BaseModel):
    """Click a numbered element from the AX list, by its mark (ADR-2).

    The reliable way to hit a target the accessibility tree already knows
    about. A coordinate the model reads off the screenshot is a guess through
    a lossy channel — the map is roughly 3.3 logical points per image pixel, so
    a 12-point-tall link is under four pixels — while a mark resolves to that
    element's own centre in logical points, exactly. ``mouse_click`` remains
    for everything the AX list does not cover.
    """

    type: Literal["click_mark"]
    mark: int = Field(ge=1, description="Index shown as [N] in the AX element list.")
    button: Literal["left", "right", "middle"] = "left"
    click_count: Literal[1, 2] = 1


class MouseDrag(BaseModel):
    type: Literal["mouse_drag"]
    start_x: int = Field(ge=0)
    start_y: int = Field(ge=0)
    end_x: int = Field(ge=0)
    end_y: int = Field(ge=0)
    # Requested total drag time; the driver stretches it for long distances
    # so a drag reads as a continuous hand motion, never a teleport (Law 1).
    duration_ms: int = Field(ge=0, default=200)


class MouseScroll(BaseModel):
    type: Literal["mouse_scroll"]
    dx: int
    dy: int


class TypeText(BaseModel):
    type: Literal["type_text"]
    text: str
    wpm: int = Field(ge=1, default=40, description="Words per minute typing speed.")


class ClipboardPaste(BaseModel):
    type: Literal["clipboard_paste"]
    text: str = Field(description="Text to paste directly into the focused field via clipboard (Cmd+V).")


class PressHotkey(BaseModel):
    type: Literal["press_hotkey"]
    modifiers: list[Modifier] = Field(default_factory=empty_modifiers)
    key: str


class ActivateApp(BaseModel):
    type: Literal["activate_app"]
    app: str = Field(min_length=1, description="Application name to bring to the front.")


class LoadSkill(BaseModel):
    """Stage-2 mount request — the one action whose payload names a file.

    ``skill_id`` carries the store's id pattern for the same reason the stored
    models do, and it is the copy that matters most: this is the id a *model*
    produces, downstream of every screen-derived string the run has read. The
    constraint was missing here while both stored models enforced it, so
    ``{"type": "load_skill", "skill_id": "../../etc/passwd"}`` validated
    cleanly and reached a path join.
    """

    type: Literal["load_skill"]
    skill_id: str = Field(pattern=SKILL_ID_PATTERN)


class Wait(BaseModel):
    type: Literal["wait"]
    duration_ms: int = Field(ge=0)
    reason: str


class WebSearch(BaseModel):
    """Look something up rather than hunting for it on screen.

    A non-physical action: no cursor, no focus, nothing the user has to share
    the machine with. It bridges to a connected MCP search tool (Tavily, Exa,
    Brave) when one exists; otherwise it answers with instructions to use the
    web browser directly (open Google Chrome, use Cmd+L, search, and read
    results from the screen).
    """

    type: Literal["web_search"]
    query: str = Field(min_length=1, max_length=400)


class WebFetch(BaseModel):
    """Read a page's text directly instead of scrolling through it."""

    type: Literal["web_fetch"]
    url: str = Field(min_length=1, max_length=2048)


class CallTool(BaseModel):
    """Invoke a tool borrowed from a Model Context Protocol server.

    One action for every tool, rather than one action type per tool: the schema
    stays closed — a model cannot invent an action shape the loop will not
    recognise — while the set of tools stays open and is discovered at startup.
    Which names are valid is stated in the prompt; an unknown one comes back as
    a failed call listing what does exist.
    """

    type: Literal["call_tool"]
    tool: str = Field(min_length=1, max_length=200)
    arguments: dict[str, object] = Field(default_factory=dict)


class Finish(BaseModel):
    type: Literal["finish"]
    status: Literal["success", "failed"]
    summary: str


Action = (
    MouseMove
    | MouseClick
    | ClickMark
    | MouseDrag
    | MouseScroll
    | TypeText
    | ClipboardPaste
    | WebSearch
    | WebFetch
    | CallTool
    | PressHotkey
    | ActivateApp
    | LoadSkill
    | Wait
    | Finish
)

#: The discriminated form, shared by the single action and the batch list so
#: both validate through one tag lookup instead of Pydantic's smart-union
#: trial-and-error (which can settle on the wrong member for two models with
#: compatible fields).
#:
#: Derived from :data:`Action` rather than re-listing the members: the union is
#: this project's wire contract, and two copies of it would eventually disagree
#: about which actions exist — the drift test compares Python against Rust, not
#: against another copy of Python.
ActionUnion = Annotated[Action, Field(discriminator="type")]


class AgentTurn(BaseModel):
    """Single OODA loop decision frame emitted by the LLM.

    OpenAI's computer-use agent (CUA) emits *action sequences* — several
    actions per model turn, executed in order — which cuts the per-step LLM
    round-trips that dominate wall-clock time. ``actions`` is that batch:
    when present, the loop executes each action in sequence within one turn
    (verifying each one and stopping the batch on the first failure).
    ``action`` remains the single-action form and is always required so the
    model emits one canonical lead action; for a batch it must repeat the
    first element of ``actions``.
    """

    thought: str
    sub_goal: str
    action: ActionUnion
    # Optional ordered batch executed within this single turn. Validated to be
    # non-empty and to place ``finish`` (if present) strictly last — a batch
    # must never continue acting after the run has ended.
    actions: list[ActionUnion] | None = None

    @model_validator(mode="after")
    def _validate_batch(self) -> AgentTurn:
        if self.actions is None:
            return self
        if not self.actions:
            raise ValueError("'actions' must contain at least one action")
        for index, item in enumerate(self.actions):
            if item.type == "finish" and index != len(self.actions) - 1:
                raise ValueError("'finish' must be the last action of a batch")
        return self


class _ActionEnvelope(BaseModel):
    """Internal: the discriminated union, addressable on its own."""

    action: ActionUnion


def action_from_payload(payload: dict[str, object]) -> Action | None:
    """Rebuild an action from a dict it was serialised into (pure).

    The approval queue stores an action as ``model_dump`` output so a person
    can read it, and something later has to read it back — deciding what a
    parked action delegates means knowing what it *is*, not what it looked
    like. Returns ``None`` for a payload that no longer parses (a record from
    an older schema, a hand-edited file), because a queue entry that cannot be
    understood is exactly the one nothing should be inferred from.
    """
    try:
        return _ActionEnvelope.model_validate({"action": payload}).action
    except ValueError:
        return None
