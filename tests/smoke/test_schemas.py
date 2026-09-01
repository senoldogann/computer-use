"""Pure-parsing tests for the action contract (no OS I/O — Law 6).

These validate that the discriminated union accepts valid frames and rejects
malformed ones at parse time, which is the scaffolding that rescues weak models
(Law 2).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from computeruse.orchestrator.schemas import AgentTurn, Finish, MouseClick, MouseMove


def test_valid_mouse_move_frame() -> None:
    frame = AgentTurn.model_validate_json(
        """
        {
            "thought": "move to the button",
            "sub_goal": "locate button",
            "action": {"type": "mouse_move", "x": 300, "y": 400}
        }
        """
    )
    assert isinstance(frame.action, MouseMove)
    assert frame.action.x == 300
    assert frame.action.duration_ms == 180


def test_valid_mouse_click_discriminates() -> None:
    frame = AgentTurn.model_validate_json(
        """
        {
            "thought": "",
            "sub_goal": "",
            "action": {"type": "mouse_click", "x": 10, "y": 10}
        }
        """
    )
    assert isinstance(frame.action, MouseClick)


def test_finish_status_must_be_in_enum() -> None:
    with pytest.raises(ValidationError):
        AgentTurn.model_validate_json(
            """
            {
                "thought": "",
                "sub_goal": "",
                "action": {"type": "finish", "status": "maybe", "summary": ""}
            }
            """
        )


def test_unknown_action_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentTurn.model_validate_json(
            """
            {
                "thought": "",
                "sub_goal": "",
                "action": {"type": "nuke_everything", "x": 1}
            }
            """
        )


def test_valid_activate_app() -> None:
    frame = AgentTurn.model_validate_json(
        """
        {
            "thought": "open chrome",
            "sub_goal": "switch app",
            "action": {"type": "activate_app", "app": "Google Chrome"}
        }
        """
    )
    from computeruse.orchestrator.schemas import ActivateApp

    assert isinstance(frame.action, ActivateApp)
    assert frame.action.app == "Google Chrome"


def test_valid_clipboard_paste() -> None:
    frame = AgentTurn.model_validate_json(
        """
        {
            "thought": "paste URL",
            "sub_goal": "paste",
            "action": {"type": "clipboard_paste", "text": "https://github.com"}
        }
        """
    )
    from computeruse.orchestrator.schemas import ClipboardPaste

    assert isinstance(frame.action, ClipboardPaste)
    assert frame.action.text == "https://github.com"


def test_negative_coordinate_rejected() -> None:
    frame = AgentTurn.model_validate_json(
        """
        {
            "thought": "",
            "sub_goal": "",
            "action": {"type": "finish", "status": "success", "summary": ""}
        }
        """
    )
    assert isinstance(frame.action, Finish)
    with pytest.raises(ValidationError):
        MouseMove.model_validate_json('{"type":"mouse_move","x":-1,"y":0}')