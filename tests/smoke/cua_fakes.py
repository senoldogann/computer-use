"""Shared focus modelling for the CUA REPL fakes.

The engine's actuating calls insist on *confirmed* frontmost focus before a
keystroke is posted, because a keystroke goes to whichever application holds
focus at that moment rather than to the one the script named. A fake that
accepts ``activate_app`` and then reports an unrelated frontmost window cannot
express that contract — which is exactly why the defect it guards against was
invisible to this suite and only showed up against the live backend.

:func:`model_focus` gives a mock the one behaviour that matters here:
activating an application actually moves the front window to it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from computeruse.vision.capture import ScreenCapture


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
