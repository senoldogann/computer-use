"""Visual confirmation crops, end to end through the real driver.

The CUA REPL's ``cropScreenshot`` used to be a stub: it echoed the bounds it
was handed back with ``"format": "png"`` and never touched a pixel. Nothing
tested it, so nothing said so — and the feature it was the foundation for
(showing a model a control instead of a display) could not have worked.

These run against the compiled driver's simulated backend, which serves a
deterministic 1024x768 checkerboard at scale 1.0. That is enough to pin the
three things that matter and were all wrong or absent:

* the answer is a real PNG, not raw bytes wearing a PNG label,
* the region is the one that was asked for, padding and Retina scale included,
* it is drastically smaller than the frame it came from.
"""

from __future__ import annotations

import base64
import struct

import pytest

from computeruse.orchestrator.client import ActuationClient
from computeruse.repl.engine import CuaReplEngine, ScreenCaptureUnavailableError
from computeruse.vision.coordinates import Point, Rect, Size
from tests.smoke.conftest import SOCKET_PATH

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
DATA_URI_PREFIX = "data:image/png;base64,"


def decode_png_size(data_uri: str) -> tuple[int, int]:
    """Width and height read out of a PNG data URI's IHDR header."""
    assert data_uri.startswith(DATA_URI_PREFIX), data_uri[:40]
    raw = base64.b64decode(data_uri[len(DATA_URI_PREFIX) :])
    assert raw[:8] == PNG_MAGIC, (
        "the data URI says PNG but carries something else. getScreenshot used "
        "to base64 the driver's raw BGRA buffer straight into a PNG-labelled "
        "URI, which no decoder accepts."
    )
    width, height = struct.unpack(">II", raw[16:24])
    return width, height


def test_crop_returns_a_real_png_of_the_requested_region() -> None:
    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        engine = CuaReplEngine(driver_client=client)
        crop = engine._dispatch_js_call(  # pyright: ignore[reportPrivateUsage]
            "cropScreenshot",
            {"app": "Safari", "bounds": {"x": 10, "y": 20, "width": 64, "height": 32}},
        )
    assert isinstance(crop, str)
    # The simulated frame is scale 1.0, so logical points and pixels coincide
    # and the crop must be exactly the requested rectangle.
    assert decode_png_size(crop) == (64, 32)


def test_crop_padding_widens_the_region_on_every_side() -> None:
    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        engine = CuaReplEngine(driver_client=client)
        padded = engine._dispatch_js_call(  # pyright: ignore[reportPrivateUsage]
            "cropScreenshot",
            {
                "app": "Safari",
                "bounds": {"x": 100, "y": 100, "width": 40, "height": 20},
                "padding": 8,
            },
        )
    assert isinstance(padded, str)
    # 8 points on each side of a 40x20 control, at scale 1.0.
    assert decode_png_size(padded) == (40 + 16, 20 + 16)


def test_crop_is_orders_of_magnitude_smaller_than_the_frame() -> None:
    """The whole point: a control costs a fraction of what a display costs."""
    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        engine = CuaReplEngine(driver_client=client)
        full = engine._dispatch_js_call("getScreenshot", {})  # pyright: ignore[reportPrivateUsage]
        crop = engine._dispatch_js_call(  # pyright: ignore[reportPrivateUsage]
            "cropScreenshot",
            {"app": "Safari", "bounds": {"x": 0, "y": 0, "width": 48, "height": 24}},
        )
    assert isinstance(full, str) and isinstance(crop, str)
    assert decode_png_size(full) == (1024, 768)
    assert len(crop) * 20 < len(full), (
        f"crop is {len(crop)} chars against a {len(full)}-char frame; the "
        "saving that justifies this path is not there"
    )


def test_crop_outside_the_display_is_refused_not_answered_blank() -> None:
    """An off-screen region is an error, never an empty image.

    The same rule as a failed capture: an image the model cannot learn
    anything from must not be handed over as though it could.
    """
    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        engine = CuaReplEngine(driver_client=client)
        with pytest.raises(ScreenCaptureUnavailableError, match="does not overlap"):
            engine._dispatch_js_call(  # pyright: ignore[reportPrivateUsage]
                "cropScreenshot",
                {
                    "app": "Safari",
                    "bounds": {"x": 9000, "y": 9000, "width": 20, "height": 20},
                },
            )


def test_zero_sized_crop_is_rejected() -> None:
    """A region with no area cannot be a picture of anything."""
    with ActuationClient(str(SOCKET_PATH), connect_retries=1) as client:
        engine = CuaReplEngine(driver_client=client)
        with pytest.raises(ValueError, match="positive size"):
            engine._crop_data_uri(  # pyright: ignore[reportPrivateUsage]
                Rect(origin=Point(0, 0), size=Size(0, 10)), padding=0
            )
