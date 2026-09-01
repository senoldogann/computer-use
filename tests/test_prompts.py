from __future__ import annotations

import pytest

from computeruse.orchestrator.prompts import _normalize_action_payload, parse_decision
from computeruse.orchestrator.schemas import AgentTurn


def _action(payload: dict[str, object]) -> dict[str, object]:
    return payload["action"]


def test_parse_decision_accepts_plain_json() -> None:
    raw = (
        '{"thought": "open github", "sub_goal": "open github", '
        '"action": {"type": "activate_app", "app": "Google Chrome"}}'
    )
    turn = parse_decision(raw)
    assert isinstance(turn, AgentTurn)
    assert turn.action.type == "activate_app"
    assert turn.action.app == "Google Chrome"


def test_parse_decision_accepts_markdown_fenced_json() -> None:
    raw = "```json\n" + (
        '{"thought": "x", "sub_goal": "y", '
        '"action": {"type": "mouse_click", "x": 100, "y": 200}}\n'
    ) + "```"
    turn = parse_decision(raw)
    assert turn.action.type == "mouse_click"
    assert turn.action.x == 100
    assert turn.action.y == 200


def test_parse_decision_skips_prose_with_earlier_braces() -> None:
    # A model that writes "Plan {step 1}:" before the JSON block must not
    # cause the fallback extractor to grab the wrong braces.
    raw = (
        "Plan {step 1}: navigate to GitHub.\n\n"
        '{"thought": "visible chrome", "sub_goal": "open chrome", '
        '"action": {"type": "activate_app", "app": "Google Chrome"}}'
    )
    turn = parse_decision(raw)
    assert turn.action.type == "activate_app"


def test_parse_decision_rejects_empty_reply() -> None:
    from computeruse.orchestrator.prompts import InvalidDecisionError

    with pytest.raises(InvalidDecisionError):
        parse_decision("")


def test_parse_decision_rejects_non_object_json() -> None:
    from computeruse.orchestrator.prompts import InvalidDecisionError

    with pytest.raises(InvalidDecisionError):
        parse_decision('["not", "an", "object"]')


def _normalized_payload(payload: dict[str, object]) -> dict[str, object]:
    return _normalize_action_payload(payload)


def test_normalize_action_alias_click_to_mouse_click() -> None:
    payload = {
        "thought": "x",
        "sub_goal": "y",
        "action": {"type": "click", "x": 10, "y": 20},
    }
    normalized = _normalized_payload(payload)
    action = _action(normalized)
    assert action["type"] == "mouse_click"
    assert action["x"] == 10
    assert action["y"] == 20


def test_normalize_action_tab_alias_is_kept_as_tab() -> None:
    payload = {
        "thought": "x",
        "sub_goal": "y",
        "action": {"type": "press_hotkey", "modifiers": [], "key": "Tab"},
    }
    normalized = _normalized_payload(payload)
    action = _action(normalized)
    assert action["type"] == "press_hotkey"
    assert action["key"] == "tab"


def test_normalize_action_composite_hotkey_splits_modifiers() -> None:
    payload = {
        "thought": "x",
        "sub_goal": "y",
        "action": {"type": "press_hotkey", "modifiers": [], "key": "Cmd+Shift+P"},
    }
    normalized = _normalized_payload(payload)
    action = _action(normalized)
    assert action["type"] == "press_hotkey"
    assert action["key"] == "p"
    assert "command" in action["modifiers"]
    assert "shift" in action["modifiers"]


def test_normalize_action_paste_alias_is_clipboard_paste() -> None:
    payload = {"thought": "x", "sub_goal": "y", "action": {"type": "paste", "text": "hello"}}
    normalized = _normalized_payload(payload)
    action = _action(normalized)
    assert action["type"] == "clipboard_paste"
    assert action["text"] == "hello"
