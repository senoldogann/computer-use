"""Pure regional visual-diff core (ADR-2: pixels as *verifier*).

After each physical action the OODA loop captures the screen *before* and
*after* and asks: did anything change in the target region? A clean yes/no is
used to confirm an action landed (Law 2 self-correction) without relying on
fragile pixel-exact equality.

Why this shape — learned the hard way from visual-regression tooling:

* **Anti-aliasing poisons pixel-exact comparison.** Fonts and window chrome
  shift sub-pixels between renders, so comparing raw pixels flags every
  repaint as a change. We instead *downsample* the region first (smoothing
  those sub-pixel differences) before computing a metric.
* **One number can't separate "animation" from "real change"**. We compute two
  signals: the *mean* absolute luminance change (magnitude) and the *fraction*
  of pixels that moved beyond a per-pixel threshold. A spinner's steady trickle
  differs from a modal that appeared; combining the two lets the caller set
  policy that is robust to both.
* **The crop must come from the vision coordinate layer**, not raw ints, so
  the region is expressed in the same logical/pixel space used for grounding.

Everything here is pure (Law 6): it consumes numeric grids and returns numeric
decisions. Capturing the screen and decoding an image are the connector's job,
kept out of this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from computeruse.vision.coordinates import Rect


class ChangeKind(str, Enum):
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    NOISE = "noise"


@dataclass(frozen=True)
class ChangeVerdict:
    """The pure outcome of a regional diff comparison (no I/O)."""

    kind: ChangeKind
    mean_abs_change: float
    changed_fraction: float


@dataclass(frozen=True)
class Verification:
    """A region plus its diff verdict — the OODA ORIENT-step artifact.

    Binds *where* we looked (``region``) to *what changed* (``verdict``). This
    is what the loop passes along when it asks "did my last action land?". The
    pre/post captures themselves are transient; once the verdict is computed
    only these aggregated numbers travel with the working state (Law 4: keep
    working context minimal, don't carry raw bitmaps around).
    """

    region: Rect
    verdict: ChangeVerdict

    @property
    def changed(self) -> bool:
        return self.verdict.kind in (ChangeKind.CHANGED, ChangeKind.NOISE)



def to_luma(rgba_row: tuple[tuple[int, int, int, int], ...]) -> tuple[float, ...]:
    """Convert an RGBA scanline to per-pixel luminance in [0, 1].

    Luminance uses the standard Rec.709 weights — it isolates *structure* from
    color noise, which is what we care about when deciding a UI changed.
    """
    return tuple(
        (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
        for (r, g, b, _a) in rgba_row
    )


def downsample_luma(luma: tuple[float, ...], to_width: int) -> tuple[float, ...]:
    """Average a scanline down to ``to_width`` buckets (anti-aliasing smear).

    Averaging neighbouring luma before comparing makes the metric tolerant of
    the half-pixel shifts that font rendering produces between frames.
    """
    if to_width <= 0:
        raise ValueError("to_width must be positive")
    if not luma:
        return ()
    factor = len(luma) / to_width
    out: list[float] = []
    for i in range(to_width):
        start = int(i * factor)
        end = max(start + 1, int((i + 1) * factor))
        slice_ = luma[start:end]
        out.append(sum(slice_) / len(slice_))
    return tuple(out)


def crop_luma(
    luma_grid: tuple[tuple[float, ...], ...], region: Rect
) -> tuple[tuple[float, ...], ...]:
    """Extract a region from an already-decoded luma grid.

    ``luma_grid`` is ``(row, col)`` indexed with origin at the grid's own
    top-left; ``region`` uses the same convention (per :mod:`coordinates`).
    """
    x0, y0 = int(region.origin.x), int(region.origin.y)
    x1 = int(region.origin.x + region.size.width)
    y1 = int(region.origin.y + region.size.height)
    x0, x1 = max(0, x0), min(len(luma_grid[0]) if luma_grid else 0, x1)
    y0, y1 = max(0, y0), min(len(luma_grid), y1)
    return tuple(row[x0:x1] for row in luma_grid[y0:y1])


def _mean_abs(yes: list[float]) -> float:
    return sum(yes) / len(yes) if yes else 0.0


def regional_diff(
    before: tuple[tuple[float, ...], ...],
    after: tuple[tuple[float, ...], ...],
    *,
    per_pixel_threshold: float = 0.06,
) -> float:
    """Fraction of pixels that changed beyond a per-pixel luminance threshold.

    Pixels below ``per_pixel_threshold`` are treated as unchanged (jitter,
    anti-aliasing). Returns the fraction of differing pixels in [0, 1].
    """
    if not before or not after:
        return 0.0
    rows = min(len(before), len(after))
    cols = min(len(before[0]), len(after[0]))
    differing = 0
    total = rows * cols
    for r in range(rows):
        for c in range(cols):
            if abs(before[r][c] - after[r][c]) > per_pixel_threshold:
                differing += 1
    return differing / total if total else 0.0


def regional_mean_abs(
    before: tuple[tuple[float, ...], ...],
    after: tuple[tuple[float, ...], ...],
) -> float:
    """Mean absolute luminance difference over the overlapping region."""
    if not before or not after:
        return 0.0
    rows = min(len(before), len(after))
    cols = min(len(before[0]), len(after[0]))
    diffs: list[float] = []
    for r in range(rows):
        for c in range(cols):
            diffs.append(abs(before[r][c] - after[r][c]))
    return _mean_abs(diffs)


def verdict(
    before: tuple[tuple[float, ...], ...],
    after: tuple[tuple[float, ...], ...],
    *,
    kind: Literal["mean", "fraction", "both"] = "both",
    change_fraction_threshold: float = 0.15,
    mean_threshold: float = 0.02,
    noise_max_fraction: float = 0.50,
    noise_max_mean: float = 0.30,
) -> ChangeVerdict:
    """Decide whether a region changed, is just noisy, or is stable.

    ``kind`` selects which signal(s) drive the *whole* decision — both the
    change detection and the noise call-out — so a single-signal mode honours
    its promise consistently (G3):

    * ``fraction`` — a decision rests on the *fraction* of moved pixels only;
      a region is NOISE when that fraction alone clears its noise cap.
    * ``mean`` — a decision rests on the *mean* absolute change only; a region
      is NOISE when that mean alone clears its noise cap.
    * ``both`` (default) — require the *fraction* to clear its threshold while
      the *mean* also clears its own (avoids calling a cursor blink a real
      change); NOISE needs *both* caps exceeded.

    NOISE means a wholesale takeover — an animation or a different view — and
    is reported as a distinct call-out rather than a crisp CHANGED (callers
    may treat it as changed or not, per their policy).
    """
    fraction = regional_diff(before, after)
    mean = regional_mean_abs(before, after)

    # Per-kind change detection: a signal the caller excluded is *neutral*
    # (True), so single-signal modes are decided by their own threshold alone
    # and never vetoed by the other one (G3).
    if kind in ("fraction", "both"):
        fraction_passed = fraction >= change_fraction_threshold
    else:
        fraction_passed = True  # mean mode ignores the fraction threshold
    if kind in ("mean", "both"):
        mean_passed = mean >= mean_threshold
    else:
        mean_passed = True  # fraction mode ignores the mean threshold
    if not (fraction_passed and mean_passed):
        return ChangeVerdict(ChangeKind.UNCHANGED, mean, fraction)

    # Noise uses the same signal(s) the caller chose for change detection.
    if kind == "fraction":
        noisy = fraction >= noise_max_fraction
    elif kind == "mean":
        noisy = mean >= noise_max_mean
    else:
        noisy = fraction >= noise_max_fraction and mean >= noise_max_mean
    return ChangeVerdict(ChangeKind.NOISE if noisy else ChangeKind.CHANGED, mean, fraction)
