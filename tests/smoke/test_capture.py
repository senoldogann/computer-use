"""Capture connector tests: pure decode + end-to-end through the real driver.

The OODA OBSERVE step's sensor is the driver's ``screenshot`` method. These
tests pin down (1) the pure BGRA→luma decode, (2) the response→model contract,
and (3) the full path: real compiled driver → typed client → luma grid → diff
verdict, plus the coordinate mapping that ties global logical points to actual
pixels (ADR-2: pixels as verifier).
"""

from __future__ import annotations

import base64

import pytest

from computeruse.orchestrator.client import ActuationClient
from computeruse.vision import (
    ChangeKind,
    ScreenCapture,
    to_luma_grid,
    verify_capture_region,
)
from computeruse.vision.capture import crop_capture
from computeruse.vision.coordinates import (
    DisplayGeometry,
    Point,
    Rect,
    Size,
    point_to_screenshot_offset,
)
from tests.smoke.conftest import SOCKET_PATH, rpc_call

# --- Pure decode -------------------------------------------------------------


def test_crop_capture_slices_raw_bytes_before_decode() -> None:
    """M4: cropping raw BGRA bytes yields a standalone capture of the region.

    Each pixel is a distinct 4-byte value (x + y*width indices), so a correct
    crop can be asserted byte-for-byte.
    """
    width, height = 4, 2
    capture = ScreenCapture(
        display_id=0,
        width=width,
        height=height,
        scale=2.0,
        data=bytes(range(width * height * 4)),
    )
    cropped = crop_capture(capture, Rect(Point(1, 0), Size(2, 2)))
    assert (cropped.width, cropped.height) == (2, 2)
    assert cropped.scale == 2.0
    # Row 0 (base 0): pixels x=1, x=2 -> bytes 4..12; row 1 (base 16): x=1, x=2.
    assert cropped.data == bytes(range(4, 12)) + bytes(range(20, 28))


def test_crop_capture_out_of_bounds_clamps_like_grid_crop() -> None:
    """M4: out-of-bounds edges clamp; a fully-outside region is empty, so the
    diff reads "unchanged" exactly as the old decode-then-crop path did."""
    capture = ScreenCapture(
        display_id=0, width=8, height=8, scale=1.0, data=bytes(8 * 8 * 4)
    )
    empty = crop_capture(capture, Rect(Point(100, 100), Size(4, 4)))
    assert (empty.width, empty.height) == (0, 0)
    assert empty.data == b""
    partial = crop_capture(capture, Rect(Point(6, 6), Size(10, 10)))
    assert (partial.width, partial.height) == (2, 2)


def test_to_luma_grid_reads_bgra_not_rgba() -> None:
    """Byte order matters: prove the decoder reads BGRA by using an R-only pixel.

    An R-only pixel (BGRA bytes ``[0, 0, 255, 255]``) must yield Rec.709 red
    luminance (0.2126). If the decoder wrongly read RGBA it would see blue and
    return 0.0722.
    """
    capture = ScreenCapture(
        display_id=0,
        width=2,
        height=1,
        scale=2.0,
        data=bytes([0, 0, 255, 255, 255, 255, 255, 255]),  # red, white
    )
    grid = to_luma_grid(capture)
    assert len(grid) == 1
    assert len(grid[0]) == 2
    assert grid[0][0] == pytest.approx(0.2126)
    assert grid[0][1] == pytest.approx(1.0)


def test_to_luma_grid_ignores_alpha() -> None:
    """Alpha must not leak into luminance (premultiplied BGRA has alpha set)."""
    capture = ScreenCapture(
        display_id=0,
        width=1,
        height=1,
        scale=1.0,
        data=bytes([0, 0, 255, 0]),  # red with zero alpha
    )
    assert to_luma_grid(capture)[0][0] == pytest.approx(0.2126)


def test_from_response_round_trip() -> None:
    payload = bytes([0, 0, 255, 255, 255, 255, 255, 255])
    raw = {
        "ok": "screenshot",
        "display_id": 0,
        "format": "bgra8",
        "width": 2,
        "height": 1,
        "scale": 2.0,
        "data_base64": base64.b64encode(payload).decode("ascii"),
    }
    capture = ScreenCapture.from_response(raw)
    assert capture.width == 2
    assert capture.height == 1
    assert capture.scale == 2.0
    assert capture.data == payload
    assert to_luma_grid(capture)[0][0] == pytest.approx(0.2126)


def test_from_response_rejects_non_screenshot() -> None:
    with pytest.raises(ValueError, match="expected a screenshot response"):
        ScreenCapture.from_response({"ok": "ack"})


def test_from_response_rejects_truncated_payload() -> None:
    """A frame whose byte count disagrees with its dimensions is corrupt."""
    raw = {
        "ok": "screenshot",
        "display_id": 0,
        "format": "bgra8",
        "width": 4,
        "height": 4,  # needs 64 bytes
        "scale": 1.0,
        "data_base64": base64.b64encode(b"\x00" * 63).decode("ascii"),
    }
    with pytest.raises(ValueError, match="expected 64"):
        ScreenCapture.from_response(raw)


# --- End-to-end through the real compiled driver -----------------------------


def test_screenshot_protocol_shape() -> None:
    """The wire contract: a screenshot response reports its own geometry."""
    payload = rpc_call({"method": "screenshot", "params": {"display_id": 0}})
    assert payload.get("ok") == "screenshot"
    assert payload.get("format") == "bgra8"
    assert isinstance(payload.get("data_base64"), str)


def test_typed_capture_round_trip_via_client() -> None:
    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        capture = client.capture()
    assert capture.pixel_format == "bgra8"
    assert capture.scale == 1.0
    assert (capture.width, capture.height) == (1024, 768)
    # Decode a corner rather than the whole frame: ``to_luma_grid`` is a pure
    # Python double loop, and a full 1024x768 decode costs seconds for no
    # extra signal. Cropping first is also how the ORIENT path itself works.
    grid = to_luma_grid(crop_capture(capture, Rect(Point(0, 0), Size(16, 16))))
    assert len(grid) == 16
    assert all(len(row) == 16 for row in grid)


def test_simulated_checkerboard_matches_expected_luma() -> None:
    """The simulated backend's frame is a deterministic 8×8 checkerboard."""
    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        capture = client.capture()
    grid = to_luma_grid(crop_capture(capture, Rect(Point(0, 0), Size(16, 16))))
    top_left = Rect(Point(0, 0), Size(8, 8))
    second = Rect(Point(8, 0), Size(8, 8))
    assert all(
        grid[y][x] == pytest.approx(1.0)
        for y in range(int(top_left.origin.y), 8)
        for x in range(int(top_left.origin.x), 8)
    )
    assert all(
        grid[y][x] == pytest.approx(0.0)
        for y in range(int(second.origin.y), 8)
        for x in range(int(second.origin.x), 8)
    )


def test_global_point_maps_to_expected_pixel() -> None:
    """ADR-2 end to end: logical point → display px → screenshot offset → luma.

    The simulated frame has scale 2.0 and a 32×18pt display, so global point
    (20, 4) lands at pixel (40, 8) — checkerboard block (5, 1) → white → 1.0.
    """
    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        capture = client.capture()
    geometry = DisplayGeometry(
        display_id=0,
        frame=Rect(Point(0, 0), Size(32, 18)),
        scale=2.0,
    )
    px = point_to_screenshot_offset(
        Point(20, 4), geometry, Size(capture.width, capture.height)
    )
    grid = to_luma_grid(capture)
    assert grid[int(px.y)][int(px.x)] == pytest.approx(1.0)


def test_identical_captures_verify_unchanged() -> None:
    """OBSERVE twice without any action → ORIENT must say nothing changed."""
    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        first = client.capture()
        second = client.capture()
    assert first.data == second.data  # deterministic sensor
    region = Rect(Point(0, 0), Size(16, 16))
    verification = verify_capture_region(first, second, region)
    assert verification.verdict.kind is ChangeKind.UNCHANGED


def test_edited_region_verifies_changed_and_neighbour_stable() -> None:
    """A real change in one block is detected; an untouched block is not."""
    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        before = client.capture()
        after = client.capture()

    # Flip the top-left 16×16 block to black in the "after" frame. The
    # simulated display is 1:1 (scale 1.0), so a 20×20 point region contains
    # 400 pixels of which 128 (the two white checker blocks inside the flip)
    # change: 32% — comfortably over verdict()'s 15% change threshold and
    # under its 50% "wholesale takeover" noise cap, so this reads as a real,
    # localized edit rather than a whole-region replacement.
    mutated = bytearray(after.data)
    for y in range(16):
        base = y * after.width * 4
        mutated[base : base + 16 * 4] = b"\x00\x00\x00\xff" * 16
    after = ScreenCapture(
        display_id=after.display_id,
        width=after.width,
        height=after.height,
        scale=after.scale,
        data=bytes(mutated),
    )

    # Region covering the flipped block plus untouched blocks: CHANGED.
    changed = verify_capture_region(
        before, after, Rect(Point(0, 0), Size(20, 20))
    )
    assert changed.verdict.kind is ChangeKind.CHANGED
    assert changed.changed

    # A region entirely outside the edit: still UNCHANGED.
    stable = verify_capture_region(
        before, after, Rect(Point(20, 0), Size(20, 20))
    )
    assert stable.verdict.kind is ChangeKind.UNCHANGED


def test_to_logical_resolution_halves_retina_frame() -> None:
    """A 2x Retina frame downscales to logical points with scale 1.0."""
    from computeruse.vision.capture import to_logical_resolution

    # 4x4 physical px (2x2 logical points), solid red.
    capture = ScreenCapture(
        display_id=0,
        width=4,
        height=4,
        scale=2.0,
        data=bytes([0, 0, 255, 255] * 16),
    )
    down = to_logical_resolution(capture)
    assert down.width == 2
    assert down.height == 2
    assert down.scale == 1.0
    assert len(down.data) == 2 * 2 * 4
    # Box-averaged block is still solid red.
    assert down.data[0:4] == bytes([0, 0, 255, 255])


def test_to_logical_resolution_averages_block_content() -> None:
    """Box sampling must blend the block, not take one corner sample."""
    from computeruse.vision.capture import to_logical_resolution

    # 2x2 px block: two black, two white pixels -> averaged grey (128).
    data = bytes([0, 0, 0, 255, 255, 255, 255, 255, 255, 255, 255, 255, 0, 0, 0, 255])
    capture = ScreenCapture(display_id=0, width=2, height=2, scale=2.0, data=data)
    down = to_logical_resolution(capture)
    assert down.width == 1
    assert down.height == 1
    # 510 / 4 = 127 with integer (truncating) division.
    assert down.data[0] == 127  # blue channel average
    assert down.data[2] == 127  # red channel average


def test_to_logical_resolution_passthrough_at_scale_one() -> None:
    """A non-Retina frame must pass through untouched (identity)."""
    from computeruse.vision.capture import to_logical_resolution

    capture = ScreenCapture(
        display_id=0,
        width=3,
        height=2,
        scale=1.0,
        data=bytes(3 * 2 * 4),
    )
    assert to_logical_resolution(capture) is capture


def test_downscale_to_max_side_creates_the_vlm_map() -> None:
    """The screenshot map matches what OpenAI `detail: low` will show (512px)."""
    from computeruse.vision.capture import downscale_to_max_side

    # 1024x600 -> 512x300 (longest side capped, aspect preserved, scale 1.0).
    width, height = 1024, 600
    capture = ScreenCapture(
        display_id=0, width=width, height=height, scale=1.0, data=bytes(width * height * 4)
    )
    mapped = downscale_to_max_side(capture)
    assert (mapped.width, mapped.height) == (512, 300)
    assert mapped.scale == 1.0
    # The coordinate gate's factor: screen points per image pixel.
    assert mapped.width * 2.0 == width


def test_downscale_to_max_side_sampled_content_stays_grounded() -> None:
    """Nearest-neighbour sampling keeps a painted region's position exact."""
    from computeruse.vision.capture import downscale_to_max_side

    width, height = 1024, 600
    buf = bytearray(width * height * 4)
    # Paint a 200x100 white block at logical (300, 200) -> image (150, 100).
    for y in range(200, 300):
        base = (y * width + 300) * 4
        buf[base : base + 200 * 4] = b"\xff\xff\xff\xff" * 200
    capture = ScreenCapture(display_id=0, width=width, height=height, scale=1.0, data=bytes(buf))
    mapped = downscale_to_max_side(capture)
    # The block's top-left lands at the mapped pixel (150, 100) and is white.
    i = (100 * mapped.width + 150) * 4
    assert mapped.data[i : i + 4] == b"\xff\xff\xff\xff"


def test_downscale_to_max_side_passthrough_when_small() -> None:
    """A display already within 512px is not resampled (factor stays 1.0)."""
    from computeruse.vision.capture import downscale_to_max_side

    capture = ScreenCapture(
        display_id=0,
        width=480,
        height=320,
        scale=1.0,
        data=bytes(480 * 320 * 4),
    )
    assert downscale_to_max_side(capture) is capture


def test_capture_to_png_encodes_valid_png() -> None:
    from computeruse.vision.capture import capture_to_base64_png, capture_to_png

    capture = ScreenCapture(
        display_id=0,
        width=4,
        height=4,
        scale=1.0,
        data=bytes([255, 0, 0, 255] * 16),
    )
    png_bytes = capture_to_png(capture)
    # Check PNG signature
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    # Check IEND chunk
    assert png_bytes.endswith(b"IEND\xaeB`\x82")

    b64_str = capture_to_base64_png(capture)
    assert isinstance(b64_str, str)
    assert len(b64_str) > 0
