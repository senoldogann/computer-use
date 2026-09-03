"""Set-of-Marks (SoM) Visual Grounding Annotator (Law 1.2 / ADR-2).

Overlays emerald bounding rectangles and corner badges over interactive AX
elements directly onto the screenshot before feeding it to the multimodal
vision model. The badges are *plain colored squares* — the model matches the
highlighted region to the AX summary list; no digits are drawn (the legacy
"numbered badges" wording was wrong — L8).

The OBSERVE step annotates the screenshot map with these boxes before encoding
it, and the AX list the model reads is numbered with the same indices — so a
highlighted region on screen and a line in the list are the same mark, and the
model can select a target by its number (``click_mark``) instead of estimating
a coordinate.

No digits are drawn into the image, deliberately. The frame reaches the model
at OpenAI ``detail: "low"`` — the whole screenshot is about 85 tokens — where a
glyph a few pixels tall is not resolvable at all. The number lives in the text
list, which is exact and free; the drawing's job is to show *which* regions are
grounded, and a coloured box survives that resolution where a numeral does not.

Benefits:
- A mark resolves to the element's own centre in logical points, so a click by
  mark skips image-space rounding entirely (~3.3 points per image pixel).
- Lets weak and strong models refer to visually grounded element regions.
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


def mark_identity(mark: MarkElement) -> str:
    """What names this element across frames, coordinates removed (pure).

    ``label`` is the whole AX summary line, geometry included — ``Button
    "Cancel" at (300, 200) 80x30``. That is the right thing to show a model and
    the wrong thing to compare two frames with: an element that shifted twelve
    points is the same element with a different label. Cutting the coordinate
    tail leaves the part that actually identifies it, so position can be judged
    separately by whoever cares about it.
    """
    return _AX_BOX_PATTERN.sub("", mark.label).strip().casefold()


def parse_ax_elements_to_marks(ui_elements: tuple[str, ...]) -> tuple[MarkElement, ...]:
    """Extract bounding boxes and roles from compact AX element summaries (pure).

    All provided summaries are processed — the caller (agent.py) already caps
    the list at ``AX_MAX_ELEMENTS`` (64), so a second, undocumented 30-element
    slice here would silently drop marks 31-64 (L8).
    """
    marks: list[MarkElement] = []
    for i, summary in enumerate(ui_elements, start=1):
        match = _AX_BOX_PATTERN.search(summary)
        if not match:
            continue
        try:
            x = float(match.group(1))
            y = float(match.group(2))
            w = float(match.group(3))
            h = float(match.group(4))
            role = summary.split()[0] if summary else "Element"
            # ``element_summary`` reports each element at its CENTRE (a click
            # at an element's corner sits on its boundary, where any rounding
            # lands outside it), so the box has to be rebuilt around that
            # point. Reading it as an origin drew every mark half an element
            # down and to the right of the thing it was marking.
            marks.append(
                MarkElement(
                    index=i,
                    label=summary,
                    rect=Rect(Point(x - w / 2, y - h / 2), Size(w, h)),
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
