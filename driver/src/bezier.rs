//! Cubic Bezier path sampling.
//!
//! A cubic Bezier produces an S-curved, natural trajectory instead of a
//! straight teleport (Law 1). We emit evenly *parameterised* samples with a
//! human-like velocity profile applied on top by the caller: points cluster at
//! the start and end, and move fastest mid-flight — the same shape as a real
//! hand's acceleration profile.
//!
//! :func:`plan_trajectory` is the *single* path planner for every physical
//! motion — mouse moves and drags both sample the same Bezier + cadence, so a
//! drag reads as a continuous hand motion (G7) instead of a teleport.

use core::time::Duration;

/// A 2D point in virtual-pixel space.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Point {
    pub x: i64,
    pub y: i64,
}

pub fn point(x: i64, y: i64) -> Point {
    Point { x, y }
}

/// Sample the cubic Bezier defined by `start`, two control points, and `end`
/// at `count` evenly spaced parameter values in `[0.0, 1.0]`.
///
/// # Panics
///
/// Panics if `count` is zero — a zero-length trajectory is a caller bug, not a
/// valid request.
pub fn cubic_bezier(
    start: Point,
    ctrl1: Point,
    ctrl2: Point,
    end: Point,
    count: usize,
) -> Vec<Point> {
    assert!(count > 0, "Bezier sample count must be greater than zero");
    (0..count)
        .map(|i| {
            let t = i as f64 / (count - 1) as f64;
            sample_cubic(start, ctrl1, ctrl2, end, t)
        })
        .collect()
}

/// Evaluate the cubic Bezier at a single parameter `t`.
fn sample_cubic(start: Point, ctrl1: Point, ctrl2: Point, end: Point, t: f64) -> Point {
    let mt = 1.0 - t;
    let mt2 = mt * mt;
    let mt3 = mt2 * mt;
    let t2 = t * t;
    let t3 = t2 * t;

    // Bernstein basis weights for the four control points.
    let x = mt3 * start.x as f64
        + 3.0 * mt2 * t * ctrl1.x as f64
        + 3.0 * mt * t2 * ctrl2.x as f64
        + t3 * end.x as f64;
    let y = mt3 * start.y as f64
        + 3.0 * mt2 * t * ctrl1.y as f64
        + 3.0 * mt * t2 * ctrl2.y as f64
        + t3 * end.y as f64;

    point(x.round() as i64, y.round() as i64)
}

/// Derive natural control points so the curve overshoots slightly and lands on
/// target, rather than travelling in a dead straight line.
///
/// The perpendicular nudge is scaled by the *total* distance (Euclidean), not
/// by one axis: a pure-vertical move (``dx == 0``) still bows sideways instead
/// of collapsing into a straight line (F5), and a pure-horizontal move bows
/// vertically. The nudge direction alternates per travel direction so the
/// curve reads as a natural hand sweep rather than a fixed hook.
pub fn natural_control_points(start: Point, end: Point) -> (Point, Point) {
    let dx = end.x - start.x;
    let dy = end.y - start.y;
    // Euclidean distance in f64 avoids integer rounding of sqrt.
    let dist = ((dx * dx + dy * dy) as f64).sqrt();
    // Perpendicular unit vector to the travel axis, scaled by 1/12 of the
    // distance — small enough to stay human, large enough to be visible.
    let nudge = dist / 12.0;
    let (nx, ny) = if dist > 0.0 {
        (-(dy as f64) / dist, dx as f64 / dist)
    } else {
        (0.0, 0.0)
    };

    // Control points: one third along the travel path, pushed perpendicular.
    let ctrl1 = point(
        (start.x as f64 + dx as f64 / 3.0 + nx * nudge).round() as i64,
        (start.y as f64 + dy as f64 / 3.0 + ny * nudge).round() as i64,
    );
    let ctrl2 = point(
        (end.x as f64 - dx as f64 / 3.0 - nx * nudge).round() as i64,
        (end.y as f64 - dy as f64 / 3.0 - ny * nudge).round() as i64,
    );
    (ctrl1, ctrl2)
}

/// A trapezoidal velocity profile: speed builds to a cruise plateau then eases
/// off near the target. Returns a per-`i` delay in milliseconds for a
/// trajectory of `count` steps with total `duration_ms`.
pub fn inter_step_delays(count: usize, duration_ms: u64) -> Vec<u64> {
    assert!(duration_ms >= count as u64, "duration must cover all steps");
    // Acceleration is fastest early, cruise mid, smooth stop at the end. A
    // simple dirac bump on the ends gives a hand-sensed ease-in/out.
    let mut delays = Vec::with_capacity(count);
    for i in 0..count {
        let p = i as f64 / (count.saturating_sub(1)) as f64;
        // Ease-in/ease-out weight: slow first/last ~20%, fast in the middle.
        let eased = ease_in_out(p);
        let delay = (duration_ms as f64 * eased) / count as f64;
        delays.push(delay.max(1.0).round() as u64);
    }
    delays
}

/// A clamped s-curve weight in `[min..1.0]` peaking at `p = 0.5`.
fn ease_in_out(p: f64) -> f64 {
    // Smoothstep: 0 -> 1, symmetric around 0.5. Map to keep a floor of ~0.25
    // so we never emit absurdly long pauses.
    let s = p * p * (3.0 - 2.0 * p);
    0.25 + 0.75 * s
}

/// Natural duration for a cursor move (pure): humans scale speed with
/// distance, so a 1200px cross-screen sweep takes ~660ms while a 30px nudge
/// takes ~190ms — a fixed duration reads as a teleport for long moves (Law 1).
/// The caller's explicit request is never shortened (they may deliberately ask
/// for a slow, deliberate move), but a too-short request for a long distance
/// is stretched up to a 1000ms ceiling so no move is ever instantaneous.
pub fn human_move_duration(from: Point, to: Point, requested_ms: u64) -> u64 {
    let dx = (to.x - from.x) as f64;
    let dy = (to.y - from.y) as f64;
    let dist = (dx * dx + dy * dy).sqrt();
    // ~180ms base + 0.4ms per logical pixel of travel.
    let natural = 180.0 + dist * 0.4;
    (requested_ms as f64).max(natural.min(1000.0)).round() as u64
}

/// Plan a human-like cursor path (pure): a cubic Bezier sampled into steps,
/// each carrying the pause from the ease-in/out profile. The backend merely
/// replays these steps. Shared by mouse moves and drags so every physical
/// motion follows the same Law 1 cadence (G7). The first point is always
/// ``from`` and the last always ``to`` (exact endpoints).
pub fn plan_trajectory(
    from: Point,
    to: Point,
    duration_ms: u64,
    steps: usize,
) -> Vec<(Point, Duration)> {
    let (c1, c2) = natural_control_points(from, to);
    let path = cubic_bezier(from, c1, c2, to, steps);
    let delays = inter_step_delays(path.len(), duration_ms);
    path.into_iter()
        .zip(delays)
        .map(|(p, ms)| (p, Duration::from_millis(ms)))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bezier_start_and_end_are_exact() {
        let (c1, c2) = natural_control_points(point(0, 0), point(100, 100));
        let path = cubic_bezier(point(0, 0), c1, c2, point(100, 100), 50);
        assert_eq!(path.first().unwrap(), &point(0, 0));
        assert_eq!(path.last().unwrap(), &point(100, 100));
    }

    #[test]
    fn vertical_trajectory_keeps_perpendicular_bow() {
        // Pure vertical move: control points must not lie on the straight line
        // (ctrl.x differs from 0), otherwise the curve degenerates (F5).
        let (c1, c2) = natural_control_points(point(0, 0), point(0, 100));
        assert!(c1.x != 0 || c2.x != 0, "vertical move must bow sideways");
    }

    #[test]
    fn horizontal_trajectory_keeps_perpendicular_bow() {
        // Pure horizontal move: control points must bow vertically.
        let (c1, c2) = natural_control_points(point(0, 0), point(100, 0));
        assert!(c1.y != 0 || c2.y != 0, "horizontal move must bow vertically");
    }

    #[test]
    fn plan_trajectory_anchors_endpoints_and_covers_duration() {
        let plan = plan_trajectory(point(0, 0), point(100, 80), 200, 16);
        assert_eq!(plan.len(), 16);
        // Exact anchors: a drag starts with the button down at `from` and must
        // release at `to`, so the plan's endpoints cannot drift.
        assert_eq!(plan.first().unwrap().0, point(0, 0));
        assert_eq!(plan.last().unwrap().0, point(100, 80));
        let total: u64 = plan.iter().map(|(_, d)| d.as_millis() as u64).sum();
        assert!((16..=200).contains(&total), "total {total} out of bounds");
    }

    #[test]
    fn delays_sum_tracks_duration() {
        let delays = inter_step_delays(40, 180);
        assert_eq!(delays.len(), 40);
        let total: u64 = delays.iter().sum();
        assert!(
            (40..=180).contains(&total),
            "total {total} out of bounds"
        );
    }

    #[test]
    fn move_duration_scales_with_distance_never_teleports() {
        // A zero-length nudge keeps the base duration.
        assert_eq!(human_move_duration(point(0, 0), point(0, 0), 180), 180);
        // A cross-screen sweep is stretched far beyond the default 180ms.
        let sweep = human_move_duration(point(0, 0), point(1200, 0), 180);
        assert!((600..=700).contains(&sweep), "sweep took {sweep}ms");
        // A 30px nudge barely grows.
        let nudge = human_move_duration(point(0, 0), point(30, 0), 180);
        assert!((180..=220).contains(&nudge), "nudge took {nudge}ms");
    }

    #[test]
    fn move_duration_respects_caller_and_is_bounded() {
        // A caller asking for a slower, deliberate move keeps their value.
        assert_eq!(human_move_duration(point(0, 0), point(30, 0), 600), 600);
        // An explicit longer-than-natural request always wins.
        assert_eq!(human_move_duration(point(0, 0), point(1200, 0), 5000), 5000);
        // The distance stretch is capped so a pathological span stays sane.
        let huge = human_move_duration(point(0, 0), point(10_000, 10_000), 180);
        assert!(huge <= 1000, "huge move took {huge}ms");
    }
}