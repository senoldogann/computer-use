"""Strictly-typed action contracts for the agent orchestration layer.

Every payload the orchestrator exchanges with the actuation micro-driver is
validated here. Pydantic v2 discriminated unions let weak LLMs be guided back
to a *single* valid shape at parse time (Law 2): an invalid action fails the
gate before it can reach the physical layer.

These models are pure data transformers and never perform OS I/O (Law 6).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

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
    type: Literal["load_skill"]
    skill_id: str


class Wait(BaseModel):
    type: Literal["wait"]
    duration_ms: int = Field(ge=0)
    reason: str


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
    | PressHotkey
    | ActivateApp
    | LoadSkill
    | Wait
    | Finish
)


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
    action: Action = Field(discriminator="type")
    # Optional ordered batch executed within this single turn. Validated to be
    # non-empty and to place ``finish`` (if present) strictly last — a batch
    # must never continue acting after the run has ended.
    actions: list[Action] | None = None

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