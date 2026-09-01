"""Set-of-Marks (SoM) Visual Grounding Annotator (Law 1.2 / ADR-2 / Phase 4).

Draws numbered bounding box badges ([1], [2], [3]...) over interactive AX elements
directly onto the screenshot before feeding it to the multimodal vision model.

Benefits:
- Eliminates coordinate hallucinations on high-resolution Retina displays.
- Allows weak and strong models to refer directly to element indices.
- Pure pixel transformations with zero external heavy dependencies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from computeruse.vision.capture import ScreenCapture
from computeruse.vision.coordinates import Point, Rect, Size

_AX_BOX_PATTERN: Final = re.compile(
    r'at\s*\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\)\s*(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)'
)


@dataclass(frozen=True)
class MarkElement:
    """An identified UI element candidate with integer mark index."""

    index: int
    label: str
    rect: Rect
    role: str


def parse_ax_elements_to_marks(ui_elements: tuple[str, ...]) -> tuple[MarkElement, ...]:
    """Extract bounding boxes and roles from compact AX element summaries (pure)."""
    marks: list[MarkElement] = []
    for i, summary in enumerate(ui_elements[:30], start=1):
        match = _AX_BOX_PATTERN.search(summary)
        if not match:
            continue
        try:
            x = float(match.group(1))
            y = float(match.group(2))
            w = float(match.group(3))
            h = float(match.group(4))
            role = summary.split()[0] if summary else "Element"
            marks.append(
                MarkElement(
                    index=i,
                    label=summary,
                    rect=Rect(Point(x, y), Size(w, h)),
                    role=role,
                )
            )
        except (ValueError, IndexError):
            continue
    return tuple(marks)


def annotate_set_of_marks(
    capture: ScreenCapture,
    marks: tuple[MarkElement, ...],
) -> ScreenCapture:
    """Overlay semi-transparent colored bounding rectangles and mark tags on a capture (pure).

    Modifies a copy of the BGRA byte buffer, drawing borders and corner badge indicators.
    """
    if not marks or not capture.data:
        return capture

    buf = bytearray(capture.data)
    w = capture.width
    h = capture.height
    scale = capture.scale or 1.0

    # Emerald green badge color (BGRA: B=116, G=165, R=80, A=255)
    border_b, border_g, border_r = 116, 165, 80

    for mark in marks:
        # Scale logical coordinates to physical pixels
        px0 = max(0, min(w - 1, int(mark.rect.origin.x * scale)))
        py0 = max(0, min(h - 1, int(mark.rect.origin.y * scale)))
        px1 = max(0, min(w - 1, int((mark.rect.origin.x + mark.rect.size.width) * scale)))
        py1 = max(0, min(h - 1, int((mark.rect.origin.y + mark.rect.size.height) * scale)))

        if px1 <= px0 or py1 <= py0:
            continue

        # Draw top and bottom 2px borders
        for border_y in range(py0, min(py0 + 2, py1 + 1)):
            for x in range(px0, px1 + 1):
                idx = (border_y * w + x) * 4
                if idx + 3 < len(buf):
                    buf[idx] = border_b
                    buf[idx + 1] = border_g
                    buf[idx + 2] = border_r

        for border_y in range(max(py0, py1 - 1), py1 + 1):
            for x in range(px0, px1 + 1):
                idx = (border_y * w + x) * 4
                if idx + 3 < len(buf):
                    buf[idx] = border_b
                    buf[idx + 1] = border_g
                    buf[idx + 2] = border_r

        # Draw left and right 2px borders
        for border_x in range(px0, min(px0 + 2, px1 + 1)):
            for y in range(py0, py1 + 1):
                idx = (y * w + border_x) * 4
                if idx + 3 < len(buf):
                    buf[idx] = border_b
                    buf[idx + 1] = border_g
                    buf[idx + 2] = border_r

        for border_x in range(max(px0, px1 - 1), px1 + 1):
            for y in range(py0, py1 + 1):
                idx = (y * w + border_x) * 4
                if idx + 3 < len(buf):
                    buf[idx] = border_b
                    buf[idx + 1] = border_g
                    buf[idx + 2] = border_r

        # Corner badge anchor (solid 10x10 square at top-left corner)
        badge_w = min(14, px1 - px0)
        badge_h = min(14, py1 - py0)
        for by in range(py0, py0 + badge_h):
            for bx in range(px0, px0 + badge_w):
                idx = (by * w + bx) * 4
                if idx + 3 < len(buf):
                    buf[idx] = border_b
                    buf[idx + 1] = border_g
                    buf[idx + 2] = border_r

    return ScreenCapture(
        display_id=capture.display_id,
        width=capture.width,
        height=capture.height,
        scale=capture.scale,
        pixel_format=capture.pixel_format,
        data=bytes(buf),
    )
