"""Pure coordinate transformers for screen-space mapping (ADR-2 groundwork).

macOS mixes three coordinate spaces, and mixing them up silently drops clicks
onto the wrong pixel or wrong display:

* **Logical display space (points)** — what CGEvent/Quartz coordinates use; the
  global space has its origin at the *top-left of the primary display* and is
  measured in points, not pixels.
* **Physical pixel space (px)** — what a screen capture
  (``CGDisplayCreateImage``) returns: one pixel per physical pixel, per display.
* **Retina scaling** — a display with ``scale`` 2.0 maps each point to a 2x2
  block of pixels; other displays (or a non-Retina mirror) can differ.

This module is intentionally *pure* (Law 6): it performs no OS I/O and takes
geometry as already-known inputs, so every transformation is unit-testable
without a display. Fetching the real ``DisplayGeometry`` from macOS is the
vision connector's job, not this module's.

Origin convention: ``(0,0)`` is the top-left of the primary display, Y grows
downward — matching Quartz global display coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Point:
    """A 2D coordinate in some (caller-documented) unit space."""

    x: float
    y: float


@dataclass(frozen=True)
class Size:
    """A rectangle's dimensions."""

    width: float
    height: float


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle; origin is the top-left corner."""

    origin: Point
    size: Size


@dataclass(frozen=True)
class DisplayGeometry:
    """Static geometry of one display, needed to map coordinates.

    ``frame`` is the display's global rectangle in *logical points* (so its
    ``origin`` carries the multi-display offset); ``scale`` is physical pixels
    per logical point for that display (Retina == 2.0).
    """

    display_id: int
    frame: Rect
    scale: float

    def __post_init__(self) -> None:
        if self.scale <= 0:
            raise ValueError(f"display scale must be positive, got {self.scale}")


def point_in_frame(point: Point, frame: Rect) -> bool:
    """True if the point falls inside the frame (inclusive of top/left edge)."""
    origin = frame.origin
    return (
        origin.x <= point.x < origin.x + frame.size.width
        and origin.y <= point.y < origin.y + frame.size.height
    )


def scale_point(point: Point, factor: float) -> Point:
    """Multiply a point's components by a scalar (pure)."""
    if factor <= 0:
        raise ValueError(f"scale factor must be positive, got {factor}")
    return Point(point.x * factor, point.y * factor)


def point_to_pixels(point: Point, scale: float) -> Point:
    """Convert a display-local logical point to physical pixels."""
    return scale_point(point, scale)


def pixels_to_point(px: Point, scale: float) -> Point:
    """Convert display-local physical pixels back to logical points."""
    return scale_point(px, 1.0 / scale)


def display_origin_offset(display: DisplayGeometry) -> Point:
    """The global point offset of a display's top-left corner (multi-display)."""
    return display.frame.origin


def global_point_to_display_px(
    global_point: Point, display: DisplayGeometry
) -> Point:
    """Map a *global logical* point to that display's *pixel* coordinates.

    The global logical point has its origin at the primary display's top-left;
    each display sits at ``frame.origin`` within that global space. We subtract
    the display offset (to get display-local points), then scale by the
    display's Retina factor. Coordinates at or above the display's pixel
    extent are *still returned* — use :func:`point_in_frame` (in points) or the
    caller's pixel bounds to reject out-of-bounds picks before actuating.
    """
    local_px = (
        global_point.x - display.frame.origin.x,
        global_point.y - display.frame.origin.y,
    )
    return scale_point(Point(local_px[0], local_px[1]), display.scale)


def display_px_to_global_point(
    display_px: Point, display: DisplayGeometry
) -> Point:
    """Inverse of :func:`global_point_to_display_px` — pixels back to global."""
    local_points = pixels_to_point(display_px, display.scale)
    return Point(
        local_points.x + display.frame.origin.x,
        local_points.y + display.frame.origin.y,
    )


def point_to_screenshot_offset(
    global_point: Point,
    display: DisplayGeometry,
    screenshot_size: Size,
) -> Point:
    """Convert a global point to an x/y offset in the display's screenshot.

    Accepts only points that actually land on the target display; anything else
    raises :class:`CoordinateOutOfBoundsError` so the caller never indexes into
    another display's (or the wrong display's) pixel buffer by accident.
    """
    if not point_in_frame(global_point, display.frame):
        raise CoordinateOutOfBoundsError(
            f"point {global_point} outside display {display.display_id} frame"
        )
    px = global_point_to_display_px(global_point, display)
    if px.x >= screenshot_size.width or px.y >= screenshot_size.height:
        raise CoordinateOutOfBoundsError(
            f"point {global_point} maps to pixels {px} outside screenshot "
            f"{screenshot_size}"
        )
    return px


class CoordinateOutOfBoundsError(ValueError):
    """A coordinate landed outside its expected display or pixel bounds.

    Law 6.3: this is a precise, catchable error (not a silent clamp) so the
    OODA loop can surface *why* an action failed rather than quietly clicking
    a neighbouring UI element.
    """


@dataclass(frozen=True)
class ScreenMap:
    """Bidirectional map between the model's image space and screen points.

    The VLM never sees the real display: it sees a downscaled screenshot map
    (``downscale_to_max_side``). Two coordinate spaces therefore coexist every
    turn, and mixing them up is the single most expensive mistake in a
    computer-use agent — a 3x error silently clicks the wrong element (or gets
    rejected as out of bounds) with no diagnostic that names the cause.

    This type is the *only* authority on that conversion. It owns both spaces
    at once, so a caller can never apply the factor in the wrong direction:
    :meth:`to_screen` is what the actuation gate uses on model coordinates, and
    :meth:`to_image` is what perception (AX summaries) uses before the model
    ever reads them. Pure, no I/O.
    """

    #: Display size in logical points — the space the driver actuates in.
    logical: Size
    #: Screenshot-map size in image pixels — the space the model reports in.
    image: Size
    #: Top-left of the captured display in *global* logical points. Zero for a
    #: single-display host. Actuation is global, so a screenshot of a secondary
    #: display describes a region that starts partway across that space: the
    #: image's (0,0) is the display's corner, not the desktop's. Owning the
    #: offset here means the same single conversion carries it, and no caller
    #: can apply the scale and forget the shift.
    origin: Point = Point(0.0, 0.0)

    def __post_init__(self) -> None:
        if self.logical.width <= 0 or self.logical.height <= 0:
            raise ValueError(f"logical size must be positive, got {self.logical}")
        if self.image.width <= 0 or self.image.height <= 0:
            raise ValueError(f"image size must be positive, got {self.image}")

    @property
    def points_per_pixel(self) -> float:
        """Logical screen points per image pixel, on the X axis.

        Kept as the axis-agnostic name because callers that only need a rough
        scale (a tolerance, a threshold) should not have to pick an axis.
        Anything converting a *coordinate* uses :attr:`points_per_pixel_x` and
        :attr:`points_per_pixel_y`, which differ slightly: the downscale rounds
        both dimensions to whole pixels, so the two ratios are not identical.
        """
        return self.logical.width / self.image.width

    @property
    def points_per_pixel_x(self) -> float:
        """X-axis scale (differs from Y by rounding, see to_screen)."""
        return self.logical.width / self.image.width

    @property
    def points_per_pixel_y(self) -> float:
        """Y-axis scale: rounding in downscale_to_max_side makes this differ
        microscopically from X, enough to miss a 12pt link on odd ratios."""
        return self.logical.height / self.image.height

    @property
    def is_identity(self) -> bool:
        """True when image space and screen space coincide (no conversion).

        A display at a non-zero global origin is never the identity, however
        its scale works out: its image coordinates still have to be shifted.
        """
        return (
            self.image.width == self.logical.width
            and self.image.height == self.logical.height
            and self.origin.x == 0.0
            and self.origin.y == 0.0
        )

    @property
    def frame(self) -> Rect:
        """The captured display's rectangle in global logical points."""
        return Rect(origin=self.origin, size=self.logical)

    def to_screen(self, point: Point) -> Point:
        """Map a model-reported image-space point to global screen points."""
        return Point(
            point.x * self.points_per_pixel_x + self.origin.x,
            point.y * self.points_per_pixel_y + self.origin.y,
        )

    def to_image(self, point: Point) -> Point:
        """Map a global screen point (e.g. an AX rect) into image space."""
        return Point(
            (point.x - self.origin.x) / self.points_per_pixel_x,
            (point.y - self.origin.y) / self.points_per_pixel_y,
        )
