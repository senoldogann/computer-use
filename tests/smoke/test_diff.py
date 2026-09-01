"""Pure unit tests for the visual-diff core (ADR-2 pixels-as-verifier)."""

from __future__ import annotations

import pytest

from computeruse.vision.coordinates import Point, Rect, Size
from computeruse.vision.diff import (
    ChangeKind,
    crop_luma,
    downsample_luma,
    regional_diff,
    to_luma,
    verdict,
)


def test_to_luma_rec709_weighted() -> None:
    # Pure red -> 0.2126, pure blue -> 0.0722, white -> 1.0.
    assert to_luma(((255, 0, 0, 255),)) == pytest.approx((0.2126,))
    assert to_luma(((0, 0, 255, 255),)) == pytest.approx((0.0722,))
    assert to_luma(((255, 255, 255, 255),)) == pytest.approx((1.0,))


def test_downsample_averages() -> None:
    luma = (0.0, 1.0, 0.0, 1.0)
    assert downsample_luma(luma, 2) == (0.5, 0.5)


def test_regional_diff_counts_moved_pixels() -> None:
    before = ((0.0, 0.0), (0.0, 0.0))
    after = ((1.0, 0.0), (0.0, 0.0))
    # 1 of 4 pixels changed -> fraction 0.25.
    assert regional_diff(before, after) == 0.25


def test_regional_diff_ignores_sub_threshold_jitter() -> None:
    # A 0.01 luminance wiggle is anti-aliasing noise, not a change.
    before = ((0.5,),)
    after = ((0.51,),)
    assert regional_diff(before, after, per_pixel_threshold=0.06) == 0.0


def test_verdict_unchanged_for_stable_region() -> None:
    before = ((0.4, 0.4), (0.4, 0.4))
    after = ((0.41, 0.39), (0.4, 0.4))
    assert verdict(before, after).kind == ChangeKind.UNCHANGED


def test_verdict_changed_for_real_region_change() -> None:
    # A *partial* but strong change: the top row lights up in a 4x4 block while
    # the rest stay put. Changed fraction = 4/16 = 0.25 (>=0.15) but below the
    # wholesale-noise bar (0.50), so -> CHANGED, not NOISE.
    before = ((0.2,) * 4, (0.2,) * 4, (0.2,) * 4, (0.2,) * 4)
    after = ((0.9,) * 4, (0.2,) * 4, (0.2,) * 4, (0.2,) * 4)
    assert verdict(before, after).kind == ChangeKind.CHANGED


def test_verdict_noise_for_wholesale_change() -> None:
    # Entire block replaced = likely a transition frame, not a targeted result.
    before = ((0.1, 0.1), (0.1, 0.1))
    after = ((0.9, 0.9), (0.9, 0.9))
    assert verdict(before, after).kind == ChangeKind.NOISE


def test_crop_luma_extracts_region() -> None:
    grid = ((0, 1, 2), (3, 4, 5), (6, 7, 8))
    cropped = crop_luma(grid, Rect(origin=Point(1, 1), size=Size(2, 2)))
    assert cropped == ((4, 5), (7, 8))