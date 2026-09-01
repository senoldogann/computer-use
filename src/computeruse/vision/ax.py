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

from typing import Final

from pydantic import BaseModel

from computeruse.vision.coordinates import Point, Rect, Size

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

    e.g. ``Button "Reload" at (232,68) 44x24`` or ``TextField "..." at
    (320,68) 400x24 value="https://example.com" (focused)`` — coordinates and
    size in the global logical space, plus the focus state when set, so a
    provider can read the location off the line *and* confirm a click landed
    on the next turn (ADR-2). A non-empty AXValue is included so the model
    can confirm text it typed/pasted is visibly present in the field.
    """
    label = element.title if element.title else "(untitled)"
    value = f' value="{element.value}"' if element.value else ""
    state = " (focused)" if element.focused else ""
    return (
        f'{element.role} "{label}" at '
        f"({element.x:.0f},{element.y:.0f}) {element.width:.0f}x{element.height:.0f}"
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


def interactive_summaries(
    root: AXElement,
    *,
    max_depth: int = 12,
    max_count: int = 24,
) -> tuple[str, ...]:
    """Compact renderings of actionable elements, depth-first (pure).

    This is what ADR-2's *primary* source feeds the provider: the model sees
    real elements with real coordinates instead of hallucinating them, and
    the pixel pipeline still verifies whatever coordinate it picks. Depth
    must be generous enough for real apps — Chrome's omnibox lives five
    levels below the app root, so a shallow cap silently de-grounds the
    model and it hallucinates coordinates — while ``max_count`` bounds the
    working context regardless of tree depth (Law 4.3); order is
    deterministic DFS.
    """
    summaries: list[str] = []

    def walk(node: AXElement, depth: int) -> None:
        if len(summaries) >= max_count:
            return
        if node.role in INTERACTIVE_ROLES and not (
            node.role == "MenuBarItem" and node.title.lower() in ("apple", "")
        ):
            summaries.append(element_summary(node))
        if depth < max_depth and len(summaries) < max_count:
            for child in node.children:
                walk(child, depth + 1)

    walk(root, 0)
    return tuple(summaries)


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
