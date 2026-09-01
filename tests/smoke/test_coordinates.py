"""Pure unit tests for vision coordinate transforms (ADR-2, Law 6).

These never touch the OS; geometry is injected so the math is fully verified.
"""

from __future__ import annotations

import pytest

from computeruse.vision.coordinates import (
    CoordinateOutOfBoundsError,
    DisplayGeometry,
    Point,
    Rect,
    Size,
    display_px_to_global_point,
    global_point_to_display_px,
    pixels_to_point,
    point_in_frame,
    point_to_screenshot_offset,
)


def _retina_display() -> DisplayGeometry:
    # Primary Retina display, 1440x900 logical points, scale 2.0.
    return DisplayGeometry(
        display_id=1,
        frame=Rect(origin=Point(0, 0), size=Size(1440, 900)),
        scale=2.0,
    )


def _secondary_display_off_to_right() -> DisplayGeometry:
    # A non-Retina display (scale 1.0) to the right of the primary.
    return DisplayGeometry(
        display_id=2,
        frame=Rect(origin=Point(1440, 0), size=Size(1920, 1080)),
        scale=1.0,
    )


def test_retina_point_to_pixels_doubles_both_axes() -> None:
    point = global_point_to_display_px(Point(100, 50), _retina_display())
    # 100pt -> 200px, 50pt -> 100px on a 2x display.
    assert point == Point(200, 100)


def test_pixels_to_point_halves() -> None:
    assert pixels_to_point(Point(200, 100), 2.0) == Point(100, 50)


def test_display_offset_accounts_for_secondary_display() -> None:
    # A point far right on the main desktop belongs to the secondary display;
    # its pixels are measured from the secondary's own top-left.
    px = global_point_to_display_px(Point(1600, 60), _secondary_display_off_to_right())
    assert px == Point(160, 60)  # 1600 - 1440 offset = 160px at scale 1.0


def test_display_px_to_global_inverse() -> None:
    display = _retina_display()
    global_point = Point(1200, 300)
    px = global_point_to_display_px(global_point, display)
    assert display_px_to_global_point(px, display) == global_point


def test_point_in_frame_rejects_outsiders() -> None:
    display = _retina_display()
    assert point_in_frame(Point(20, 20), display.frame)
    assert not point_in_frame(Point(1440, 20), display.frame)
    assert not point_in_frame(Point(-1, 20), display.frame)


def test_screenshot_offset_valid_point_round_trips() -> None:
    display = _retina_display()
    offset = point_to_screenshot_offset(Point(100, 50), display, Size(2880, 1800))
    assert offset == Point(200, 100)


def test_screenshot_offset_lands_on_wrong_display_raises() -> None:
    display = _retina_display()
    with pytest.raises(CoordinateOutOfBoundsError):
        point_to_screenshot_offset(Point(1441, 100), display, Size(2880, 1800))


def test_zero_and_negative_scale_rejected() -> None:
    with pytest.raises(ValueError):
        DisplayGeometry(
            display_id=1,
            frame=Rect(origin=Point(0, 0), size=Size(100, 100)),
            scale=0.0,
        )