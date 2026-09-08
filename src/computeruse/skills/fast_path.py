"""Deterministic verified-skill provider that reuses the normal OODA pipeline.

This module deliberately does not execute anything. It converts a tiny,
coordinate-free skill instruction vocabulary into ordinary ``AgentTurn``
objects. ``OodaRunner`` remains the only component that can validate, guard,
act, verify, recover, audit completion, and honour the kill switch.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Final

from computeruse.orchestrator.schemas import (
    ActivateApp,
    AgentTurn,
    Finish,
    MouseClick,
    PressHotkey,
    Wait,
)
from computeruse.skills.schemas import (
    FastPathActivateApp,
    FastPathAxPress,
    FastPathHotkey,
    FastPathInstruction,
)

if TYPE_CHECKING:
    from computeruse.orchestrator.loop import WorkingState


_AX_LINE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<identity>.+?)\s+at\s+"
    r"\((?P<x>-?\d+(?:\.\d+)?),(?P<y>-?\d+(?:\.\d+)?)\)\s+"
    r"(?P<width>\d+(?:\.\d+)?)x(?P<height>\d+(?:\.\d+)?)$"
)


def _normalize_identity(value: str) -> str:
    return " ".join(value.split())


def resolve_ax_target(target: str, ui_elements: tuple[str, ...]) -> MouseClick | None:
    """Resolve one exact semantic AX identity against the current observation.

    Stored coordinates and mark numbers are forbidden. Every fast-path click
    therefore asks the live AX surface for the control again. Exact identity is
    required and duplicate identities are ambiguous, so either case fails open
    to the normal provider instead of guessing.
    """
    wanted = _normalize_identity(target)
    matches: list[tuple[float, float, float, float]] = []
    for line in ui_elements:
        match = _AX_LINE.fullmatch(line.strip())
        if match is None:
            continue
        if _normalize_identity(match.group("identity")) != wanted:
            continue
        matches.append(
            (
                float(match.group("x")),
                float(match.group("y")),
                float(match.group("width")),
                float(match.group("height")),
            )
        )
    if len(matches) != 1:
        return None
    x, y, width, height = matches[0]
    # Construct through a valid non-negative payload, then preserve signed
    # global logical coordinates exactly like resolve_mark does for secondary
    # displays. These are fresh AX coordinates, never stored skill geometry.
    return MouseClick(type="mouse_click", x=0, y=0).model_copy(
        update={"x": round(x + width / 2), "y": round(y + height / 2)}
    )


def _turn_for_instruction(
    instruction: FastPathInstruction,
    state: WorkingState,
) -> AgentTurn | None:
    """Translate one typed instruction into the ordinary action contract."""
    if isinstance(instruction, FastPathAxPress):
        action = resolve_ax_target(instruction.target, state.ui_elements)
        if action is None:
            return None
        sub_goal = f"Press {instruction.target}"
    elif isinstance(instruction, FastPathHotkey):
        action = PressHotkey(
            type="press_hotkey",
            modifiers=list(instruction.modifiers),
            key=instruction.key,
        )
        sub_goal = f"Press shortcut {instruction.key}"
    elif isinstance(instruction, FastPathActivateApp):
        action = ActivateApp(type="activate_app", app=instruction.app)
        sub_goal = f"Activate {instruction.app}"
    else:
        action = Wait(
            type="wait",
            duration_ms=instruction.duration_ms,
            reason=instruction.reason,
        )
        sub_goal = instruction.reason
    return AgentTurn(
        thought="Use the verified semantic skill step against the current machine state.",
        sub_goal=sub_goal,
        action=action,
    )


class FastPathProvider:
    """Emit verified semantic steps, then permanently fall back on uncertainty.

    The wrapper owns only a cursor and observability counters. It never mutates
    ``WorkingState`` and never retries a failed fast-path step. The first error,
    stale target, or ambiguous observation hands control to the real provider
    for the remainder of the run.
    """

    def __init__(
        self,
        *,
        instructions: tuple[FastPathInstruction, ...],
        fallback: Callable[[WorkingState], AgentTurn],
    ) -> None:
        self._instructions = instructions
        self._fallback = fallback
        self._index = 0
        self._disabled = False
        self.provider_calls = 0
        self.fast_path_turns = 0

    def _call_fallback(self, state: WorkingState) -> AgentTurn:
        self.provider_calls += 1
        return self._fallback(state)

    def __call__(self, state: WorkingState) -> AgentTurn:
        if self._disabled or state.last_error is not None:
            self._disabled = True
            return self._call_fallback(state)

        if self._index >= len(self._instructions):
            self.fast_path_turns += 1
            return AgentTurn(
                thought="All verified skill steps completed; ask the normal completion auditor to verify the goal.",
                sub_goal="Verify completed fast-path workflow",
                action=Finish(
                    type="finish",
                    status="success",
                    summary="verified skill fast-path completed",
                ),
            )

        instruction = self._instructions[self._index]
        turn = _turn_for_instruction(instruction, state)
        if turn is None:
            self._disabled = True
            return self._call_fallback(state)

        self._index += 1
        self.fast_path_turns += 1
        return turn