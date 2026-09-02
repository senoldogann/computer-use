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

import html
import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, cast

from computeruse.orchestrator.evidence import CompletionVerdict
from computeruse.orchestrator.loop import WorkingState
from computeruse.orchestrator.schemas import Action, AgentTurn, ClipboardPaste, TypeText

# The action contract, spelled out for a model that has never seen it. The
# exact JSON shape mirrors what `AgentTurn` validates at parse time, so the
# instruction and the gate can never disagree about the *shape*.
ACTION_CONTRACT: Final[str] = (
    "You are operating a real macOS computer as an autonomous agent. Every action you emit "
    "moves a physical cursor and presses physical keys on a machine someone is using.\n"
    "\n"
    "1. OUTPUT CONTRACT:\n"
    "Return exactly ONE valid JSON object. Output NOTHING outside the JSON object (no markdown formatting, no explanations, no text wrappers).\n"
    "The JSON object must match EITHER of these two forms:\n"
    "  Single action (default):\n"
    '  {\n'
    '    "thought": "<what you SEE on the current screenshot, and why it justifies this action>",\n'
    '    "sub_goal": "<the single immediate objective this action advances>",\n'
    '    "action": {"type": "<action_type>", ...}\n'
    '  }\n'
    "  Action batch (up to 3 actions executed back-to-back in this ONE turn):\n"
    '  {\n'
    '    "thought": "<observation>",\n'
    '    "sub_goal": "<objective advanced by the whole batch>",\n'
    '    "action": {"type": "<type of the FIRST action>", ...},\n'
    '    "actions": [{"type": "<action_type>", ...}, {"type": "<action_type>", ...}]\n'
    '  }\n'
    '  (In the batch form, "action" repeats the FIRST element of "actions".)\n'
    "  BATCHING RULES: batch only actions that are safe to execute SEQUENTIALLY from the CURRENT screen without re-observation — e.g. Cmd+L, then paste a URL, then Return; or two clicks on already-visible elements. NEVER batch an action whose target depends on a screen change caused by an earlier action in the same batch (e.g. do not click a search result in the same batch as the Return that submits the search). If any doubt, emit a single action. `finish` must be the LAST action of a batch.\n"
    "\n"
    "2. SUPPORTED ACTIONS:\n"
    '- mouse_click: {"type": "mouse_click", "x": int, "y": int, "button": "left|right|middle", "click_count": 1|2}\n'
    '- mouse_move: {"type": "mouse_move", "x": int, "y": int, "duration_ms": int (default 180)} — ONLY when hover, tooltip, or drag preparation is explicitly needed\n'
    '- mouse_drag: {"type": "mouse_drag", "start_x": int, "start_y": int, "end_x": int, "end_y": int, "duration_ms": int (default 200)}\n'
    '- mouse_scroll: {"type": "mouse_scroll", "dx": int, "dy": int} — scrolls at the CURRENT cursor position; move the cursor over the target scrollable area first\n'
    '- type_text: {"type": "type_text", "text": str, "wpm": int (default 40)}\n'
    '- clipboard_paste: {"type": "clipboard_paste", "text": str} — preferred for URLs, search queries, and any long text (Cmd+V)\n'
    '- press_hotkey: {"type": "press_hotkey", "modifiers": ["command|shift|alt|control"], "key": str} — key: "return", "enter", "tab", "escape", "space", "backspace", "l", "t", "w", "a", "c", "v", etc.\n'
    '- activate_app: {"type": "activate_app", "app": str} — brings an application (e.g. "Google Chrome", "Notes", "Finder") to the front\n'
    '- wait: {"type": "wait", "duration_ms": int, "reason": str}\n'
    '- finish: {"type": "finish", "status": "success|failed", "summary": str}\n'
    "\n"
    "3. THE CYCLE YOU ARE INSIDE:\n"
    "   OBSERVE -> UNDERSTAND -> PLAN -> VALIDATE -> ACT -> VERIFY -> RECOVER.\n"
    "   The orchestrator runs VALIDATE, VERIFY and RECOVER for you: it rejects coordinates that\n"
    "   do not exist, checks after every action whether the expected change actually happened,\n"
    "   and reports what it found in 'Last error to recover from'. Your job is the other half:\n"
    "   OBSERVE honestly, and change approach when the orchestrator tells you something failed.\n"
    "   - The screenshot is the single authoritative source of truth for the current state.\n"
    "   - Never assume an action succeeded because you emitted it.\n"
    "   - Keep observed facts, inferences, and intentions separate. Prefer what you can see.\n"
    "\n"
    "4. VISUAL GROUNDING & DISPLAY COORDINATES:\n"
    "   - Never reuse a coordinate from an earlier turn. Windows move, pages scroll, layouts adapt.\n"
    "   - Derive every (x, y) from the CURRENT screenshot.\n"
    "   - Coordinate space: the screenshot is a SCALED-DOWN MAP of the screen (max 512px on its\n"
    "     longest side). Report x,y EXACTLY as they appear in that image. The system converts them\n"
    "     to real screen points for you — never apply any scale math yourself.\n"
    "   - AX UI element coordinates are listed in the SAME image space, so both sources are directly\n"
    "     comparable. PREFER the AX list whenever it names your target: it is exact, while the\n"
    "     screenshot is downscaled ~3x, where body text is a few pixels tall and a link is easy to\n"
    "     misplace by a whole row. It covers page content (links, headings, cells) as well as native\n"
    "     chrome. Use the screenshot for what AX does not list, and for layout and reading.\n"
    "   - Each AX element is listed at its CENTER point. Click that point EXACTLY as given — do not\n"
    "     add half the width or height, and do not adjust it. The listed size is for judging what an\n"
    "     element is, not for offsetting the click.\n"
    "   - When you aim from the screenshot instead, click the CENTER of the target.\n"
    "\n"
    "5. SAFE BROWSER NAVIGATION & TEXT INPUT:\n"
    "   - To enter a URL or search query in Chrome/Safari, always in this order:\n"
    "     Step 1: press_hotkey {'modifiers': ['command'], 'key': 'l'} (focuses and selects the whole address bar)\n"
    "     Step 2: clipboard_paste {'text': '<target URL or query>'} (replaces the selection cleanly)\n"
    "     Step 3: press_hotkey {'modifiers': [], 'key': 'return'} (submits — never leave it unsubmitted)\n"
    "   - NEVER call clipboard_paste twice in a row on an address bar without pressing Return between them.\n"
    "   - Before typing into any field: click it, confirm it is focused, and clear it (Cmd+A) first.\n"
    "   - If the target is ALREADY visible on screen, do NOT touch the address bar — click it directly.\n"
    "\n"
    "6. SCROLLING & OFF-SCREEN TARGETS:\n"
    "   - Pages are taller than the viewport. If the element you need is NOT visible in the current\n"
    "     screenshot, it is off-screen: DO NOT guess its coordinate and DO NOT click blindly. Scroll.\n"
    "   - Move the cursor over the page content area first, then mouse_scroll: dy POSITIVE scrolls DOWN\n"
    "     (reveals content below), dy NEGATIVE scrolls UP (reveals content above).\n"
    "   - Re-read the new screenshot after each scroll. Repeat small scrolls until the target is visible,\n"
    "     then click the coordinate you read from that screenshot.\n"
    "   - If a scroll changes nothing, the cursor is probably not over a scrollable area (move it to the\n"
    "     page center) — or you are already at the end, in which case scroll the OTHER way.\n"
    "   - Headers, avatars and account menus live at the TOP: scroll up before hunting downward.\n"
    "   - Do NOT click browser chrome (tab bar, address bar, toolbar) when you mean a page element.\n"
    "\n"
    "7. NEVER NAVIGATE BY KEYBOARD FOCUS (Tab / arrows):\n"
    "   - Do not press Tab, Shift+Tab or arrow keys to 'find' an invisible element. Focus routing is\n"
    "     unpredictable across web apps and lands on the wrong control (profile popups, the address bar).\n"
    "   - Scroll the element into view and click it instead.\n"
    "\n"
    "8. RECOVERY — READ THIS WHENEVER 'Last error to recover from' IS PRESENT:\n"
    "   The orchestrator escalates deliberately, and the error text tells you which rung you are on:\n"
    "   - FIRST failure of a kind: a corrected retry is fine. Fix the specific thing named.\n"
    "   - SECOND in a row: do NOT retry the same action with adjusted coordinates. Change the METHOD —\n"
    "     a different UI path, a keyboard shortcut, a direct URL, or scrolling to reveal the real target.\n"
    "   - THIRD in a row: abandon the tactic completely. Re-derive the shortest remaining route to the\n"
    "     goal from the CURRENT screenshot alone, and take its first step.\n"
    "   - Unexpected states (dialogs, popups, login screens, wrong focus, loading delays) are normal.\n"
    "     Stop following your previous plan and re-plan from the new visible state.\n"
    "   - The macOS menu bar (y near the top edge) is permanent system UI, not an open popup. Do not spam Escape.\n"
    "   - If unexpected tabs appear, a click opened a background tab instead of navigating: close it with\n"
    "     Cmd+W and return to the original tab.\n"
    "\n"
    "9. ACTION MINIMIZATION:\n"
    "   - Prefer the smallest reliable action that advances the goal.\n"
    "   - Do not emit mouse_move before mouse_click unless hover, tooltip inspection, or a drag needs it.\n"
    "   - Prefer clipboard_paste for long text, queries, prompts, and URLs.\n"
    "\n"
    "10. FINISHING — THE STRICTEST RULE:\n"
    "   - Emit finish with status 'success' ONLY when the CURRENT screenshot itself shows the goal is done.\n"
    "   - Having performed the right actions is NOT evidence of success. The final screen is.\n"
    "   - Your summary must state the visible evidence ('the profile page shows the name updated to X'),\n"
    "     not the actions you took. A separate check re-reads the screen against your claim and will\n"
    "     reject a summary it cannot see evidence for.\n"
    "   - If the goal genuinely cannot be reached from here, emit finish with status 'failed' and say\n"
    "     exactly what blocked it. An honest failure is a correct answer; a false success is not.\n"
    "\n"
    "11. IDE INPUT HANDLING:\n"
    "   - Locate the IDE composer from the screenshot or AX elements; never rely on a fixed coordinate.\n"
    "   - Focus the composer, paste the complete prompt, submit with Return, then verify the prompt\n"
    "     appears in the conversation before finishing."
)


COMPLETION_AUDIT_CONTRACT: Final[str] = (
    "You are a verification checker, not an operator. You do NOT control the computer.\n"
    "An autonomous agent has just claimed it finished a task. You are shown the goal, the agent's "
    "own claim, and the CURRENT state of the machine: a screenshot and, when available, the "
    "accessibility (AX) state of what is on screen.\n"
    "\n"
    "Decide one thing: does the current state show that the goal is complete?\n"
    "\n"
    "Rules:\n"
    "- Judge the OBSERVED STATE, not the agent's story. Whether its reasoning sounds plausible is\n"
    "  irrelevant; whether the machine is in the goal state is everything.\n"
    "- The screenshot AND the AX element list are both observed state, and they are complementary.\n"
    "  The AX list is authoritative for things a picture reports poorly — the exact text in a field,\n"
    "  which control has focus, the title of a window. The screenshot is authoritative for layout and\n"
    "  for anything the AX list does not cover. Either one showing the goal state is sufficient.\n"
    "- Do NOT demand visual evidence for a fact the AX state already establishes, and do not treat a\n"
    "  screenshot you find hard to read as proof of incompleteness — say the state is unverifiable\n"
    "  only if NEITHER source supports the claim.\n"
    "- The agent having performed reasonable actions is NOT evidence. The resulting state is.\n"
    "- If the state shows work in progress, a loading indicator, an error, or an unrelated view, the\n"
    "  goal is NOT satisfied.\n"
    "- A goal with SEVERAL parts needs evidence for EACH of them, not just the last one. When a\n"
    "  task reads a value in one place and uses it in another, the number now on screen proves the\n"
    "  arithmetic, NOT that the value was read correctly. If nothing supports a part of the goal,\n"
    "  say which part and answer false — the agent can go back and re-read it. Do not let a\n"
    "  correct-looking final result stand in for an unchecked input.\n"
    "- Earlier observed text (when provided) is evidence too, and it is how a multi-application\n"
    "  goal is proved at all: a calculator covers the page whose number it used, so the two facts\n"
    "  can never be on screen together. That list is read from the machine, not written by the\n"
    "  agent, so trust it exactly as much as the current screen. The FINAL state must still be\n"
    "  visible now — use the earlier text only for the parts that have moved out of view.\n"
    "- Accept an unverifiable claim ONLY when the goal has no checkable end state at all (it asked\n"
    "  the agent to read something out to the user, say). A goal whose evidence simply is not on\n"
    "  screen right now is NOT in that category: it is unverified, so answer false.\n"
    "- Be strict but not pedantic: cosmetic differences from the goal's wording do not make a\n"
    "  completed task incomplete.\n"
    "\n"
    "Reply with exactly one JSON object and nothing else:\n"
    '{"satisfied": true|false, "evidence": "<the specific observed detail behind your verdict>"}'
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
        # accessibility tree, each at its centre point. Exact for native UI
        # (toolbars, menus, address bars) AND for web page content — browsers
        # expose links, headings and cells the same way, and reading a link's
        # position here beats inferring it from a screenshot where its text is
        # three pixels tall.
        lines.append(
            "AX UI elements (exact CENTER coordinates from the accessibility tree "
            "— click these points as given, do not offset them):"
        )
        lines.extend(f"- {el}" for el in state.ui_elements)
    if state.open_tabs:
        # Browser tab awareness: the agent must know which tabs are open
        # to detect stray tabs (e.g. accidental Cmd+click opens) and to
        # decide whether to close or switch tabs.
        tab_count = len(state.open_tabs)
        lines.append(f"Open browser tabs ({tab_count}):")
        for idx, tab_title in enumerate(state.open_tabs, 1):
            lines.append(f"  {idx}. {tab_title}")
    if state.screenshot_b64:
        lines.append(
            "PRIMARY PERCEPTION (VISION-FIRST): A live screenshot is attached as a scaled-down "
            "MAP of the screen (max 512px). Report every click coordinate EXACTLY as it appears "
            "in the image — the system converts image pixels to real screen points "
            "automatically, so never apply scale math yourself. AX element coordinates, when "
            "listed above, are in this same image space. What you point at is what gets clicked."
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


def _unescape_text_action(action: Action) -> Action:
    """Unescape HTML entities in the text of paste/type actions (pure)."""
    if isinstance(action, (ClipboardPaste, TypeText)):
        return action.model_copy(update={"text": html.unescape(action.text)})
    return action


def _normalize_action_dict(action: dict[str, object]) -> dict[str, object]:
    """Normalize one action dict's common model alias variations (pure)."""
    action_type = action.get("type")
    if not isinstance(action_type, str):
        return action

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

    return action


def _normalize_action_payload(payload: dict[str, object]) -> dict[str, object]:
    """Normalize model alias variations in the single action AND any batch.

    Applies :func:`_normalize_action_dict` to ``action`` and to every item of
    ``actions`` on the same path, so a batch item using an alias (e.g.
    ``{"type": "click"}``) is corrected exactly like the single-action form.
    Malformed sub-payloads (non-dict actions) pass through untouched so the
    Pydantic gate rejects them with its own corrective hint (Law 2).
    """
    result = dict(payload)
    action_raw = payload.get("action")
    if isinstance(action_raw, dict):
        raw_dict = cast(dict[object, object], action_raw)
        result["action"] = _normalize_action_dict(
            {str(k): v for k, v in raw_dict.items()}
        )
    actions_raw = payload.get("actions")
    if isinstance(actions_raw, list):
        normalized_items: list[dict[str, object]] = []
        for item in cast(list[object], actions_raw):
            if not isinstance(item, dict):
                return payload
            raw_item = cast(dict[object, object], item)
            normalized_items.append(
                _normalize_action_dict({str(k): v for k, v in raw_item.items()})
            )
        result["actions"] = normalized_items
    return result


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
        turn = AgentTurn.model_validate(normalized)
    except ValueError as exc:
        raise InvalidDecisionError(
            cause=f"schema validation failed: {exc}",
            hint="the decision did not match the action contract (wrong action "
            "type, missing field, or invalid parameter); use exactly the "
            "shapes listed above",
        ) from exc
    # Weak-model text hygiene: models frequently HTML-escape user-facing text
    # (observed in the field: a YouTube URL arrived as `watch?v=...&amp;t=1860s`
    # and was pasted literally, breaking the `t=` seek). Unescape pasted and
    # typed text deterministically so the driver always receives the plain
    # string the goal meant.
    if turn.actions is not None:
        turn = turn.model_copy(
            update={"actions": [_unescape_text_action(item) for item in turn.actions]}
        )
    else:
        turn = turn.model_copy(update={"action": _unescape_text_action(turn.action)})
    return turn


def _supports_image_argument(model: Callable[..., str]) -> bool:
    """Whether ``model`` accepts ``image_b64`` as a second positional arg.

    Inspected from the signature once per call — never inferred from a thrown
    ``TypeError``, which would also swallow a genuine bug raised from *inside*
    the model implementation and re-invoke it under a misleading assumption
    (L10). A ``Callable[..., str]`` from a C extension without an
    introspectable signature conservatively reads as prompt-only.
    """
    try:
        params = list(inspect.signature(model).parameters.values())
    except (TypeError, ValueError):
        return False
    if not params:
        return False
    # A variadic second position (``def f(prompt, *rest)``) accepts the image.
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
        return True
    if len(params) < 2:
        return False
    second = params[1]
    return second.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_ONLY,
    )


def _call_model(model: Callable[..., str], prompt: str, image_b64: str | None) -> str:
    """Invoke a model callable, passing image_b64 if supported (pure wrapper)."""
    if image_b64 is None or not _supports_image_argument(model):
        return model(prompt)
    return model(prompt, image_b64)


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


def completion_prompt(state: WorkingState, claim: str, *, app: str) -> str:
    """The verification checker's prompt for one completion claim (pure).

    Deliberately narrow. The auditor sees the goal, the claim, and the current
    perception — but *not* the acting model's reasoning, its plan, or its step
    history. Sharing those would let the story that produced a wrong claim also
    justify it: the whole value of a second read is that it is uncontaminated
    by the first one's beliefs.
    """
    lines = [
        COMPLETION_AUDIT_CONTRACT,
        "",
        f"Application: {app}",
        f"Goal: {state.goal}",
        f"Agent's completion claim: {claim}",
    ]
    if state.active_window:
        lines.append(f"Active window: {state.active_window}")
    if state.ui_elements:
        lines.append("AX UI elements currently on screen:")
        lines.extend(f"- {element}" for element in state.ui_elements)
    if state.observed_trail:
        lines.append(
            "Text observed on screen EARLIER in this run (read from the machine, "
            "not the agent's account of it) — use it for parts of the goal whose "
            "evidence is no longer on screen:"
        )
        lines.extend(f"- {entry}" for entry in state.observed_trail)
    if state.screenshot_b64:
        lines.append("A screenshot of the current screen is attached — judge from it.")
    lines.append("")
    lines.append("Reply now with exactly one JSON object.")
    return "\n".join(lines)


def parse_completion(raw: str) -> CompletionVerdict:
    """Parse an auditor reply into a verdict (pure, with gate).

    A malformed reply raises :class:`InvalidDecisionError`. The caller treats
    that as "the auditor could not answer" and accepts the finish rather than
    blocking it: a broken checker must never trap a run that genuinely
    completed. Refusing to *guess* a verdict here is what keeps that policy
    decision in one place instead of buried in a parser default.
    """
    candidate = _first_json_object(raw.strip())
    if candidate is None:
        raise InvalidDecisionError(
            cause="no JSON object found in the completion reply",
            hint='reply with {"satisfied": true|false, "evidence": "..."}',
        )
    try:
        payload: object = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise InvalidDecisionError(
            cause=f"invalid JSON: {exc}",
            hint='reply with {"satisfied": true|false, "evidence": "..."}',
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidDecisionError(
            cause="root JSON element is not an object",
            hint='reply with {"satisfied": true|false, "evidence": "..."}',
        )
    typed = cast(dict[str, object], payload)
    satisfied = typed.get("satisfied")
    if not isinstance(satisfied, bool):
        raise InvalidDecisionError(
            cause=f"'satisfied' must be a boolean, got {type(satisfied).__name__}",
            hint='"satisfied" must be exactly true or false',
        )
    evidence = typed.get("evidence")
    return CompletionVerdict(
        satisfied=satisfied,
        evidence=evidence if isinstance(evidence, str) and evidence else "(no evidence given)",
    )


def completion_auditor(
    model: Callable[..., str],
    *,
    app: str,
) -> Callable[[WorkingState, str], CompletionVerdict]:
    """Build the goal-completion checker the loop calls before accepting success.

    A model is the least reliable witness to its own success: the same context
    that convinced it to act convinces it the acting worked. Re-asking the
    question in a fresh, minimal context — goal, claim, current screen, nothing
    else — turns "I did the steps" back into "the screen shows the result",
    which is the only claim a user actually cares about.
    """

    def audit(state: WorkingState, claim: str) -> CompletionVerdict:
        prompt = completion_prompt(state, claim, app=app)
        return parse_completion(_call_model(model, prompt, state.screenshot_b64))

    return audit
