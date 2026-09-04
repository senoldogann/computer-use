"""ADR-2's OCR fallback: seeing a window that exposes no accessibility tree.

Marks are derived from AX elements, so a window that exposes none leaves the
model with no indexed target at all — it can only guess coordinates off a
downscaled screenshot, where one image pixel is several logical points. Games,
virtual machines, remote desktops, a drawing canvas and Electron apps with poor
accessibility are all in that set, and ADR-2 named this fallback for years
before anything implemented it.

The wire-shape tests run against the *real compiled driver* (conftest spawns
one for the session), so the Rust and Python halves are checked against each
other rather than against a mock of one of them.
"""

from __future__ import annotations

from computeruse.agent import AX_BLINDNESS_THRESHOLD, ax_left_us_blind
from computeruse.orchestrator.client import (
    OCR_MAX_LINES,
    OCR_MIN_CONFIDENCE,
    ActuationClient,
)
from computeruse.vision.ax import RecognizedLine, recognized_summaries
from computeruse.vision.som import parse_ax_elements_to_marks
from tests.smoke.conftest import SOCKET_PATH, rpc_call

# --- the wire ---------------------------------------------------------------


def test_recognize_text_wire_shape() -> None:
    payload = rpc_call({"method": "recognize_text", "params": {"display_id": 0}})
    assert payload.get("ok") == "recognize_text"
    assert isinstance(payload.get("lines"), list)


def test_the_optional_budgets_may_be_omitted() -> None:
    """An orchestrator that predates them must still be answered.

    The driver defaults them, so a newer driver never breaks an older caller —
    the same wire-back-compat rule ``ax_snapshot``'s node budget follows.
    """
    bare = rpc_call({"method": "recognize_text", "params": {"display_id": 0}})
    full = rpc_call(
        {
            "method": "recognize_text",
            "params": {
                "display_id": 0,
                "min_confidence": 0.0,
                "max_lines": OCR_MAX_LINES,
            },
        }
    )
    assert bare.get("ok") == full.get("ok") == "recognize_text"


def test_the_line_budget_is_honoured() -> None:
    payload = rpc_call(
        {"method": "recognize_text", "params": {"display_id": 0, "max_lines": 2}}
    )
    lines = payload.get("lines")
    assert isinstance(lines, list)
    assert len(lines) <= 2


def test_a_confidence_floor_above_every_reading_returns_nothing() -> None:
    """The floor has to actually filter, or raising it is a silent no-op."""
    payload = rpc_call(
        {
            "method": "recognize_text",
            "params": {"display_id": 0, "min_confidence": 1.0},
        }
    )
    assert payload.get("lines") == []


def test_the_client_reads_the_driver_back_as_typed_lines() -> None:
    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        lines = client.recognize_text(
            display_id=0,
            window_pid=None,
            min_confidence=OCR_MIN_CONFIDENCE,
            max_lines=OCR_MAX_LINES,
        )
    assert lines, "the simulated fixture must produce text"
    assert all(isinstance(line, RecognizedLine) for line in lines)
    assert all(line.width > 0 and line.height > 0 for line in lines)


# --- becoming marks ---------------------------------------------------------


def test_recognized_lines_render_as_element_summaries() -> None:
    """OCR needs no new marks code: it renders into the shape AX already uses.

    The reported point is the element's *centre*. Aiming at a corner cost six
    consecutive misses on a real page — one image pixel is several logical
    points, and a rounded corner lands outside the target.
    """
    line = RecognizedLine(
        text="Sign in", confidence=0.98, x=380.0, y=309.0, width=64.0, height=18.0
    )
    (summary,) = recognized_summaries((line,))
    assert summary == 'Text "Sign in" at (412,318) 64x18'


def test_ocr_summaries_flow_through_the_existing_mark_parser() -> None:
    """The whole point of matching the format: nothing downstream changes."""
    lines = (
        RecognizedLine(
            text="Sign in", confidence=0.98, x=380.0, y=309.0, width=64.0, height=18.0
        ),
        RecognizedLine(
            text="Cancel", confidence=0.91, x=300.0, y=309.0, width=52.0, height=18.0
        ),
    )
    marks = parse_ax_elements_to_marks(recognized_summaries(lines))

    assert [mark.index for mark in marks] == [1, 2]
    assert marks[0].role == "Text"
    # Round-trips back to the rect it came from: the summary carries the
    # centre, the parser rebuilds the box around it.
    assert marks[0].rect.origin.x == 380.0
    assert marks[0].rect.origin.y == 309.0
    assert marks[0].rect.size.width == 64.0


def test_a_blank_reading_still_names_something_clickable() -> None:
    """Vision can return whitespace; a mark with no label is unreferenceable."""
    line = RecognizedLine(
        text="   ", confidence=0.5, x=10.0, y=10.0, width=20.0, height=10.0
    )
    (summary,) = recognized_summaries((line,))
    assert '"(untitled)"' in summary


# --- when the fallback engages ---------------------------------------------


def test_a_window_with_real_elements_never_pays_for_ocr() -> None:
    """AX is primary: a frame it answered must not also run a Vision pass."""
    rich = tuple(
        f'Button "b{index}" at ({index},10) 20x10' for index in range(AX_BLINDNESS_THRESHOLD + 1)
    )
    assert not ax_left_us_blind(rich, threshold=AX_BLINDNESS_THRESHOLD)


def test_a_window_that_exposed_nothing_falls_back() -> None:
    assert ax_left_us_blind((), threshold=AX_BLINDNESS_THRESHOLD)
    assert ax_left_us_blind(('Button "Close" at (5,5) 10x10',), threshold=AX_BLINDNESS_THRESHOLD)


def test_a_truncation_note_is_not_mistaken_for_an_element() -> None:
    """The note means the tree had MORE to say, so it must not read as less.

    Counting it would invert the decision on exactly the frames that are best
    grounded — the ones so rich the probe ran out of budget.
    """
    truncated = (
        '(AX grounding truncated at 64 elements — page content may be missing)',
    )
    assert ax_left_us_blind(truncated, threshold=AX_BLINDNESS_THRESHOLD)
