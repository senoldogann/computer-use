"""Smoke tests for Phase 4: Set-of-Marks (SoM) visual annotator."""

from __future__ import annotations

from computeruse.vision.capture import ScreenCapture
from computeruse.vision.som import (
    annotate_set_of_marks,
    parse_ax_elements_to_marks,
)


def test_parse_ax_elements_to_marks() -> None:
    """Parses AX element coordinate summaries into MarkElements."""
    summaries = (
        'Button "Reload" at (232, 68) 44x24',
        'TextField "Search" at (320, 68) 400x24 (focused)',
        'InvalidElementWithoutCoords',
    )
    marks = parse_ax_elements_to_marks(summaries)
    assert len(marks) == 2
    assert marks[0].index == 1
    assert marks[0].role == "Button"
    assert marks[0].rect.origin.x == 232.0
    assert marks[0].rect.origin.y == 68.0
    assert marks[0].rect.size.width == 44.0
    assert marks[0].rect.size.height == 24.0

    assert marks[1].index == 2
    assert marks[1].role == "TextField"
    assert marks[1].rect.origin.x == 320.0


def test_annotate_set_of_marks_modifies_buffer() -> None:
    """Annotating a blank capture draws bounding box pixels into the buffer."""
    w, h = 100, 100
    # Blank black BGRA frame (100x100 = 10000 pixels * 4 = 40000 bytes)
    blank_data = bytes([0] * (w * h * 4))
    capture = ScreenCapture(
        display_id=0,
        width=w,
        height=h,
        scale=1.0,
        data=blank_data,
    )

    summaries = ('Button "Test" at (10, 10) 30x20',)
    marks = parse_ax_elements_to_marks(summaries)
    annotated = annotate_set_of_marks(capture, marks)

    assert annotated.data != blank_data
    # Check that border pixel (10, 10) was colored with emerald green (B=116, G=165, R=80)
    idx = (10 * w + 10) * 4
    assert annotated.data[idx] == 116      # B
    assert annotated.data[idx + 1] == 165  # G
    assert annotated.data[idx + 2] == 80   # R
