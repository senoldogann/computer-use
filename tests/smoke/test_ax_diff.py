"""Tests for accessibility diffing and element indexing in CUA REPL mode."""

from __future__ import annotations

from computeruse.vision.ax import AXElement
from computeruse.vision.ax_diff import AXStateTracker, index_accessible_elements


def test_index_accessible_elements_assigns_sequential_indices() -> None:
    root = AXElement(
        role="Window",
        title="Main",
        x=0,
        y=0,
        width=800,
        height=600,
        children=(
            AXElement(role="Button", title="Cancel", x=100, y=100, width=50, height=30),
            AXElement(role="Button", title="Open", x=160, y=100, width=50, height=30),
            AXElement(
                role="TextField",
                title="Filename",
                value="test.txt",
                x=50,
                y=50,
                width=200,
                height=25,
            ),
        ),
    )
    nodes = index_accessible_elements(root)
    assert len(nodes) == 4  # Window, Cancel, Open, TextField
    assert [n.index for n in nodes] == [0, 1, 2, 3]
    # Check centre calculations
    cancel = nodes[1]
    assert cancel.title == "Cancel"
    assert cancel.centre_x == 125.0
    assert cancel.centre_y == 115.0


def test_ax_state_tracker_first_call_and_disable_diffing() -> None:
    tracker = AXStateTracker(app_name="TextEdit")
    root = AXElement(
        role="Window",
        title="Open",
        x=0,
        y=0,
        width=400,
        height=300,
        children=(
            AXElement(role="Button", title="Vazgeç", x=10, y=10, width=60, height=20),
        ),
    )
    # First call produces full tree
    rendered = tracker.render_state(root, "Open")
    assert 'Window: "Open", App: TextEdit.' in rendered
    assert '[1] Button "Vazgeç"' in rendered

    # Second call with no change produces "no change" message
    unchanged = tracker.render_state(root, "Open")
    assert (
        'There has been no change in the accessibility tree for Window: "Open".'
        == unchanged
    )

    # Second call with disable_diffing produces full tree again
    forced = tracker.render_state(root, "Open", disable_diffing=True)
    assert 'Window: "Open", App: TextEdit.' in forced


def test_ax_state_tracker_diffs_added_and_removed_elements() -> None:
    tracker = AXStateTracker(app_name="TextEdit")
    root1 = AXElement(
        role="Window",
        title="Open",
        x=0,
        y=0,
        width=400,
        height=300,
        children=(
            AXElement(role="Button", title="Vazgeç", x=10, y=10, width=60, height=20),
        ),
    )
    tracker.render_state(root1, "Open")

    root2 = AXElement(
        role="Window",
        title="Open",
        x=0,
        y=0,
        width=400,
        height=300,
        children=(
            AXElement(role="Button", title="Vazgeç", x=10, y=10, width=60, height=20),
            AXElement(role="Button", title="Aç", x=80, y=10, width=60, height=20),
        ),
    )
    diff = tracker.render_state(root2, "Open")
    assert (
        'The following is a diff from the previous accessibility tree for Window: "Open"'
        in diff
    )
    assert '+ [2] Button "Aç"' in diff


def test_ax_state_tracker_user_interruption() -> None:
    tracker = AXStateTracker(app_name="TextEdit")
    tracker.mark_user_interruption()
    rendered = tracker.render_state(
        AXElement(role="Window", title="Main", width=10, height=10), "Main"
    )
    assert "The user changed 'TextEdit'." in rendered
    assert "Re-query the latest state" in rendered
