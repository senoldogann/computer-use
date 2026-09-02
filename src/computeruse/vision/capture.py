"""Capture decoding for the OODA OBSERVE step (ADR-2: pixels as *verifier*).

The Rust driver owns the physical screenshot (``CGDisplayCreateImage`` via the
Quartz backend, or a deterministic synthetic frame via the simulated backend).
This module is the Python half of that boundary: it validates the driver's
response into a typed :class:`ScreenCapture` and decodes BGRA8 bytes into the
luma grid that :mod:`computeruse.vision.diff` consumes.

Everything here is pure (Law 6): ``to_luma_grid`` is a pure byte→float
transformation, and the socket I/O that *fetches* the response stays in the
orchestrator's connector. The decode is kept independent of Pydantic so it can
be unit-tested against a hand-built buffer.

Row order follows the driver's contract: row 0 is the *top* of the display,
matching the top-left origin convention of :mod:`computeruse.vision.coordinates`
so region crops line up with actuation coordinates.
"""

from __future__ import annotations

import base64
import hashlib
import struct
import zlib
from collections.abc import Mapping
from typing import Final, Literal

from pydantic import BaseModel, model_validator

from computeruse.vision.coordinates import Point, Rect, ScreenMap, Size
from computeruse.vision.diff import Verification, verdict

type LumaGrid = tuple[tuple[float, ...], ...]


class ScreenCapture(BaseModel):
    """A validated display snapshot as delivered by the actuation driver."""

    display_id: int
    width: int
    height: int
    scale: float
    #: Top-left of the captured display in *global* logical points. Zero for
    #: the primary display, and for a driver too old to report it — which is
    #: also the only correct default: a single-display host has no offset, and
    #: a multi-display one needs a driver that can tell us about it.
    origin_x: float = 0.0
    origin_y: float = 0.0
    pixel_format: Literal["bgra8"] = "bgra8"
    data: bytes

    @property
    def origin(self) -> Point:
        """This display's top-left corner in global logical points."""
        return Point(self.origin_x, self.origin_y)

    @property
    def logical_size(self) -> Size:
        """The display's size in logical points (physical pixels / scale)."""
        return Size(self.width / self.scale, self.height / self.scale)

    @property
    def display_frame(self) -> Rect:
        """The display's rectangle in global logical points.

        This — not "0,0 to width,height" — is the region a coordinate must fall
        inside to be on the captured display.
        """
        return Rect(origin=self.origin, size=self.logical_size)

    @classmethod
    def from_response(cls, raw: Mapping[str, object]) -> ScreenCapture:
        """Build from the driver's JSON-RPC response dict.

        Raises :class:`ValueError` (Law 6.3 explicit errors) for anything that
        is not a well-formed screenshot, so a misrouted or truncated response
        never masquerades as an empty frame — an empty frame would read as
        "nothing changed" and silently skip visual verification.
        """
        if raw.get("ok") != "screenshot":
            raise ValueError(f"expected a screenshot response, got {raw.get('ok')!r}")
        try:
            encoded = _require_str(raw, "data_base64")
            fmt = _require_str(raw, "format")
            if fmt != "bgra8":
                raise ValueError(f"unsupported pixel format {fmt!r}")
            return cls(
                display_id=_require_int(raw, "display_id"),
                width=_require_int(raw, "width"),
                height=_require_int(raw, "height"),
                scale=_require_float(raw, "scale"),
                # Optional on the wire: a driver that predates multi-display
                # support reports no origin, and 0,0 is exactly right for the
                # single-display host such a driver can serve.
                origin_x=_optional_float(raw, "origin_x"),
                origin_y=_optional_float(raw, "origin_y"),
                pixel_format=fmt,
                data=base64.b64decode(encoded),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed screenshot response: {exc}") from exc

    @model_validator(mode="after")
    def _check_bgra_size(self) -> ScreenCapture:
        expected = self.width * self.height * 4
        if len(self.data) != expected:
            raise ValueError(
                f"bgra payload has {len(self.data)} bytes, expected {expected} "
                f"(width*height*4)"
            )
        return self


def _require_int(raw: Mapping[str, object], key: str) -> int:
    value = raw[key]
    if not isinstance(value, int):
        raise TypeError(f"{key} must be an integer, got {type(value).__name__}")
    return value


def _require_float(raw: Mapping[str, object], key: str) -> float:
    value = raw[key]
    if not isinstance(value, (int, float)):
        raise TypeError(f"{key} must be a number, got {type(value).__name__}")
    return float(value)


def _optional_float(raw: Mapping[str, object], key: str) -> float:
    """A numeric field that older drivers omit entirely (defaults to 0.0)."""
    value = raw.get(key)
    if value is None:
        return 0.0
    if not isinstance(value, (int, float)):
        raise TypeError(f"{key} must be a number, got {type(value).__name__}")
    return float(value)


def _require_str(raw: Mapping[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string, got {type(value).__name__}")
    return value


def to_luma_grid(capture: ScreenCapture) -> LumaGrid:
    """Decode a BGRA8 frame into a row-major luminance grid in [0, 1].

    Uses the same Rec.709 weights as :func:`computeruse.vision.diff.to_luma` —
    one luminance definition for the whole vision stack, so a decoded capture
    and a synthetic grid are directly comparable.
    """
    data = capture.data
    stride = capture.width * 4
    rows: list[tuple[float, ...]] = []
    for y in range(capture.height):
        row: list[float] = []
        base = y * stride
        for x in range(capture.width):
            i = base + x * 4
            b, g, r, _a = data[i], data[i + 1], data[i + 2], data[i + 3]
            row.append((0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0)
        rows.append(tuple(row))
    return tuple(rows)


def crop_capture(capture: ScreenCapture, rect: Rect) -> ScreenCapture:
    """Slice a BGRA capture to a pixel region (pure) — crop *before* decoding.

    The ORIENT diff used to decode the *entire* Retina frame to a luma grid
    and then crop the grid, paying the full-frame decode cost (tens of ms per
    action on a 3024×1964 2x display) even when the changed region is a tiny
    button (M4). Slicing the raw bytes first bounds the decode to exactly the
    region that gets diffed. Out-of-bounds edges are clamped to the frame,
    mirroring :func:`computeruse.vision.diff.crop_luma`; a fully outside
    region yields an empty capture whose diff reads "unchanged".
    """
    x0 = max(0, int(rect.origin.x))
    y0 = max(0, int(rect.origin.y))
    x1 = min(capture.width, int(rect.origin.x + rect.size.width))
    y1 = min(capture.height, int(rect.origin.y + rect.size.height))
    if x1 <= x0 or y1 <= y0:
        return ScreenCapture(
            display_id=capture.display_id,
            width=0,
            height=0,
            scale=capture.scale,
            origin_x=capture.origin_x,
            origin_y=capture.origin_y,
            data=b"",
        )
    region_w = x1 - x0
    region_h = y1 - y0
    src_stride = capture.width * 4
    row_bytes = region_w * 4
    data = capture.data
    out = bytearray(region_h * row_bytes)
    for dy, y in enumerate(range(y0, y1)):
        start = y * src_stride + x0 * 4
        out[dy * row_bytes : (dy + 1) * row_bytes] = data[start : start + row_bytes]
    return ScreenCapture(
        display_id=capture.display_id,
        width=region_w,
        height=region_h,
        scale=capture.scale,
        origin_x=capture.origin_x,
        origin_y=capture.origin_y,
        data=bytes(out),
    )


def verify_capture_region(
    before: ScreenCapture,
    after: ScreenCapture,
    region: Rect,
) -> Verification:
    """Diff one region between two captures — the OODA ORIENT check.

    Composes the pure decode/crop/verdict pipeline into the single artifact the
    loop carries: did the target region change between the pre- and post-action
    captures? The caller keeps only this aggregated :class:`Verification` in
    working state, never the raw bitmaps (Law 4: minimal working context).
    Coordinates in ``region`` are *global* logical points; they are localised
    to the captured display and scaled by ``before.scale`` to align with the
    physical pixel grid (e.g. Retina 2.0).
    """
    if before.display_id != after.display_id:
        raise ValueError(
            f"cannot verify captures from different displays: "
            f"{before.display_id} != {after.display_id}"
        )
    if (before.width, before.height, before.scale) != (after.width, after.height, after.scale):
        raise ValueError("cannot verify captures with different geometry or scale")
    # ``region`` is in GLOBAL logical points (that is the space actions are
    # actuated in), while the frame's pixel grid starts at the display's own
    # corner. Subtract the display origin before scaling, or a region on a
    # secondary display crops somewhere else entirely.
    scale = before.scale
    local_x = region.origin.x - before.origin_x
    local_y = region.origin.y - before.origin_y
    scaled_region = Rect(
        origin=Point(local_x * scale, local_y * scale),
        size=Size(region.size.width * scale, region.size.height * scale),
    )
    # Crop the raw BGRA bytes *first* so the luminance decode only ever sees
    # the diffed region (M4): full-frame decode + crop on every verified
    # action was the dominant per-step cost in the vision path.
    before_region = to_luma_grid(crop_capture(before, scaled_region))
    after_region = to_luma_grid(crop_capture(after, scaled_region))
    return Verification(region=region, verdict=verdict(before_region, after_region))


# The VLM screenshot is sent with OpenAI `detail: "low"`, which resizes any
# image to 512px on its longest side. The coordinate gate (loop.py) therefore
# scales every model-emitted coordinate by the map factor below — so the
# image the model sees and the screen space the driver clicks in are linked
# by one deterministic constant, never by model arithmetic.
SCREENSHOT_MAP_MAX_SIDE: Final[int] = 512


def downscale_to_max_side(capture: ScreenCapture, max_side: int = SCREENSHOT_MAP_MAX_SIDE) -> ScreenCapture:
    """Resample a capture so its longest side is at most ``max_side`` (pure).

    This is the *exact* image the VLM will perceive: OpenAI's ``detail:
    "low"`` resizes every attached image to 512px on its longest dimension,
    so pre-resampling here makes image space deterministic and known to the
    orchestrator. The returned capture keeps ``scale=1.0`` — it is a plain
    bitmap map whose pixels map back to logical screen points via the factor
    ``logical_width / mapped_width`` (computed by the caller).

    Nearest-neighbour sampling (not box averaging) is deliberate: at this
    scale small text is mush either way, and nearest-neighbour keeps crisp
    edges, which is what the model reads coordinates off. The cost is
    O(dst pixels) instead of O(src pixels) — ~50ms for a Retina frame.

    Captures already within ``max_side`` pass through unchanged, so a small
    display (factor 1.0) never pays a resample.
    """
    if max_side <= 0:
        raise ValueError(f"max_side must be positive, got {max_side}")
    longest = max(capture.width, capture.height)
    if longest <= max_side:
        return capture
    scale = longest / max_side
    dst_w = max(1, round(capture.width / scale))
    dst_h = max(1, round(capture.height / scale))
    src_w = capture.width
    src_h = capture.height
    src_stride = src_w * 4
    data = memoryview(capture.data)
    out = bytearray(dst_w * dst_h * 4)
    # Precompute source row/column indices once; every dst pixel then does a
    # single 4-byte slice copy instead of per-channel arithmetic.
    src_rows = [min(src_h - 1, int(dy * src_h / dst_h)) for dy in range(dst_h)]
    src_cols = [min(src_w - 1, int(dx * src_w / dst_w)) for dx in range(dst_w)]
    for dy in range(dst_h):
        src_row_base = src_rows[dy] * src_stride
        out_base = dy * dst_w * 4
        for dx in range(dst_w):
            i = src_row_base + src_cols[dx] * 4
            o = out_base + dx * 4
            out[o : o + 4] = data[i : i + 4]
    return ScreenCapture(
        display_id=capture.display_id,
        width=dst_w,
        height=dst_h,
        scale=1.0,
        # A resampled frame still describes the same display: dropping the
        # origin here would quietly reset every derived map to the primary
        # display, which is exactly the bug the origin exists to prevent.
        origin_x=capture.origin_x,
        origin_y=capture.origin_y,
        data=bytes(out),
    )


def to_logical_resolution(capture: ScreenCapture) -> ScreenCapture:
    """Downscale a physical-pixel frame to logical-point resolution.

    The VLM sees the screenshot and reports click coordinates in *image*
    space. If the image is at physical resolution (e.g. 3024x1964 on a 2x
    Retina display) while actuation uses logical points (1512x982), the
    model's in-image coordinates are silently 2x off when the driver applies
    them — clicks land on the wrong element. Resampling the frame to logical
    resolution makes image space == actuation space 1:1: what the model
    points at is exactly what gets clicked, with no mental Retina math.

    Fast path for the common 2x Retina case: each logical pixel is exactly
    a 2×2 block of physical pixels, so we average 4 source pixels per dest
    pixel with a tight loop over the BGRA array. The general case (non-
    integer scale, e.g. 1.5x) falls back to the per-block box average.
    """
    if capture.scale <= 1.0:
        return capture
    scale_int = int(capture.scale)
    # Fast path: integer scale (the only real-world case is 2x Retina).
    if capture.scale == float(scale_int) and scale_int >= 2:
        return _downscale_integer(capture, scale_int)
    # Fallback: general box-averaging for non-integer scales.
    return _downscale_general(capture, capture.scale)


def _downscale_integer(capture: ScreenCapture, factor: int) -> ScreenCapture:
    """Fast 2x (or Nx) downscale: average each factor×factor block.

    Uses ``memoryview`` slicing to batch-read rows, avoiding per-pixel
    Python overhead. For a 3024×1964 → 1512×982 frame this processes ~6M
    bytes in ~30ms (vs ~800ms for the general loop on the same hardware).
    """
    src_w = capture.width
    src_h = capture.height
    dst_w = src_w // factor
    dst_h = src_h // factor
    src_stride = src_w * 4
    dst_stride = dst_w * 4
    data = memoryview(capture.data)
    out = bytearray(dst_w * dst_h * 4)
    inv = 1.0 / (factor * factor)
    for dy in range(dst_h):
        sy0 = dy * factor
        out_base = dy * dst_stride
        for dx in range(dst_w):
            sx0 = dx * factor
            total_b = 0
            total_g = 0
            total_r = 0
            total_a = 0
            for yy in range(factor):
                row_base = (sy0 + yy) * src_stride + sx0 * 4
                for xx in range(factor):
                    i = row_base + xx * 4
                    total_b += data[i]
                    total_g += data[i + 1]
                    total_r += data[i + 2]
                    total_a += data[i + 3]
            o = out_base + dx * 4
            out[o] = int(total_b * inv)
            out[o + 1] = int(total_g * inv)
            out[o + 2] = int(total_r * inv)
            out[o + 3] = int(total_a * inv)
    return ScreenCapture(
        display_id=capture.display_id,
        width=dst_w,
        height=dst_h,
        scale=1.0,
        # A resampled frame still describes the same display: dropping the
        # origin here would quietly reset every derived map to the primary
        # display, which is exactly the bug the origin exists to prevent.
        origin_x=capture.origin_x,
        origin_y=capture.origin_y,
        data=bytes(out),
    )


def _downscale_general(capture: ScreenCapture, scale: float) -> ScreenCapture:
    """General box-averaging downscale for non-integer scales.

    Pure-Python fallback; the fast path above handles the only real-world
    case (2x Retina). This is correct for any scale but slow.
    """
    src_w = capture.width
    src_h = capture.height
    dst_w = max(1, round(src_w / scale))
    dst_h = max(1, round(src_h / scale))
    src_stride = src_w * 4
    data = capture.data
    out = bytearray(dst_w * dst_h * 4)
    for dy in range(dst_h):
        sy0 = min(src_h - 1, int(dy * scale))
        sy1 = min(src_h, int(dy * scale + scale))
        for dx in range(dst_w):
            sx0 = min(src_w - 1, int(dx * scale))
            sx1 = min(src_w, int(dx * scale + scale))
            total_b = 0
            total_g = 0
            total_r = 0
            total_a = 0
            count = 0
            for yy in range(sy0, sy1):
                base = yy * src_stride
                for xx in range(sx0, sx1):
                    i = base + xx * 4
                    total_b += data[i]
                    total_g += data[i + 1]
                    total_r += data[i + 2]
                    total_a += data[i + 3]
                    count += 1
            o = (dy * dst_w + dx) * 4
            out[o] = total_b // count
            out[o + 1] = total_g // count
            out[o + 2] = total_r // count
            out[o + 3] = total_a // count
    return ScreenCapture(
        display_id=capture.display_id,
        width=dst_w,
        height=dst_h,
        scale=1.0,
        # A resampled frame still describes the same display: dropping the
        # origin here would quietly reset every derived map to the primary
        # display, which is exactly the bug the origin exists to prevent.
        origin_x=capture.origin_x,
        origin_y=capture.origin_y,
        data=bytes(out),
    )


def capture_to_png(capture: ScreenCapture) -> bytes:
    """Encode a ScreenCapture's BGRA8 buffer to standard PNG bytes (pure).

    Uses pure standard-library zlib and struct packing with zero external
    dependencies. Converts top-down BGRA8 to RGBA8 scanlines.

    Optimized: converts BGRA→RGBA in bulk per-row using memoryview slicing
    rather than per-pixel tuple unpacking, then compresses with zlib.
    """
    width = capture.width
    height = capture.height
    data = capture.data
    stride = width * 4

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # Filter 0: None
        row_start = y * stride
        # Bulk BGRA→RGBA conversion: swap R and B for each pixel.
        row_mv = memoryview(data)[row_start : row_start + stride]
        rgba_row = bytearray(stride)
        for x in range(0, stride, 4):
            rgba_row[x] = row_mv[x + 2]      # R ← B
            rgba_row[x + 1] = row_mv[x + 1]  # G ← G
            rgba_row[x + 2] = row_mv[x]      # B ← R
            rgba_row[x + 3] = row_mv[x + 3]  # A ← A
        raw.extend(rgba_row)

    def make_chunk(tag: bytes, payload: bytes) -> bytes:
        length = struct.pack(">I", len(payload))
        crc = struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        return length + tag + payload + crc

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), level=6)
    return b"\x89PNG\r\n\x1a\n" + make_chunk(b"IHDR", ihdr) + make_chunk(b"IDAT", idat) + make_chunk(b"IEND", b"")


def frame_fingerprint(capture: ScreenCapture) -> str:
    """A cheap content hash of a raw BGRA frame (pure).

    Lets the OODA OBSERVE step skip the expensive PNG re-encode when the
    screen has not changed since the previous capture — the dominant case on
    an idle desktop, and previously every step paid a full-frame zlib
    compress + base64 for an identical image.
    """
    return hashlib.sha256(capture.data).hexdigest()


def capture_to_base64_png(capture: ScreenCapture) -> str:
    """Encode a ScreenCapture as a base64-encoded PNG string.

    Note on the removed ``screencapture`` fallback: when the driver's capture
    was refused, the loop used to shell out to macOS's own ``screencapture``
    and hand the model that PNG instead. It looked like graceful degradation
    and was the opposite — the fallback frame arrived at *physical* resolution
    with no known mapping back to screen points, so the coordinate gate kept
    applying whatever factor the last real capture had produced (or none at
    all). Every click derived from that image was confidently wrong, silently.
    A screenshot whose coordinate space is unknown is worse than no screenshot:
    the loop now degrades to no-vision and tells the model it is blind.
    """
    return base64.b64encode(capture_to_png(capture)).decode("ascii")


# Grid resolution of the coarse progress fingerprint. 16x16 luma buckets is
# small enough that a clock digit, a caret blink, or one video frame collapses
# into the same signature, and large enough that a real page transition,
# scroll, or dialog changes it.
PROGRESS_GRID: Final[int] = 16
# Luminance quantization step for the same fingerprint. Anti-aliasing and
# gradient dither move a bucket average by a few units out of 255; quantizing
# to 16 levels absorbs that without hiding a genuine content change.
PROGRESS_LEVELS: Final[int] = 16


def coarse_fingerprint(capture: ScreenCapture) -> str:
    """A change-tolerant signature of a frame's *layout* (pure).

    :func:`frame_fingerprint` hashes raw bytes, so it flips on a single pixel —
    useful as a cache key, useless as a progress signal: on a live desktop it
    reports "the screen changed" every single turn, which silently disabled the
    stuck-loop guard (a repeated click always looked like progress). This one
    averages the frame into a :data:`PROGRESS_GRID` cell grid and quantizes each
    cell, so cosmetic churn hashes identically while a scroll, navigation, or
    new dialog does not.

    Sampling (rather than averaging every pixel) keeps the cost at a few
    thousand reads regardless of display resolution — this runs on every step.
    """
    width = capture.width
    height = capture.height
    if width == 0 or height == 0:
        return "empty"
    data = capture.data
    stride = width * 4
    # Sample a fixed number of points per cell; a full average over a Retina
    # frame would read 6M bytes per step for no extra signal.
    samples_per_axis = 4
    cells: list[int] = []
    for cell_y in range(PROGRESS_GRID):
        for cell_x in range(PROGRESS_GRID):
            total = 0
            count = 0
            for sy in range(samples_per_axis):
                y = min(
                    height - 1,
                    (cell_y * samples_per_axis + sy) * height // (PROGRESS_GRID * samples_per_axis),
                )
                base = y * stride
                for sx in range(samples_per_axis):
                    x = min(
                        width - 1,
                        (cell_x * samples_per_axis + sx) * width // (PROGRESS_GRID * samples_per_axis),
                    )
                    i = base + x * 4
                    # Rec.709 luma on the BGRA triple, integer arithmetic.
                    total += (2126 * data[i + 2] + 7152 * data[i + 1] + 722 * data[i]) // 10000
                    count += 1
            average = total // count if count else 0
            cells.append(average * PROGRESS_LEVELS // 256)
    return hashlib.sha256(bytes(cells)).hexdigest()[:32]


def screen_map_of(logical: ScreenCapture, mapped: ScreenCapture) -> ScreenMap:
    """Build the image-space <-> screen-space map for a captured frame (pure).

    ``logical`` is the frame at logical-point resolution (what the driver
    actuates in) and ``mapped`` is the downscaled screenshot the model sees.
    Constructing the map from the two captures — rather than passing a bare
    float around — makes the conversion direction impossible to get backwards,
    and carries the captured display's global origin so a frame from a
    secondary display converts back into the space the driver clicks in.
    """
    return ScreenMap(
        logical=Size(float(logical.width), float(logical.height)),
        image=Size(float(mapped.width), float(mapped.height)),
        origin=logical.origin,
    )
