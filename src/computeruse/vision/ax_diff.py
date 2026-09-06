"""Accessibility (AX) Tree Indexing and Stateful Diffing Engine.

Indexes interactive UI elements with numeric IDs for programmatic code actuation
and computes unified-diff style token-efficient updates (+, -, ~) across turns,
matching OpenAI CUA REPL (`cua.getApp`, `app.getAXState`) design.
Includes historical node tracking and self-healing locator capabilities.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace

from computeruse.vision.ax import AXElement

# Actionable roles that should be indexed for programmatic interaction
INTERACTIVE_ROLES: frozenset[str] = frozenset(
    {
        "Button",
        "RadioButton",
        "CheckBox",
        "TextField",
        "TextArea",
        "PopUpButton",
        "ComboBox",
        "MenuItem",
        "Tab",
        "Link",
        "Slider",
        "Row",
        "Cell",
        "OutlineRow",
        "SearchField",
        "SecureTextField",
    }
)


@dataclass(frozen=True)
class IndexedNode:
    """An accessibility element indexed with a stable numeric identifier for code interaction."""

    index: int
    role: str
    title: str
    x: int
    y: int
    width: int
    height: int
    value: str | None = None
    focused: bool = False

    @property
    def centre_x(self) -> int:
        """Centre X point in logical screen coordinates."""
        return self.x + self.width // 2

    @property
    def centre_y(self) -> int:
        """Centre Y point in logical screen coordinates."""
        return self.y + self.height // 2

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "role": self.role,
            "title": self.title,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "centre_x": self.centre_x,
            "centre_y": self.centre_y,
            "value": self.value,
            "focused": self.focused,
        }

    @property
    def signature(self) -> str:
        """Structural signature used to match elements across state updates."""
        return f"{self.role}:{self.title}:{self.x}:{self.y}:{self.width}:{self.height}"

    def summary_line(self, prefix: str = "") -> str:
        """Format node as a compact representation line."""
        val_str = f' value="{self.value}"' if self.value else ""
        focus_str = " (focused)" if self.focused else ""
        pref = f"{prefix} " if prefix else ""
        return (
            f'{pref}[{self.index}] {self.role} "{self.title}" at '
            f"({self.x},{self.y}) {self.width}x{self.height}{val_str}{focus_str}"
        )


def index_accessible_elements(root: AXElement, start_index: int = 0) -> list[IndexedNode]:
    """Traverse the AXElement tree and index all interactive or actionable controls.

    Depth-first traversal assigns monotonic integers starting at `start_index`.
    """
    indexed: list[IndexedNode] = []
    current_idx = start_index

    def _traverse(node: AXElement) -> None:
        nonlocal current_idx
        norm_role = node.role.removeprefix("AX") if node.role else ""
        is_interactive = (
            node.role in INTERACTIVE_ROLES
            or norm_role in INTERACTIVE_ROLES
            or node.role in {"Window", "Sheet", "Dialog"}
            or norm_role in {"Window", "Sheet", "Dialog"}
        )
        if is_interactive and (node.width > 0 or node.height > 0):
            indexed.append(
                IndexedNode(
                    index=current_idx,
                    role=node.role,
                    title=node.title or "",
                    x=int(node.x),
                    y=int(node.y),
                    width=int(node.width),
                    height=int(node.height),
                    value=node.value or None,
                    focused=node.focused,
                )
            )
            current_idx += 1

        for child in node.children:
            _traverse(child)

    _traverse(root)
    return indexed


def _default_nodes_list() -> list[IndexedNode]:
    return []


def _default_index_map() -> dict[int, IndexedNode]:
    return {}


def _stable_nodes(
    observed: list[IndexedNode], previous: list[IndexedNode], next_index: int,
) -> tuple[list[IndexedNode], int]:
    """Allocate non-recycled IDs in linear time, without mutating observations."""
    by_signature: dict[str, list[IndexedNode]] = {}
    by_identity: dict[tuple[str, str], list[IndexedNode]] = {}
    counts = Counter((node.role, node.title) for node in observed)
    for node in previous:
        by_signature.setdefault(node.signature, []).append(node)
        by_identity.setdefault((node.role, node.title), []).append(node)
    result: list[IndexedNode] = []
    used: set[int] = set()
    for node in observed:
        exact = by_signature.get(node.signature, [])
        same = by_identity.get((node.role, node.title), [])
        candidate = exact[0] if len(exact) == 1 else None
        if candidate is None and node.title and len(same) == counts[(node.role, node.title)] == 1:
            candidate = same[0]
        if candidate is not None and candidate.index not in used:
            index = candidate.index
        else:
            index = next_index
            next_index += 1
        used.add(index)
        result.append(replace(node, index=index))
    return result, next_index


@dataclass
class AXStateTracker:
    """Tracks state and calculates diffs across consecutive turns for an app."""

    app_name: str
    last_window_title: str | None = None
    last_nodes: list[IndexedNode] = field(default_factory=_default_nodes_list)
    current_index_map: dict[int, IndexedNode] = field(default_factory=_default_index_map)
    historical_index_map: dict[int, IndexedNode] = field(default_factory=_default_index_map)
    user_interrupted: bool = False
    next_index: int = 0
    current_window_title: str | None = None

    def mark_user_interruption(self) -> None:
        """Mark that a user input (mouse/keyboard) interrupted the agent."""
        self.user_interrupted = True

    def get_element_by_index(self, index: int) -> IndexedNode | None:
        """Find an indexed element by its index in the latest observation."""
        return self.current_index_map.get(index)

    def get_historical_element(self, index: int) -> IndexedNode | None:
        """Find an indexed element from historical observations (for self-healing)."""
        return self.historical_index_map.get(index) or self.current_index_map.get(index)

    def find_matching_element(self, role: str, title: str) -> IndexedNode | None:
        """Find a currently live element matching role and title (case-insensitive)."""
        clean_title = title.strip().casefold()
        clean_role = role.strip().casefold()
        matches: list[IndexedNode] = []
        for elem in self.current_index_map.values():
            norm_role = elem.role.removeprefix("AX").casefold()
            elem_role = elem.role.strip().casefold()
            if (elem_role == clean_role or norm_role == clean_role) and elem.title.strip().casefold() == clean_title:
                matches.append(elem)
        return matches[0] if len(matches) == 1 else None

    def find_elements(
        self,
        *,
        role: str | None = None,
        title: str | None = None,
        query: str | None = None,
    ) -> list[IndexedNode]:
        """Find live elements matching criteria with fuzzy ranking."""
        scored: list[tuple[int, IndexedNode]] = []
        clean_role = role.strip().removeprefix("AX").casefold() if role else None
        clean_title = title.strip().casefold() if title else None
        clean_query = query.strip().casefold() if query else None

        for elem in self.current_index_map.values():
            score = 0
            elem_norm_role = elem.role.strip().removeprefix("AX").casefold()
            elem_title = elem.title.strip().casefold()
            elem_val = (elem.value or "").strip().casefold()

            # Role filter
            if clean_role is not None:
                if elem_norm_role == clean_role:
                    score += 30
                else:
                    continue  # Role did not match

            # Title filter
            if clean_title is not None:
                if elem_title == clean_title:
                    score += 70
                elif clean_title in elem_title:
                    score += 40
                else:
                    continue  # Title did not match

            # Query filter (matches either title or value or role)
            if clean_query is not None:
                if elem_title == clean_query:
                    score += 100
                elif clean_query in elem_title:
                    score += 60
                elif elem_val and clean_query in elem_val:
                    score += 40
                elif clean_query in elem_norm_role:
                    score += 20
                else:
                    continue  # Query did not match

            if score > 0 or (role is None and title is None and query is None):
                scored.append((score, elem))

        # Sort descending by score, ascending by index for stability
        scored.sort(key=lambda s: (-s[0], s[1].index))
        return [elem for _, elem in scored]

    def find_element(
        self,
        *,
        role: str | None = None,
        title: str | None = None,
        query: str | None = None,
    ) -> IndexedNode | None:
        """Find top-ranked element matching search criteria."""
        matches = self.find_elements(role=role, title=title, query=query)
        return matches[0] if matches else None

    def refresh_state(self, root: AXElement, window_title: str) -> None:
        """Refresh actuation identity without consuming the model-visible diff."""
        observed = index_accessible_elements(root, start_index=0)
        # IDs are never recycled. A banner cannot silently steal a button's ID.
        # Only unique identities survive movement; repeated labels must be
        # re-observed instead of guessing which identical control was intended.
        if self.current_window_title is not None and self.current_window_title != window_title:
            self.historical_index_map = {}
        new_nodes, self.next_index = _stable_nodes(
            observed, list(self.historical_index_map.values()), self.next_index,
        )
        self.current_window_title = window_title
        self.current_index_map = {node.index: node for node in new_nodes}
        self.historical_index_map.update(self.current_index_map)

    def render_state(
        self,
        root: AXElement,
        window_title: str,
        disable_diffing: bool = False,
    ) -> str:
        """Generate token-efficient state string (initial full tree, or incremental diff)."""
        self.refresh_state(root, window_title)
        new_nodes = list(self.current_index_map.values())

        # Handle user disruption / drift guard
        if self.user_interrupted:
            self.user_interrupted = False
            return (
                f"The user changed '{self.app_name}'. Re-query the latest state with "
                "`await app.getAXState({disableDiffing: true})` before proceeding."
            )

        # First observation or diffing disabled -> render full tree
        if disable_diffing or not self.last_nodes:
            self.last_nodes = new_nodes
            self.last_window_title = window_title
            lines: list[str] = [
                "## Computer Use",
                f'Window: "{window_title}", App: {self.app_name}.',
                "",
                "Accessibility Tree:",
            ]
            for n in new_nodes:
                lines.append(n.summary_line())
            return "\n".join(lines)

        # Compute diff between last_nodes and new_nodes
        old_sigs: dict[str, IndexedNode] = {n.signature: n for n in self.last_nodes}
        new_sigs: dict[str, IndexedNode] = {n.signature: n for n in new_nodes}

        added: list[IndexedNode] = [n for n in new_nodes if n.signature not in old_sigs]
        removed: list[IndexedNode] = [n for n in self.last_nodes if n.signature not in new_sigs]
        changed: list[IndexedNode] = []

        for n in new_nodes:
            if n.signature in old_sigs:
                old = old_sigs[n.signature]
                if n.index != old.index or n.focused != old.focused or n.value != old.value:
                    changed.append(n)

        self.last_nodes = new_nodes
        self.last_window_title = window_title

        if not added and not removed and not changed:
            return f'There has been no change in the accessibility tree for Window: "{window_title}".'

        lines = [
            f'The following is a diff from the previous accessibility tree for Window: "{window_title}" with {self.app_name}:'
        ]
        for item_added in added:
            lines.append(item_added.summary_line(prefix="+"))
        for item_removed in removed:
            lines.append(item_removed.summary_line(prefix="-"))
        for item_changed in changed:
            lines.append(item_changed.summary_line(prefix="~"))

        return "\n".join(lines)
