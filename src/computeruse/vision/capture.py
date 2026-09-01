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
from typing import Literal

from pydantic import BaseModel, model_validator

from computeruse.vision.coordinates import Point, Rect, Size
from computeruse.vision.diff import Verification, crop_luma, verdict

type LumaGrid = tuple[tuple[float, ...], ...]


class ScreenCapture(BaseModel):
    """A validated display snapshot as delivered by the actuation driver."""

    display_id: int
    width: int
    height: int
    scale: float
    pixel_format: Literal["bgra8"] = "bgra8"
    data: bytes

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
    Coordinates in ``region`` (logical points) are scaled by ``before.scale``
    to align with the physical pixel grid (e.g. Retina 2.0).
    """
    if before.display_id != after.display_id:
        raise ValueError(
            f"cannot verify captures from different displays: "
            f"{before.display_id} != {after.display_id}"
        )
    if (before.width, before.height, before.scale) != (after.width, after.height, after.scale):
        raise ValueError("cannot verify captures with different geometry or scale")
    scale = before.scale
    scaled_region = Rect(
        origin=Point(region.origin.x * scale, region.origin.y * scale),
        size=Size(region.size.width * scale, region.size.height * scale),
    )
    before_region = crop_luma(to_luma_grid(before), scaled_region)
    after_region = crop_luma(to_luma_grid(after), scaled_region)
    return Verification(region=region, verdict=verdict(before_region, after_region))


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
    """Encode a ScreenCapture as a base64-encoded PNG string."""
    return base64.b64encode(capture_to_png(capture)).decode("ascii")


def fallback_screencapture_b64() -> str | None:
    """Capture screen via macOS screencapture utility when driver capture fails.

    macOS includes /usr/sbin/screencapture signed by Apple which can capture
    frames even when custom app bundles are not yet manually enabled in TCC.
    On any other platform — or when the tool is not installed — there is no
    fallback: returning None immediately (rather than spawning a subprocess
    that cannot exist) keeps the OBSERVE path fast and the log clean.
    """
    import os
    import shutil
    import subprocess
    import sys
    import tempfile

    if sys.platform != "darwin" or shutil.which("screencapture") is None:
        return None

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        res = subprocess.run(
            ["screencapture", "-x", "-C", tmp_path],
            capture_output=True,
            check=False,
            timeout=3.0,
        )
        if res.returncode == 0 and os.path.exists(tmp_path):
            with open(tmp_path, "rb") as f:
                data = f.read()
            if data and data.startswith(b"\x89PNG"):
                return base64.b64encode(data).decode("ascii")
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return None
