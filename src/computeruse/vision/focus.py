"""Focused-window perception — the OBSERVE step's non-pixel half.

§5's OBSERVE step reads two signals besides pixels: the *active window title*
and the *cursor position*. Both come from the driver's ``focused_window`` RPC
(system-wide AX + a probe CGEvent on the real backend; a deterministic Safari
fixture on the simulated one), so the orchestrator never touches the host
directly (ADR-1).

This module is the typed, pure Python half of that boundary:

* :class:`FocusedWindow` validates the driver payload — the frontmost app's
  pid and name, its focused window's title, and the cursor.
* :func:`window_summary` renders it into the compact string that lands in the
  provider's working context (Law 4.3: minimal context, one line per signal).
"""

from __future__ import annotations

from pydantic import BaseModel


class FocusedWindow(BaseModel):
    """The frontmost app, its focused window, and the cursor (validated)."""

    #: Process id of the frontmost application (feeds ``ax_snapshot``).
    pid: int
    #: Application name (its AXTitle; empty for odd hosts).
    #:
    #: **Localized**: on a Turkish desktop Calculator reports "Hesap Makinesi".
    #: Compare against :attr:`bundle_id` too before concluding the frontmost
    #: app is a different one.
    app_name: str
    #: The app's ``CFBundleIdentifier`` ("com.apple.calculator"), "" when the
    #: host has no bundle. Locale-independent, so it — not ``app_name`` — is
    #: what identifies an application across languages.
    bundle_id: str = ""
    #: Title of the focused window inside that app ("" when none).
    window_title: str = ""
    #: Cursor position in global logical points (Y grows down).
    cursor_x: float = 0.0
    cursor_y: float = 0.0


def window_summary(focused: FocusedWindow) -> str:
    """Compact provider-facing description of the focused window (pure).

    e.g. ``Safari — GitHub — computeruse (cursor 420,300)``; the window title
    is omitted when the app has none (a bare desktop). The cursor is part of
    OBSERVE per §5, so it belongs in the one-line summary rather than a
    separate field the provider must remember to read.
    """
    window = f" — {focused.window_title}" if focused.window_title else ""
    return (
        f"{focused.app_name}{window} "
        f"(cursor {focused.cursor_x:.0f},{focused.cursor_y:.0f})"
    )
