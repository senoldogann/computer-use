"""Shared fakes for the CUA REPL tests.

The engine's actuating calls insist on *confirmed* frontmost focus before a
keystroke is posted, because a keystroke goes to whichever application holds
focus at that moment rather than to the one the script named. A fake that
accepts ``activate_app`` and then reports an unrelated frontmost window cannot
express that contract — which is exactly why the defect it guards against was
invisible to this suite and only showed up against the live backend.

:func:`model_focus` gives a mock the one behaviour that matters here:
activating an application actually moves the front window to it. Where the
engine needs a real driver-shaped object (``send``, ``activate_app``,
``focused_window``, …) the tests share :class:`MockDriverClient` instead of
copying the same recording fake into every file that exercises the engine.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from computeruse.orchestrator.schemas import Action
from computeruse.vision.capture import ScreenCapture


class MockDriverClient:
    """Mock client capturing driver calls for verification.

    ``list_apps`` answers the full fixture set (``test_cua_repl`` asserts on
    the count); tests that do not care read whatever they need.
    """

    def __init__(self) -> None:
        self.sent_actions: list[Action] = []
        self.activated_apps: list[str] = []
        # Which application owns the front window. Modelled rather than
        # assumed: the engine confirms focus before it posts a keystroke, and
        # a fake that ignores activation cannot express that contract.
        self.frontmost: str = "TextEdit"

    def send(self, action: Action) -> None:
        self.sent_actions.append(action)

    def release_inputs(self) -> None:
        """No hardware is held by this recording driver."""

    def activate_app(self, app_name: str) -> None:
        self.activated_apps.append(app_name)
        self.frontmost = app_name

    def focused_window(self) -> dict[str, Any]:
        return {"app_name": self.frontmost, "app": self.frontmost, "bundle_id": ""}

    def list_apps(self) -> list[str]:
        return ["TextEdit", "Safari", "Finder"]

    def capture(self) -> ScreenCapture:
        return fake_capture()


def model_focus(mock_client: MagicMock) -> None:
    """Make ``activate_app`` move the mock's frontmost window (in place).

    Mutates the mapping already registered as ``focused_window``'s return
    value, so a test keeps whatever pid, title and geometry it set up and gains
    only the focus transition.
    """

    def _activate(app_name: str) -> None:
        window: dict[str, Any] = mock_client.focused_window.return_value
        window["app_name"] = app_name
        window["app"] = app_name

    mock_client.activate_app.side_effect = _activate


def fake_capture(width: int = 8, height: int = 4) -> ScreenCapture:
    """A small but *real* BGRA frame.

    The fakes used to answer ``capture()`` with an object carrying only
    ``.data``, which was enough while the engine base64'd those bytes straight
    into a ``data:image/png;base64,`` URI — a URI that never held a PNG. Now
    that the bytes are actually encoded, a capture has to be a capture:
    correct geometry, correct scale, and ``width * height * 4`` bytes.
    """
    return ScreenCapture(
        display_id=0,
        width=width,
        height=height,
        scale=2.0,
        origin_x=0.0,
        origin_y=0.0,
        data=bytes([32, 64, 96, 255]) * (width * height),
    )
