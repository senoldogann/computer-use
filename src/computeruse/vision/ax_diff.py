"""Accessibility (AX) Tree Indexing and Stateful Diffing Engine.

Indexes interactive UI elements with numeric IDs for programmatic code actuation
and computes unified-diff style token-efficient updates (+, -, ~) across turns,
matching OpenAI CUA REPL (`cua.getApp`, `app.getAXState`) design.
Includes historical node tracking and self-healing locator capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
        is_interactive = node.role in INTERACTIVE_ROLES or node.role in {"Window", "Sheet", "Dialog"}
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


@dataclass
class AXStateTracker:
    """Tracks state and calculates diffs across consecutive turns for an app."""

    app_name: str
    last_window_title: str | None = None
    last_nodes: list[IndexedNode] = field(default_factory=_default_nodes_list)
    current_index_map: dict[int, IndexedNode] = field(default_factory=_default_index_map)
    historical_index_map: dict[int, IndexedNode] = field(default_factory=_default_index_map)
    user_interrupted: bool = False

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
        for elem in self.current_index_map.values():
            if elem.role.strip().casefold() == clean_role and elem.title.strip().casefold() == clean_title:
                return elem
        return None

    def render_state(
        self,
        root: AXElement,
        window_title: str,
        disable_diffing: bool = False,
    ) -> str:
        """Generate token-efficient state string (initial full tree, or incremental diff)."""
        new_nodes = index_accessible_elements(root, start_index=0)
        self.current_index_map = {node.index: node for node in new_nodes}
        self.historical_index_map.update(self.current_index_map)

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
                if n.focused != old.focused or n.value != old.value:
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
