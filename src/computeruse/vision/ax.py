"""Accessibility element grounding (ADR-2 *primary* source).

The constitution names the Accessibility API as the primary localization
source: it provides exact roles, titles, and coordinates per element — stable
across DPI and theme changes — and the pixel pipeline *verifies* those
coordinates before acting. This module is the Python half of that boundary:

* :class:`AXElement` is the typed tree the driver's ``ax_snapshot`` RPC
  returns (roles without the ``AX`` prefix, e.g. ``Button``).
* :func:`find_elements` is the pure grounding query — "find the Reload button".
* :func:`element_rect` bridges an element into the vision coordinate layer.

Coordinates are in the *same global logical point space* as
:mod:`computeruse.vision.coordinates` (origin at the primary display's
top-left, Y grows down), so an element's rect feeds directly into
``verification_region`` / capture cropping with no transform.
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel

from computeruse.vision.coordinates import Point, Rect, ScreenMap, Size, point_in_frame

# Roles an agent can meaningfully act on (click, type, toggle). Everything
# else (containers, static text, scroll areas) is noise for a coordinate
# decision — the grounding context stays minimal (Law 4.3).
INTERACTIVE_ROLES: Final[frozenset[str]] = frozenset(
    {
        "Button",
        "CheckBox",
        "RadioButton",
        "ComboBox",
        "PopUpButton",
        "TextField",
        "SecureTextField",
        "SearchField",
        "TextArea",
        "MenuItem",
        "MenuBarItem",
        "Slider",
        "Stepper",
        "DisclosureTriangle",
        "Tab",
        "Link",
        "Heading",
        "Cell",
    }
)


class AXElement(BaseModel):
    """One node of the host accessibility tree (validated driver payload)."""

    role: str
    title: str = ""
    # AXValue: the element's current text content (text fields, sliders).
    # Empty when absent or empty. Lets the orchestrator verify that typed or
    # pasted text actually landed in the focused input (ADR-2 state source).
    value: str = ""
    # Whether this element currently holds keyboard focus (AXFocused). The
    # consent-free "did my click land" signal: after clicking a text field or
    # button, the next snapshot reports it focused — no Screen Recording
    # needed (ADR-2: AX is the state source; pixels verify movement).
    focused: bool = False
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    children: tuple[AXElement, ...] = ()


# Recursive models need an explicit rebuild after the class is fully defined.
AXElement.model_rebuild()


def find_elements(
    root: AXElement,
    *,
    role: str | None = None,
    title: str | None = None,
) -> tuple[AXElement, ...]:
    """Grounding query: depth-first search over the tree (pure).

    ``role`` must match exactly (the driver strips the ``AX`` prefix, so pass
    ``Button``, ``Window``, ...). ``title`` matches case-insensitively as a
    substring, so ``"reload"`` finds the ``"Reload"`` button. Returns every
    match (a window may contain several buttons with the same role); callers
    that expect one should disambiguate by title.
    """
    needle = title.lower() if title is not None else None
    matches: list[AXElement] = []

    def walk(node: AXElement) -> None:
        role_ok = role is None or node.role == role
        title_ok = needle is None or needle in node.title.lower()
        if role_ok and title_ok:
            matches.append(node)
        for child in node.children:
            walk(child)

    walk(root)
    return tuple(matches)


def element_rect(element: AXElement) -> Rect:
    """An element's global logical rect, in the vision coordinate convention."""
    return Rect(
        origin=Point(element.x, element.y),
        size=Size(element.width, element.height),
    )


def element_summary(element: AXElement) -> str:
    """One compact, parseable line describing an actionable element (pure).

    e.g. ``Button "Reload" at (254,80) 44x24`` or ``TextField "..." at
    (520,80) 400x24 value="https://example.com" (focused)`` — plus the focus
    state when set, so a provider can read the location off the line *and*
    confirm a click landed on the next turn (ADR-2). A non-empty AXValue is
    included so the model can confirm text it typed/pasted is visibly present.

    The reported point is the element's **centre**, not its top-left origin.
    The model clicks the coordinate it is given, and a click at an element's
    exact corner sits on its boundary, where any rounding at all lands outside.
    That is not hypothetical: one image pixel is ~3.3 logical points on a
    Retina display, summaries are rounded to whole image pixels before the
    model sees them, and a 12-point-tall link is under 4 pixels. Aiming at
    corners, the model repeatedly reported the right link and clicked one point
    above it, into the row behind — six consecutive misses on a real page.
    Aiming at centres gives every element half its own size as slack, which is
    an order of magnitude more than the rounding can consume.
    """
    label = element.title if element.title else "(untitled)"
    value = f' value="{element.value}"' if element.value else ""
    state = " (focused)" if element.focused else ""
    centre_x = element.x + element.width / 2
    centre_y = element.y + element.height / 2
    return (
        f'{element.role} "{label}" at '
        f"({centre_x:.0f},{centre_y:.0f}) {element.width:.0f}x{element.height:.0f}"
        f"{value}{state}"
    )


# Text-entry roles whose AXValue reflects the user's typed/pasted content.
# SecureTextField is deliberately excluded: its value is redacted by the OS,
# so verification against it would false-fail valid input.
TEXT_VALUE_ROLES: Final[frozenset[str]] = frozenset(
    {"TextField", "SearchField", "TextArea", "ComboBox"}
)


def focused_text_value(root: AXElement) -> str | None:
    """Value of the focused text-entry element, or None when not determinable.

    Returns ``None`` when no focused text-like element exists, when its value
    is empty/absent (the app does not expose AXValue), or for secure fields.
    The ``None`` contract means "insufficient evidence" — callers must skip
    verification rather than treat absence as a failure (Law 2: never claim
    verification without evidence). A non-empty value is trustworthy:
    ``expected not in value`` after a paste/type is a real miss.
    """
    def walk(node: AXElement) -> str | None:
        if node.focused and node.role in TEXT_VALUE_ROLES and node.value:
            return node.value
        for child in node.children:
            found = walk(child)
            if found is not None:
                return found
        return None

    return walk(root)


def is_actionable(node: AXElement) -> bool:
    """Can the agent actually aim at this element (pure)?

    Filters out what has no clickable area. A collapsed menu still reports
    every one of its items through AX, sized 0x0 and parked off the bottom of
    the display; a browser page carries hidden and zero-height nodes for the
    same reason. Observed before this filter: all 24 summary slots went to
    0x0 menu items at y=1112 on an 1112-point display, so the model's entire
    view of the machine was elements it could never click, while the page's
    real links never made the list.
    """
    return node.width > 0 and node.height > 0


def interactive_summaries(
    root: AXElement,
    *,
    max_depth: int = 20,
    max_count: int = 24,
) -> tuple[str, ...]:
    """Compact renderings of actionable elements, web-content first (pure).

    This is what ADR-2's *primary* source feeds the provider: the model sees
    real elements with real coordinates instead of hallucinating them, and
    the pixel pipeline still verifies whatever coordinate it picks. Depth
    must be generous enough for real apps — Chrome's omnibox lives five
    levels below the app root and its page links fourteen to eighteen, so a
    shallow cap silently de-grounds the model and it hallucinates
    coordinates — while ``max_count`` bounds the
    working context regardless of tree depth (Law 4.3); order is
    deterministic.

    Traversal order is deliberately **web-first**: browsers expose the page
    (``AXWebArea``) as a sibling subtree *after* their own chrome (tab strip,
    toolbar, omnibox). A plain DFS with a count cap therefore fills the
    budget with browser chrome and hides the very page links the agent needs
    (observed in the field: Google result links never appeared, and the
    model guessed coordinates). Collecting the ``WebArea`` subtree before the
    rest of the tree keeps the budget on what the user actually interacts
    with. Non-browser apps have no ``WebArea`` and keep plain DFS order.
    """
    summaries: list[str] = []

    def collect(node: AXElement, depth: int) -> None:
        """Append an interactive element (and its subtree) when budget remains."""
        if len(summaries) >= max_count:
            return
        if (
            node.role in INTERACTIVE_ROLES
            and is_actionable(node)
            and not (node.role == "MenuBarItem" and node.title.lower() in ("apple", ""))
        ):
            summaries.append(element_summary(node))
        if depth < max_depth and len(summaries) < max_count:
            for child in node.children:
                collect(child, depth + 1)

    def walk_all(node: AXElement, depth: int) -> None:
        """DFS over the whole tree, never entering WebArea subtrees."""
        if node.role == "WebArea" or len(summaries) >= max_count:
            return
        if (
            node.role in INTERACTIVE_ROLES
            and is_actionable(node)
            and not (node.role == "MenuBarItem" and node.title.lower() in ("apple", ""))
        ):
            summaries.append(element_summary(node))
        # Nodes at exactly max_depth are still collected; only their children
        # are cut off (same depth semantics as the original walk).
        if depth < max_depth:
            for child in node.children:
                walk_all(child, depth + 1)

    def walk_web(node: AXElement, depth: int) -> None:
        """DFS that enters WebArea subtrees (page content) and skips chrome."""
        if len(summaries) >= max_count:
            return
        if node.role == "WebArea":
            collect(node, depth)
            return
        if depth < max_depth:
            for child in node.children:
                walk_web(child, depth + 1)

    # Pass 1: page content. Chrome's WebArea subtree (links, buttons, inputs)
    # is what the agent interacts with; it gets the whole count budget first.
    walk_web(root, 0)
    # Pass 2: everything else (native toolbar, menus, tabs) with the leftover.
    walk_all(root, 0)
    return tuple(summaries)


_SUMMARY_RECT: Final = re.compile(r"at \((\d+),(\d+)\) (\d+)x(\d+)")


def summary_covering(summaries: tuple[str, ...], x: float, y: float) -> str | None:
    """The most specific summarised element whose rect contains a point (pure).

    "Most specific" means smallest by area: a button and the toolbar holding it
    both contain the same point, and only the button says anything useful about
    what a click at that point hit. Returns ``None`` when no summary covers the
    point — which callers must read as "no information", never as "nothing is
    there": the summary list is budget-capped and may simply not include it.

    The summary's point is the element's centre (see :func:`element_summary`),
    so the rect it stands for spans half the width and height either side.
    """
    best: str | None = None
    best_area = float("inf")
    for line in summaries:
        match = _SUMMARY_RECT.search(line)
        if match is None:
            continue
        centre_x, centre_y, width, height = (int(group) for group in match.groups())
        left = centre_x - width / 2
        top = centre_y - height / 2
        if not (left <= x < left + width and top <= y < top + height):
            continue
        area = float(width * height)
        if area < best_area:
            best, best_area = line, area
    return best


_SUMMARY_LABEL: Final = re.compile(r'^\S+ "(.*)" at \(')


def summary_label(summary: str) -> str | None:
    """The element's own title from one summary line (pure).

    The line reads ``Button "Empty Trash" at (100,200) 80x24``; this returns
    ``Empty Trash``. The safety guard classifies the *control* the agent is
    about to press, and the control's title is the only part of the line that
    says anything about what pressing it does — the role, the rect and the
    focus marker are noise for that question, and an element's ``value`` would
    make a search box containing the word "delete" look destructive.

    ``None`` when the line carries no title (the truncation note, or an
    untitled element), which callers must read as "no information".
    """
    match = _SUMMARY_LABEL.match(summary)
    if match is None:
        return None
    label = match.group(1).strip()
    return label if label and label != "(untitled)" else None


def summaries_within(summaries: tuple[str, ...], frame: Rect) -> tuple[str, ...]:
    """Keep only the elements whose centre lies on a given display (pure).

    AX rects are global: on a two-display desktop, an app's tree describes
    windows the captured frame does not contain. Listing those would hand the
    model coordinates that fall outside its own screenshot — negative once
    rewritten into image space, and rejected by the bounds gate if it clicked
    one. Filtering here, *before* the image-space rewrite and before marks are
    numbered, keeps every downstream list derived from the same elements.

    Lines with no coordinate fragment (the truncation note) are always kept:
    they describe the list itself, not a place on it.
    """
    kept: list[str] = []
    for line in summaries:
        match = _SUMMARY_RECT.search(line)
        if match is None:
            kept.append(line)
            continue
        centre_x, centre_y, _width, _height = (int(group) for group in match.groups())
        if point_in_frame(Point(float(centre_x), float(centre_y)), frame):
            kept.append(line)
    return tuple(kept)


def summaries_to_image_space(
    summaries: tuple[str, ...], screen_map: ScreenMap
) -> tuple[str, ...]:
    """Rewrite element summaries from logical points into image-map space (pure).

    The provider works in exactly one coordinate space: the screenshot map the
    VLM sees (``downscale_to_max_side``). AX rects arrive in logical screen
    points, which are *larger* numbers than the map's — a 1512pt-wide display
    maps to a 512px image, so a button at x=232pt sits at x=79px in the image.
    The conversion therefore **divides** by the map's points-per-pixel; the
    runner's actuation gate multiplies by the same number on the way back.

    Getting this direction wrong is not a rounding error: it multiplied every
    AX coordinate by ~3 instead of dividing, so the model was handed positions
    off the right-hand edge of its own screenshot, and the gate then scaled
    them again into coordinates the bounds check rejected outright. That is why
    AX grounding silently never worked and the model fell back to guessing from
    pixels. :class:`~computeruse.vision.coordinates.ScreenMap` now owns both
    directions so the mistake cannot recur.

    The map also carries the captured display's global origin, so an element
    on a secondary display is placed relative to the frame the model is
    actually looking at rather than to the desktop's corner.

    Only the ``at (x,y) WxH`` fragment is rewritten (rounded to integers);
    titles, values, and focus markers pass through untouched. Lines without a
    coordinate fragment (e.g. the truncation note) are unchanged.
    """
    if screen_map.is_identity or not summaries:
        return summaries
    points_per_pixel = screen_map.points_per_pixel

    def rescale(match: re.Match[str]) -> str:
        x, y, width, height = (int(g) for g in match.groups())
        centre = screen_map.to_image(Point(float(x), float(y)))
        return (
            f"at ({round(centre.x)},{round(centre.y)}) "
            f"{round(width / points_per_pixel)}x{round(height / points_per_pixel)}"
        )

    pattern = re.compile(r"at \((\d+),(\d+)\) (\d+)x(\d+)")
    return tuple(pattern.sub(rescale, line) for line in summaries)


#: Cap on the content digest. Bounds the comparison on a text-heavy page
#: without losing the signal: any real change moves one of the first entries.
CONTENT_DIGEST_MAX: Final[int] = 200


def content_digest(root: AXElement) -> tuple[str, ...]:
    """The app's visible text content, for change detection only (pure).

    Never shown to the model — this exists because the two witnesses that were
    supposed to notice an action's effect are both blind to the most common
    effect there is: text changing.

    Measured on Calculator, three real button presses in a row: the interactive
    element list was identical every time (a display is a ``StaticText``, not
    an interactive role, so it is not in that list at all), and the pixel diff
    was unchanged every time — even across the whole window, because a few
    digits redrawing is far below the 15%-of-pixels threshold that keeps a
    cursor blink from reading as a change. The agent pressed the right buttons,
    watched the display update, and was told it had missed. Comparing the text
    itself sees all three presses.

    Roles are included so a value moving between elements still registers, and
    the list is ordered by traversal so the comparison is deterministic.
    """
    digest: list[str] = []

    def walk(node: AXElement) -> None:
        if len(digest) >= CONTENT_DIGEST_MAX:
            return
        text = node.value or node.title
        if text:
            digest.append(f"{node.role}={text}")
        for child in node.children:
            walk(child)

    walk(root)
    return tuple(digest)


def open_tabs_from_tree(root: AXElement) -> tuple[str, ...]:
    """Extract open browser tab titles from the AX tree (pure).

    Chrome and Safari expose their tab bar as a hierarchy of ``Tab``
    elements whose ``title`` is the tab's page title. This function
    collects them so the agent knows which tabs are open — essential
    for detecting stray tabs (e.g. accidental background-tab opens
    from a leaked Cmd+click) and for deciding whether to close or
    switch tabs.

    Returns a deterministic tuple of tab titles. Empty when no ``Tab``
    elements exist (non-browser apps, or the AX tree is absent).
    Duplicate titles are preserved — they reflect real open tabs.
    """
    tabs: list[str] = []

    def walk(node: AXElement) -> None:
        if node.role == "Tab" and node.title:
            tabs.append(node.title)
        for child in node.children:
            walk(child)

    walk(root)
    return tuple(tabs)
