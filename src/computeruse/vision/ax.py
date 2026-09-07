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
from dataclasses import dataclass
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
    subrole: str = ""
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


class RecognizedLine(BaseModel):
    """One line of text the OCR fallback read off the screen (ADR-2).

    The same coordinate space and field names as :class:`AXElement`, in global
    logical points, because both are grounding sources and the orchestrator
    should not need two mental models for "where is this control".
    """

    text: str
    #: Vision's own score, 0..1. Carried so a caller can raise the floor
    #: without a driver rebuild.
    confidence: float
    x: float
    y: float
    width: float
    height: float


def recognized_summaries(lines: tuple[RecognizedLine, ...]) -> tuple[str, ...]:
    """Render OCR lines in the same summary shape elements use (pure).

    Deliberately identical in format to :func:`element_summary`, which is what
    lets the whole downstream pipeline consume OCR with no changes at all: the
    viewport cull, the image-space rewrite and the Set-of-Marks parser all
    drive off one regex over this line shape.

    The reported point is the **centre**, for the reason
    :func:`element_summary` documents at length — aiming at a corner cost six
    consecutive misses on a real page, because one image pixel is several
    logical points and a rounded corner lands outside the target.

    The role is spelled ``Text`` rather than borrowed from the accessibility
    vocabulary: it is honest about where the reading came from, and it tells
    the model that this target was read off pixels rather than declared by the
    application.
    """
    summaries: list[str] = []
    for line in lines:
        label = line.text.strip() or "(untitled)"
        centre_x = line.x + line.width / 2
        centre_y = line.y + line.height / 2
        summaries.append(
            f'Text "{label}" at '
            f"({centre_x:.0f},{centre_y:.0f}) {line.width:.0f}x{line.height:.0f}"
        )
    return tuple(summaries)


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


#: The role macOS gives a field whose contents it redacts — a password box.
#: Its *presence* is perfectly visible in the accessibility tree even though
#: its value is not, which is what makes it a reliable signal that the screen
#: is asking for a credential.
SECURE_FIELD_ROLE: Final[str] = "SecureTextField"
SECURE_FIELD_ROLES: Final[frozenset[str]] = frozenset(
    {"SecureTextField", "AXSecureTextField", "NSSecureTextField"}
)


def is_secure_field(node: AXElement) -> bool:
    """Is this element a password / redacted text field (pure)?"""
    subrole = (node.subrole or "").strip().lower()
    role = (node.role or "").strip().lower()
    return (
        node.role in SECURE_FIELD_ROLES
        or node.subrole in SECURE_FIELD_ROLES
        or "secure" in subrole
        or "secure" in role
    )


def asks_for_a_credential(root: AXElement) -> bool:
    """Is a password field on screen at all (pure)?

    Deliberately broader than "is one focused". Focus can move between the
    snapshot the model decided from and the moment a keystroke lands, and
    typing a password into the wrong field is worse than not typing it — so
    the presence of the box anywhere on screen is the signal, and it fails
    closed.

    The apparent false positive is the wanted behaviour: on a sign-in form the
    agent cannot complete the sign-in anyway, so filling the username field
    achieves nothing except leaving a half-filled form and a person who now has
    to work out what happened. "This one needs you" is the honest answer to the
    whole screen, not to one field on it.
    """

    def walk(node: AXElement) -> bool:
        if is_secure_field(node):
            return True
        return any(walk(child) for child in node.children)

    return walk(root)


def is_actionable(node: AXElement, viewport: Rect | None = None) -> bool:
    """Can the agent actually aim at this element (pure)?

    Filters out what has no clickable area. A collapsed menu still reports
    every one of its items through AX, sized 0x0 and parked off the bottom of
    the display; a browser page carries hidden and zero-height nodes for the
    same reason. Observed before this filter: all 24 summary slots went to
    0x0 menu items at y=1112 on an 1112-point display, so the model's entire
    view of the machine was elements it could never click, while the page's
    real links never made the list.

    ``viewport`` extends the same reasoning from size to position: an element
    outside the observed display cannot be clicked and cannot be read off the
    screenshot either, so it is not a target — it is budget spent on nothing.
    Measured on a GitHub issues page: 1525 accessibility nodes, **429 of them
    on screen**. The other 1096 — collapsed menus, rows below the fold, other
    windows — were competing for the same capped list as the answer the task
    depended on, and whether that answer made the cut was luck.

    Intersection, not containment: an element half-scrolled off the top is
    still clickable on the part that shows.
    """
    if node.width <= 0 or node.height <= 0:
        return False
    if viewport is None:
        return True
    return (
        node.x < viewport.origin.x + viewport.size.width
        and node.x + node.width > viewport.origin.x
        and node.y < viewport.origin.y + viewport.size.height
        and node.y + node.height > viewport.origin.y
    )


def interactive_summaries(
    root: AXElement,
    *,
    max_depth: int = 20,
    max_count: int = 24,
    viewport: Rect | None = None,
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
            and is_actionable(node, viewport)
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
            and is_actionable(node, viewport)
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


_SUMMARY_RECT: Final = re.compile(r"at \((-?\d+),(-?\d+)\) (\d+)x(\d+)")


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


#: Greedy on the title for the same reason :data:`_SUMMARY_LABEL` is, so the
#: two never disagree about where an element's title ends.
_SUMMARY_IDENTITY: Final = re.compile(r'^(\S+ ".*") at \(')


def element_identity(summary: str) -> str | None:
    """The run-stable identity of one summarised element (pure).

    The line reads ``Button "Save" at (100,200) 80x24 value="x" (focused)``;
    this returns ``Button "Save"``. Everything the regex drops is the part that
    moves between two runs of the same workflow: the rect drifts when a window
    is repositioned, ``value`` is the field's contents rather than the field,
    and the focus marker follows whatever the user last clicked.

    What is left is what makes a click on Save a different act from a click on
    Delete. The de-duplication signature deliberately excludes coordinates
    (:func:`computeruse.skills.distiller.signature_of`), and without an identity
    to put in their place two clicks are two clicks: "save the draft" and
    "delete the draft" — both a click into the document then a click on a
    toolbar button — hashed identically, so the second was filed as a duplicate
    of the first and never learned.

    ``None`` when the line carries no identity fragment (the truncation note),
    which callers must read as "no information", never as "nothing was there".
    """
    match = _SUMMARY_IDENTITY.match(summary)
    return match.group(1) if match is not None else None


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
    # Per axis, matching ``to_image``: rounding in the downscale makes the two
    # ratios differ slightly, and a width scaled by the Y factor describes a
    # box the model is not looking at.
    per_pixel_x = screen_map.points_per_pixel_x
    per_pixel_y = screen_map.points_per_pixel_y

    def rescale(match: re.Match[str]) -> str:
        x, y, width, height = (int(g) for g in match.groups())
        centre = screen_map.to_image(Point(float(x), float(y)))
        return (
            f"at ({round(centre.x)},{round(centre.y)}) "
            f"{round(width / per_pixel_x)}x{round(height / per_pixel_y)}"
        )

    return tuple(_SUMMARY_RECT.sub(rescale, line) for line in summaries)


#: Cap on the content digest. Bounds the comparison on a text-heavy page
#: without losing the signal: any real change moves one of the first entries.
CONTENT_DIGEST_MAX: Final[int] = 200


def content_digest(root: AXElement, viewport: Rect | None = None) -> tuple[str, ...]:
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
        # Off-screen text is not evidence about what the agent just did, and
        # including it crowded the cap: on one page 72% of nodes were not
        # visible, so whether a visible fact survived truncation was chance.
        if text and (viewport is None or is_actionable(node, viewport)):
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


# ---------------------------------------------------------------------------
# Popup / consent dialog detection
# ---------------------------------------------------------------------------
#
# A consent banner, cookie wall or sign-in sheet stops a task dead: the model
# sees the page behind it, plans a click on a link the dialog covers, and the
# click lands on the overlay instead — then the recovery ladder calls it a
# miss and the run spends its budget re-aiming at an obstacle it never named.
# These helpers make the overlay *visible to the model*: they find modal
# containers in the AX tree, surface their controls as the FIRST numbered
# marks in the grounding list (so they always survive the element budget),
# and annotate them in the prompt so the agent knows to resolve them first.


@dataclass(frozen=True)
class Dialogue:
    """One detected modal container and the controls it offers (pure data).

    ``kind`` is the container's AX role for native dialogs (``Sheet``,
    ``Dialog``, ``Popover``) or ``Overlay`` for keyword-gated web overlays.
    ``label`` is the container's own title when it has one, else the first
    text found inside it — the fragment the prompt uses to name the dialog.
    ``elements`` are the actionable descendants (buttons, links, checkboxes)
    with real titles, ordered by tree traversal, each culled to the viewport.
    """

    kind: str
    label: str
    elements: tuple[AXElement, ...]


#: Roles that are modal containers by construction on macOS. A Sheet, Dialog
#: or Popover blocks its app until answered, so any actionable child inside
#: one is a control the agent may need to press — no keyword gate needed.
DIALOG_CONTAINER_ROLES: Final[frozenset[str]] = frozenset(
    {"Sheet", "Dialog", "Popover"}
)

#: Subroles that also name a modal container. Some apps expose a dialog as a
#: Window or Group with a dialog-ish subrole rather than a dedicated role;
#: matching the subrole catches those without trusting the tree to always
#: pick the same spelling (the driver strips the ``AX`` prefix, so these are
#: ``Dialog``/``Sheet``/``Popover``).
DIALOG_CONTAINER_SUBROLES: Final[frozenset[str]] = frozenset(
    {"Dialog", "Sheet", "Popover"}
)

#: Container roles admitted to the *web-overlay* path. Web consent dialogs
#: are plain page groups — the modal web has no dedicated role — so these
#: are gated by content keywords (see below) instead of by role.
WEB_OVERLAY_ROLES: Final[frozenset[str]] = frozenset({"Group", "Section"})

#: Consent/privacy context terms that must appear somewhere inside a web
#: overlay's subtree for it to count as a consent-style dialog. Matched
#: across languages — a Turkish page says "çerez", an English one "cookie"
#: — the same way the capability grants match verb families across languages
#: (Law 5.1), so a consent wall can never dodge detection by speaking Turkish.
CONSENT_CONTEXT_KEYWORDS: Final[tuple[str, ...]] = (
    "consent",
    "cookie",
    "cerez",
    "çerez",
    "kabul",
    "kvkk",
    "gizlilik",
    "privacy",
    "oturum",
    "login",
    "sign in",
    "giriş",
    "giris",
    "kişisel",
    "kisisel",
    "verilerinizin",
    "izin",
    "onay",
)

#: Decision terms that must appear on at least one actionable child of a web
#: overlay for it to count. "Kabul Et" and "Reddet" are the common Turkish
#: pair, "Consent" / "Do not consent" the English one, "Şenol olarak devam
#: et" the sign-in sheet — the gate is deliberately the *pair* of context +
#: decision keywords, so an ordinary page section that merely mentions
#: cookies next to an unrelated button is never mistaken for a modal.
WEB_DECISION_KEYWORDS: Final[tuple[str, ...]] = (
    "accept",
    "consent",
    "reject",
    "allow",
    "kabul",
    "reddet",
    "onayla",
    "manage",
    "ayarlar",
    "devam",
    "continue",
    "sign in",
    "giriş",
    "giris",
    "close",
    "kapat",
    "dismiss",
    "tamam",
    "ok",
)


#: Cap on the actionable controls collected per dialogue. A settings sheet
#: can carry a dozen buttons, but the model only needs the first handful to
#: understand and resolve the dialog — the rest compete with the page for
#: the element budget (Law 4.3) without adding a decision.
DIALOGUE_MAX_ELEMENTS: Final[int] = 8


def _container_kind(node: AXElement) -> str | None:
    """The dialogue kind a node represents, or None (pure)."""
    if node.role in DIALOG_CONTAINER_ROLES:
        return node.role
    if (node.subrole or "") in DIALOG_CONTAINER_SUBROLES:
        return node.subrole
    if node.role in WEB_OVERLAY_ROLES:
        return "Overlay"
    return None


def _title_has_keyword(element: AXElement, keywords: tuple[str, ...]) -> bool:
    """Whether an element's title contains any lowercase keyword (pure)."""
    title = (element.title or "").lower()
    return any(keyword in title for keyword in keywords)


def _subtree_mentions_consent(node: AXElement) -> bool:
    """Whether any text in the subtree names a consent/privacy concern (pure).

    Short-circuits on the first hit and never builds the subtree text into
    one string, so gating a page full of ordinary groups stays cheap.
    """
    def walk(current: AXElement) -> bool:
        for text in (current.title, current.value):
            if not text:
                continue
            lowered = text.lower()
            if any(keyword in lowered for keyword in CONSENT_CONTEXT_KEYWORDS):
                return True
        return any(walk(child) for child in current.children)

    return walk(node)


def _collect_actionable(
    node: AXElement, viewport: Rect | None, *, max_elements: int
) -> tuple[AXElement, ...]:
    """Actionable descendants of a container, culled to the viewport (pure)."""
    collected: list[AXElement] = []

    def walk(current: AXElement) -> None:
        if len(collected) >= max_elements:
            return
        if (
            current.role in INTERACTIVE_ROLES
            and (current.title or current.value)
            and is_actionable(current, viewport)
        ):
            collected.append(current)
        for child in current.children:
            walk(child)

    walk(node)
    return tuple(collected)


def _first_text(node: AXElement) -> str:
    """The first title or value found in a subtree (pure)."""
    def walk(current: AXElement) -> str:
        if current.title:
            return current.title
        if current.value:
            return current.value
        for child in current.children:
            found = walk(child)
            if found:
                return found
        return ""

    return walk(node)


def detect_dialogs(
    root: AXElement, *, viewport: Rect | None = None, max_elements: int = DIALOGUE_MAX_ELEMENTS
) -> tuple[Dialogue, ...]:
    """Find modal / consent-style containers in an AX tree (pure).

    Two tiers, matching how macOS and the web differ:

    * **Native containers** — ``Sheet``, ``Dialog``, ``Popover`` roles, or any
      node whose subrole names one — are modal by construction. Any actionable
      child inside one is reported; no keyword evidence required.
    * **Web overlays** — plain ``Group``/``Section`` nodes inside a page — are
      reported only when their subtree mentions a consent/privacy concern
      *and* at least one of their controls carries a decision word. The pair
      gate keeps an ordinary page section that mentions cookies next to an
      unrelated button from reading as a modal.

    Containers nested inside a reported dialogue are skipped (their controls
    are already part of the outer report), while sibling dialogues — a sign-in
    sheet *and* a cookie wall on the same page — are each reported, which is
    exactly the multi-overlay case observed in the field.

    The container's own title, or the first text in it, becomes the label the
    prompt names the dialog by. Deterministic DFS order throughout.
    """
    found: list[Dialogue] = []

    def walk(node: AXElement, inside_dialogue: bool) -> None:
        kind = _container_kind(node)
        if kind is not None:
            elements = _collect_actionable(node, viewport, max_elements=max_elements)
            if elements and not inside_dialogue:
                if kind == "Overlay" and not (
                    _subtree_mentions_consent(node)
                    and any(
                        _title_has_keyword(element, WEB_DECISION_KEYWORDS)
                        for element in elements
                    )
                ):
                    # An ordinary page section wearing a dialog-ish shape:
                    # not a modal — descend and keep looking.
                    pass
                else:
                    label = node.title or node.value or ""
                    if not label:
                        label = _first_text(node)
                    found.append(
                        Dialogue(
                            kind=kind,
                            label=label[:120],
                            elements=elements,
                        )
                    )
                    for child in node.children:
                        walk(child, inside_dialogue=True)
                    return
        for child in node.children:
            walk(child, inside_dialogue)

    walk(root, False)
    return tuple(found)


def dialogue_element_summaries(dialogs: tuple[Dialogue, ...]) -> tuple[str, ...]:
    """Grounding lines for every control a detected dialog offers (pure).

    Same format as :func:`element_summary`, so these lines prepend cleanly to
    the interactive list and inherit the whole downstream pipeline: display
    culling, the image-space rewrite, mark numbering and the click resolution.
    Prepending is what makes the dialog's controls the FIRST numbered marks —
    the easiest clicks in the whole list, exactly where a blocked agent needs
    them.
    """
    return tuple(
        element_summary(element)
        for dialogue in dialogs
        for element in dialogue.elements
    )


def dialogue_notes(dialogs: tuple[Dialogue, ...], *, max_buttons: int = 4) -> tuple[str, ...]:
    """One prompt line per detected dialog, naming it and its controls (pure).

    e.g. ``Sheet "Save changes?" — controls: Save, Don't Save`` or
    ``Overlay "Webtekno.com asks for your consent…" — controls: Do not
    consent, Consent, Manage options``. The label and the control titles are
    page/app text and must stay inside the prompt's observed-data framing
    (they are rendered through :mod:`computeruse.orchestrator.untrusted`),
    never concatenated as instructions.
    """
    notes: list[str] = []
    for dialogue in dialogs:
        labels = [element.title or "(untitled)" for element in dialogue.elements]
        if len(labels) > max_buttons:
            labels = labels[:max_buttons] + ["…"]
        label = dialogue.label or dialogue.kind
        notes.append(f'{dialogue.kind} "{label}" — controls: {", ".join(labels)}')
    return tuple(notes)
