"""Weak-model scaffolding — prompt construction & decision parsing (Law 2.1).

The constitution's core bet is **Prompt & Orchestration Supremacy**: never
assume the underlying LLM can do native tool-use reasoning. This module is the
orchestrator-side embodiment of that:

* :func:`decision_prompt` renders the full prompt from the immutable working
  state — goal, completed steps, the *last error* (Law 2 error injection), and
  the app's semantic knowledge — plus the action-contract instruction.
* :func:`parse_decision` turns the model's raw text into a validated
  :class:`AgentTurn` through the strict Pydantic gate, and raises
  :class:`InvalidDecisionError` with a *corrective hint* when the model
  hallucinates a shape.
* :func:`scaffolded_provider` wraps any ``Callable[[str], str]`` model so the
  OODA loop talks to a well-behaved provider: on a parse failure it appends
  the hint to the prompt and asks the model to try again (bounded retries).

Everything here is pure except the model call itself, which stays behind the
injected callable — so weak models, strong models, and deterministic fakes all
flow through identical scaffolding.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, cast

from computeruse.orchestrator.loop import WorkingState
from computeruse.orchestrator.schemas import AgentTurn

# The action contract, spelled out for a model that has never seen it. The
# exact JSON shape mirrors what `AgentTurn` validates at parse time, so the
# instruction and the gate can never disagree about the *shape*.
ACTION_CONTRACT: Final[str] = (
    "You are driving a physical macOS computer as an autonomous agent via closed-loop visual perception and natural input actuation.\n"
    "\n"
    "1. OUTPUT CONTRACT:\n"
    "Return exactly ONE valid JSON object. Output NOTHING outside the JSON object (no markdown formatting, no explanations, no text wrappers).\n"
    "The JSON object must match:\n"
    '{\n'
    '  "thought": "<brief evidence-based observation of the current screen supporting the action>",\n'
    '  "sub_goal": "<single immediate objective that moves the main goal forward>",\n'
    '  "action": {"type": "<action_type>", ...}\n'
    '}\n'
    "\n"
    "2. SUPPORTED ACTIONS:\n"
    '- mouse_click: {"type": "mouse_click", "x": int, "y": int, "button": "left|right|middle", "click_count": 1|2}\n'
    '- mouse_move: {"type": "mouse_move", "x": int, "y": int, "duration_ms": int (default 180)} — ONLY when hover, tooltip, or drag preparation is explicitly needed\n'
    '- mouse_drag: {"type": "mouse_drag", "start_x": int, "start_y": int, "end_x": int, "end_y": int, "duration_ms": int (default 400)}\n'
    '- mouse_scroll: {"type": "mouse_scroll", "dx": int, "dy": int} — scrolls at CURRENT cursor position (ensure cursor is over the target scrollable area first)\n'
    '- type_text: {"type": "type_text", "text": str, "wpm": int (default 50)}\n'
    '- clipboard_paste: {"type": "clipboard_paste", "text": str} — preferred for URLs, search queries, coding prompts, and long text (Cmd+V)\n'
    '- press_hotkey: {"type": "press_hotkey", "modifiers": ["command|shift|alt|control"], "key": str} — key: "return", "enter", "tab", "escape", "space", "backspace", "l", "t", "w", "a", "c", "v", etc.\n'
    '- activate_app: {"type": "activate_app", "app": str} — brings an application (e.g. "Google Chrome", "Notes", "Finder") to the front\n'
    '- wait: {"type": "wait", "duration_ms": int, "reason": str}\n'
    '- finish: {"type": "finish", "status": "success|failed", "summary": str} — emit ONLY when goal completion is visibly verified on screen\n'
    "\n"
    "3. CLOSED-LOOP AUTONOMOUS CONTROL (OBSERVE → DECIDE → ACT → VERIFY):\n"
    "   - The screenshot is the single authoritative source of truth for the current computer state.\n"
    "   - Never assume an action succeeded. After every meaningful action, inspect the next screenshot and verify whether the expected state transition occurred.\n"
    "   - If the expected state change did not occur, diagnose the new screen state before acting again.\n"
    "   - Maintain a clear distinction between: (a) observed visual facts, (b) inferred state, (c) intended outcome. Always prefer observed visual facts over assumptions.\n"
    "\n"
    "4. VISUAL GROUNDING & DISPLAY COORDINATES:\n"
    "   - Never assume fixed coordinates. Windows move, resize, and layouts adapt.\n"
    "   - Derive all click coordinates (x, y) directly from visible UI evidence on the current screenshot.\n"
    "   - Coordinate Space: The attached screenshot is at LOGICAL resolution — 1 image pixel == 1 screen point. Report x,y EXACTLY as they appear in the image; never apply Retina/scale math.\n"
    "   - Click directly in the center of the target link, button, or input field you wish to activate.\n"
    "   - AX UI elements list exact native-app coordinates (toolbar, menu, address bar). For web page content (search results, links, article text), the AX tree is often empty — ground on the screenshot directly in that case.\n"
    "\n"
    "5. SAFE BROWSER NAVIGATION & TEXT INPUT:\n"
    "   - When entering a URL or search query in Chrome/Safari: ALWAYS use Cmd+L first to select all existing text cleanly before pasting:\n"
    "     Step 1: press_hotkey: {'modifiers': ['command'], 'key': 'l'} (focuses and selects entire address bar)\n"
    "     Step 2: clipboard_paste: {'text': '<target URL or query>'} (replaces selection cleanly without appending)\n"
    "     Step 3: press_hotkey: {'modifiers': [], 'key': 'return'} (submits the URL immediately — do not leave unsubmitted)\n"
    "   - NEVER call clipboard_paste twice in a row on an address bar without pressing Return first.\n"
    "   - When typing into any text input: ensure the field is focused and cleared before entering text.\n"
    "   - If target content (e.g. search results, repo links) is ALREADY visible on screen, DO NOT touch the address bar — click the visible target directly.\n"
    "\n"
    "6. SCROLLING & OFF-SCREEN TARGETS (CRITICAL):\n"
    "   - Web pages are often taller than the viewport. If the element you need (menu, button, link, avatar, sign-out option) is NOT visible in the current screenshot, it may be off-screen — DO NOT guess its coordinate and DO NOT click blindly. Scroll to bring it into view.\n"
    "   - To scroll DOWN (see content below): first move the cursor over the page content area, then mouse_scroll with dy=positive (e.g. {\"dy\": 300}).\n"
    "   - To scroll UP (see content above): mouse_scroll with dy=negative (e.g. {\"dy\": -300}).\n"
    "   - Re-observe the new screenshot after each scroll. Repeat small scrolls until the target is visible; only then click the center coordinate read from the screenshot.\n"
    "   - If scrolling produces no visible change you may not be over a scrollable area — move to the page center first.\n"
    "   - When looking for a UI element (avatar, settings, sign out) that is not on screen: scroll UP first (page headers/menus are usually at the top), then scroll down if needed.\n"
    "   - Do NOT click on browser chrome (tab bar, address bar, toolbar) when you mean to click a page element. Page content is below the toolbar/bookmarks bar.\n"
    "\n"
    "7. NEVER GUESS VIA KEYBOARD FOCUS (Tab / arrows):\n"
    "   - If a UI element is not visible, DO NOT press Tab, Shift+Tab, arrows, or other focus-steering keys to 'find' it. Keyboard focus routing is unpredictable across web apps and often lands on the wrong control (browser profile popups, address bar, unrelated buttons).\n"
    "   - Instead: scroll to reveal the element, then click the visible element directly.\n"
    "\n"
    "8. ERROR RECOVERY & ADAPTATION:\n"
    "   - Unexpected states (dialogs, popups, login screens, wrong window focus, loading delays, unchanged UI) are normal.\n"
    "   - When an unexpected state appears, stop following your previous plan and re-plan from the new visible state.\n"
    "   - If a click produces no visible change, the target may be wrong or off-screen. Try: (a) scroll to find the real target, (b) use a different interaction (e.g. navigate via URL instead of clicking), (c) wait for a loading state to finish.\n"
    "   - If an action fails or produces no visible change twice, choose a fundamentally different approach (do not repeat the same click at nearby coordinates).\n"
    "   - The persistent macOS top menu bar (y=0..25) is permanent system UI, not an open popup menu. Do not spam Escape.\n"
    "   - Browser tabs open at the top of the window. If you see unexpected tabs labeled 'Logout' or similar, your previous click may have opened a new tab instead of navigating — close extra tabs with Cmd+W and return to the original tab.\n"
    "\n"
    "9. ACTION MINIMIZATION & EFFICIENCY:\n"
    "   - Prefer the smallest reliable action that advances the goal.\n"
    "   - Do not emit mouse_move before mouse_click unless hover behavior, tooltip inspection, or drag preparation is explicitly needed.\n"
    "   - Prefer clipboard_paste for long text, queries, prompts, and URLs.\n"
    "\n"
    "10. SUCCESS VERIFICATION:\n"
    "   - Emit 'finish' ONLY when the requested goal has been visibly verified on the screenshot.\n"
    "   - Do not consider an action itself proof of success. The final screen must show visible evidence that the task is complete.\n"
    "   - Once success is verified, immediately emit finish with status 'success'.\n"
    "\n"
    "11. IDE INPUT HANDLING:\n"
    "   - Locate the current IDE composer from the screenshot or AX elements; never rely on a fixed coordinate.\n"
    "   - Focus the composer, paste the complete prompt, submit with Return, then verify the prompt appears in the conversation before finishing."
)


@dataclass(frozen=True)
class InvalidDecisionError(ValueError):
    """A model reply failed the JSON/validation gate (Law 2.1).

    ``hint`` is the LLM-facing corrective guidance the scaffold appends to the
    next prompt; ``cause`` is the underlying parse/validation error.
    """

    cause: str
    hint: str


def state_context(state: WorkingState, *, max_steps: int = 100) -> str:
    """Render the immutable working state as model-facing context (pure)."""
    lines = [f"Goal: {state.goal}"]
    if state.plan is not None:
        # Phase 3: the strategic roadmap the loop is executing against. The
        # provider sees the full plan (completed/in-progress/pending markers)
        # so it can steer toward the CURRENT sub-goal instead of re-deriving
        # the whole workflow at every step.
        from computeruse.orchestrator.planner import plan_summary_for_prompt

        lines.append(plan_summary_for_prompt(state.plan))
    if state.active_window:
        # Law 2 OBSERVE: what the host currently shows, so the model grounds
        # its next coordinate on the real active window (ADR-2).
        lines.append(f"Active window: {state.active_window}")
    if state.ui_elements:
        # ADR-2 AX grounding: real element coordinates from the host's
        # accessibility tree. These are EXACT and reliable for native UI
        # (toolbar buttons, menu items, address bars). For web content
        # (links, search results, article text), the AX tree is often empty
        # or truncated — in that case, ignore these and ground on the
        # screenshot directly.
        lines.append("AX UI elements (exact coordinates from accessibility tree):")
        lines.extend(f"- {el}" for el in state.ui_elements)
    if state.screenshot_b64:
        lines.append(
            "PRIMARY PERCEPTION (VISION-FIRST): A live screenshot is attached at LOGICAL "
            "resolution — 1 image pixel == 1 screen point, no Retina scaling to apply. "
            "Examine it directly to locate buttons, text inputs, search results, and chat "
            "boxes, and report every click coordinate EXACTLY as it appears in the image "
            "(no division or multiplication). What you see is what gets clicked."
        )
    if state.skill is not None:
        # Law 3 Stage 2: the mounted skill's full instructions, so the model
        # can follow a known workflow instead of re-deriving it from scratch.
        lines.append(f"Mounted skill: {state.skill.skill_id} — {state.skill.description}")
        if state.skill.steps:
            lines.append("Skill steps:")
            lines.extend(f"{index}. {step}" for index, step in enumerate(state.skill.steps, 1))
    if state.completed_steps:
        total = len(state.completed_steps)
        # Show only the last 10 steps to keep the context budget lean; older
        # steps are summarized as a count so the model still knows its position.
        recent = state.completed_steps[-10:]
        if total > 10:
            lines.append(f"Steps executed so far: {total} total (showing last 10):")
        else:
            lines.append(f"Steps executed so far ({total}):")
        for idx, step in enumerate(recent, start=max(1, total - 9)):
            lines.append(f"  {idx}. {step}")
    # Step budget awareness: the model must know where it stands. The real
    # max_steps is passed by the caller so the prompt never lies about the
    # budget (a hardcoded default would desync from --max-steps).
    remaining = max(0, max_steps - state.step_index)
    lines.append(f"Step {state.step_index} of {max_steps} — {remaining} steps remaining")
    if state.last_error:
        # Law 2 error injection: the previous failure is the single most
        # important signal for steering the next decision.
        lines.append(f"Last error to recover from: {state.last_error}")
    if state.knowledge:
        lines.append("Known app facts:")
        lines.extend(f"- {fact}" for fact in state.knowledge)
    return "\n".join(lines)


def decision_prompt(
    state: WorkingState, *, app: str, correction: str | None = None, max_steps: int = 100
) -> str:
    """The full prompt for one decision (pure).

    ``correction`` is the previous ``InvalidDecisionError.hint`` on a retry —
    the model sees exactly what it got wrong and is asked to reply again.
    """
    parts = [
        f"Application: {app}",
        ACTION_CONTRACT,
        "",
        state_context(state, max_steps=max_steps),
        "",
        'Reply now with exactly one JSON decision object. JSON object ONLY — no markdown, no prose, no extra braces outside the object.',
    ]
    if correction is not None:
        parts.append(f"\nYour previous reply was rejected: {correction}\nReply again.")
    return "\n".join(parts)


def _normalize_action_payload(payload: dict[str, object]) -> dict[str, object]:
    """Normalize common model alias variations into standard Action contract."""
    action_raw = payload.get("action")
    if not isinstance(action_raw, dict):
        return payload
    raw_dict = cast(dict[object, object], action_raw)
    action: dict[str, object] = {str(k): v for k, v in raw_dict.items()}
    action_type = action.get("type")
    if not isinstance(action_type, str):
        return payload

    # Common aliases from LLM models
    if action_type == "click":
        action["type"] = "mouse_click"
    elif action_type == "double_click":
        action["type"] = "mouse_click"
        action["click_count"] = 2
    elif action_type == "right_click":
        action["type"] = "mouse_click"
        action["button"] = "right"
    elif action_type in ("move", "hover"):
        action["type"] = "mouse_move"
    elif action_type in ("type", "write"):
        action["type"] = "type_text"
    elif action_type == "paste":
        action["type"] = "clipboard_paste"
    elif action_type in ("hotkey", "key", "press_key", "shortcut"):
        action["type"] = "press_hotkey"
    elif action_type in ("open_app", "launch_app", "switch_app"):
        action["type"] = "activate_app"

    # Default missing modifiers and normalize hotkeys
    if action["type"] == "press_hotkey":
        raw_mods = action.get("modifiers")
        normalized_mods: list[str] = []
        if isinstance(raw_mods, str):
            raw_mods = [m.strip() for m in raw_mods.split("+") if m.strip()]
        if isinstance(raw_mods, list):
            for m in cast(list[object], raw_mods):
                m_str = str(m).lower().strip()
                if m_str in ("cmd", "command"):
                    normalized_mods.append("command")
                elif m_str in ("ctrl", "control"):
                    normalized_mods.append("control")
                elif m_str in ("opt", "alt", "option"):
                    normalized_mods.append("alt")
                elif m_str == "shift":
                    normalized_mods.append("shift")
                elif m_str:
                    normalized_mods.append(m_str)
        action["modifiers"] = normalized_mods

        raw_key = action.get("key")
        if isinstance(raw_key, str):
            parts = [part.strip().lower() for part in raw_key.split("+") if part.strip()]
            if len(parts) > 1:
                modifier_aliases = {
                    "cmd": "command", "command": "command", "shift": "shift",
                    "alt": "alt", "opt": "alt", "option": "alt",
                    "ctrl": "control", "control": "control",
                }
                primary = parts[-1]
                for modifier in parts[:-1]:
                    canonical = modifier_aliases.get(modifier)
                    if canonical is not None and canonical not in normalized_mods:
                        normalized_mods.append(canonical)
                action["modifiers"] = normalized_mods
                action["key"] = primary
            else:
                k_lower = raw_key.lower().strip()
                if k_lower in ("enter", "return"):
                    action["key"] = "return"
                elif k_lower in ("esc", "escape"):
                    action["key"] = "escape"
                elif k_lower in ("space", "spacebar", " "):
                    action["key"] = "space"
                else:
                    action["key"] = k_lower

    # Coordinate / integer casts
    for int_key in ("x", "y", "start_x", "start_y", "end_x", "end_y", "duration_ms", "click_count", "wpm", "dx", "dy"):
        if int_key in action and action[int_key] is not None:
            try:
                action[int_key] = int(float(str(action[int_key])))
            except (ValueError, TypeError):
                pass

    # Text string casts
    if "text" in action and action["text"] is not None:
        action["text"] = str(action["text"])

    return {**payload, "action": action}


def _first_json_object(text: str) -> str | None:
    """First balanced ``{...}`` span in ``text`` that parses as JSON (pure).

    Braces belonging to prose (``"Plan {step 1}:"``) or to JSON string
    values (``"thought": "hit {" ``) must not truncate the extraction:
    candidate spans are bracket-matched *string-aware*, and only a span
    that ``json.loads`` accepts is returned. When a candidate span fails
    to parse, the scan resumes at the next ``{`` — so prose braces before
    the real payload are skipped rather than trusted.
    """
    for start, opening in enumerate(text):
        if opening != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    span = text[start : index + 1]
                    try:
                        json.loads(span)
                    except json.JSONDecodeError:
                        break
                    return span
    return None


def parse_decision(raw: str) -> AgentTurn:
    """Parse a model reply into a validated decision (pure, with gate).

    Tolerates markdown fences, prose around the JSON, and common model
    action aliases; anything else is rejected with a corrective hint
    instead of silently guessing (Law 6.3).
    """
    stripped = raw.strip()

    # Prefer a Markdown fenced JSON block when present (e.g.
    # ```json\n{"thought":...}\n```). Use a single matching pair so
    # prose that mentions braces earlier (e.g. "Plan {step 1}:") is
    # not mistaken for the payload.
    block = re.search(
        r"""```(?:json)?\s*(\{.*?\})\s*```""",
        stripped,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if block is not None:
        candidate = block.group(1)
    else:
        # Fallback: take the first *parseable* balanced ``{...}`` span.
        # A naive first-``{``-to-last-``}`` slice is wrong twice over: an
        # earlier prose brace (``"Plan {step 1}:"``) truncates the span,
        # and a naive ``rfind("}")`` over-extends past stray closing
        # braces in explanatory text. ``_first_json_object`` handles both.
        candidate = _first_json_object(stripped)
        if candidate is None:
            raise InvalidDecisionError(
                cause="no JSON object found in the reply",
                hint="your reply must contain exactly one JSON object matching the action contract above",
            )

    if not candidate or not candidate.startswith("{"):
        raise InvalidDecisionError(
            cause="no JSON object found in the reply",
            hint="your reply must contain exactly one JSON object matching the action contract above",
        )
    try:
        raw_payload: object = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise InvalidDecisionError(
            cause=f"invalid JSON: {exc}",
            hint="the JSON object was malformed (check quotes, commas, braces); "
            "reply with one well-formed JSON object only",
        ) from exc
    if not isinstance(raw_payload, dict):
        raise InvalidDecisionError(
            cause="root JSON element is not an object",
            hint="the decision must be a JSON object with 'thought', 'sub_goal', and 'action'",
        )
    raw_dict = cast(dict[object, object], raw_payload)
    typed_payload: dict[str, object] = {str(k): v for k, v in raw_dict.items()}
    normalized = _normalize_action_payload(typed_payload)
    try:
        return AgentTurn.model_validate(normalized)
    except ValueError as exc:
        raise InvalidDecisionError(
            cause=f"schema validation failed: {exc}",
            hint="the decision did not match the action contract (wrong action "
            "type, missing field, or invalid parameter); use exactly the "
            "shapes listed above",
        ) from exc


def _call_model(model: Callable[..., str], prompt: str, image_b64: str | None) -> str:
    """Invoke a model callable, passing image_b64 if supported (pure wrapper)."""
    try:
        return model(prompt, image_b64)
    except TypeError:
        return model(prompt)


def scaffolded_provider(
    model: Callable[..., str],
    *,
    app: str,
    max_retries: int = 2,
    max_steps: int = 100,
) -> Callable[[WorkingState], AgentTurn]:
    """Wrap a model transport so the OODA loop sees a well-behaved provider.

    Each turn builds the prompt from the current state, asks the model
    (including multimodal visual perception when a screenshot is available),
    and validates the reply. A failed parse appends the corrective hint and
    asks again (bounded by ``max_retries``); if the model never complies,
    the last :class:`InvalidDecisionError` propagates so the runner folds
    it into ``last_error`` — the loop survives a hallucinating model (Law 2).
    """

    def provider(state: WorkingState) -> AgentTurn:
        prompt = decision_prompt(state, app=app, max_steps=max_steps)
        last_error: InvalidDecisionError | None = None
        # One initial ask plus at most ``max_retries`` corrective re-asks.
        for _ in range(max_retries + 1):
            reply = _call_model(model, prompt, state.screenshot_b64)
            try:
                return parse_decision(reply)
            except InvalidDecisionError as exc:
                last_error = exc
                prompt = decision_prompt(state, app=app, correction=exc.hint, max_steps=max_steps)
        # Exhausted retries: surface the last failure so the runner folds it
        # into ``last_error`` and the loop survives a hallucinating model.
        assert last_error is not None
        raise last_error

    return provider
