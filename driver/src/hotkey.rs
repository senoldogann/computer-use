//! Global kill-switch hotkey listener (Law 5.2).
//!
//! The user must always be able to reclaim physical control instantly. Two
//! host-side channels live in this file, both on the same CGEventTap, and the
//! SIGINT catcher stays orchestrator-side:
//!
//! * a *global hotkey* — Command+Shift+Escape;
//! * a *cursor shake* — grabbing the mouse and shaking it.
//!
//! The shake used to be detected in the orchestrator and was therefore never
//! wired: every production entry point built the switch with ``monitor=None``,
//! and even wired it could not have worked, because the OODA loop polls once
//! per step — seconds apart — and the gesture lasts under a second. The tap
//! already runs at HID rate, which is the only place the burst is visible.
//!
//! A tap is the only way to see the user's real input (the driver otherwise
//! observes only events it posts itself), and ADR-1 keeps every host API
//! behind the socket: the tap lives in this process, and the orchestrator
//! polls :func:`tripped` over the ``hotkey_state`` RPC before every step.
//!
//! The kill combo is deliberately rare: Command+Shift+Escape collides with no
//! common shortcut, yet is distinctive enough to be an unmistakable takeover
//! gesture. On match the event is *consumed* (``CallbackResult::Drop``), so
//! the combo never reaches applications as a stray keypress.
//!
//! The listener thread blocks in its own run loop until the process exits; a
//! missing Accessibility consent makes the tap uninstallable, which we log and
//! degrade — the other kill channels still protect the user.
//!
//! **The tap has to be put back.** macOS switches an event tap off when its
//! callback is slow to answer (`kCGEventTapDisabledByTimeout`) and tells the
//! callback so by delivering that as an event. A tap listening only for
//! `KeyDown` never hears it, and a callback that does not call
//! `CGEventTapEnable(tap, true)` never comes back: the first time the host
//! stutters, the kill hotkey dies silently and stays dead for the rest of the
//! session — precisely the moment Law 5.2 exists for. Both disable
//! notifications are therefore in the mask, and both re-arm the tap.
//!
//! This file is macOS-only (gated in ``lib.rs``).

use core::ffi::c_void;
use core::ptr;
use core::sync::atomic::{AtomicBool, AtomicPtr, Ordering};
use std::collections::VecDeque;
use std::sync::{mpsc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use core_foundation::base::TCFType;
use core_foundation::runloop::{kCFRunLoopCommonModes, CFRunLoop};
use foreign_types::ForeignType;
use core_graphics::event::{
    CGEvent, CGEventFlags, CGEventTap, CGEventTapLocation, CGEventTapOptions,
    CGEventTapPlacement, CGEventType, CallbackResult, EventField,
};

/// Virtual keycode for Escape (kVK_Escape) — the kill combo's key.
const KILL_KEYCODE: i64 = 53;

/// The kill-hotkey flag, set by the tap thread and read by the RPC.
static KILL_TRIPPED: AtomicBool = AtomicBool::new(false);

/// The live tap's own mach port, published for the callback.
///
/// Re-arming a tap needs the tap itself, and the callback is handed a *proxy*,
/// which is a different thing — so the port is stashed here the moment it
/// exists and read back from inside the callback. Null until the tap installs;
/// only ever written by the listener thread that owns the tap.
static TAP_PORT: AtomicPtr<c_void> = AtomicPtr::new(ptr::null_mut());

/// How long startup waits for the tap to report armed-or-failed before
/// giving up on it. Generous next to a tap install (microseconds when
/// consent is present) and short next to a run.
const LISTENER_SETTLE_TIMEOUT: Duration = Duration::from_secs(5);

/// Whether the kill-hotkey listener is actively running and armed.
static TAP_ARMED: AtomicBool = AtomicBool::new(false);

/// Query whether the kill-hotkey listener is actively installed and armed.
pub fn is_listener_armed() -> bool {
    TAP_ARMED.load(Ordering::SeqCst)
}

/// Pure: does a key event (keycode + modifier flags) match the kill combo?
///
/// The combo is Command+Shift+Escape. ``contains`` allows *extra* modifier
/// bits (caps lock, function) — only the combo's bits must be present, which
/// is the standard global-hotkey semantics. Kept pure and separate from the
/// tap so the matching rule is unit-testable without a live event stream.
pub fn matches_kill_combo(keycode: i64, flags: CGEventFlags) -> bool {
    let combo = CGEventFlags::CGEventFlagCommand | CGEventFlags::CGEventFlagShift;
    keycode == KILL_KEYCODE && flags.contains(combo)
}

/// Poll the kill-switch state (backing the ``hotkey_state`` RPC).
///
/// Every wired channel is OR-ed here: the Command+Shift+Escape hotkey and the
/// cursor shake. The orchestrator asks one question — "is the human taking
/// over?" — and must not have to know which gesture answered it.
pub fn tripped() -> bool {
    KILL_TRIPPED.load(Ordering::SeqCst) || SHAKE_TRIPPED.load(Ordering::SeqCst)
}

/// Poll only the shake channel (diagnostics and tests).
pub fn shake_tripped() -> bool {
    SHAKE_TRIPPED.load(Ordering::SeqCst)
}

/// Whether a human grabbed the mouse and shook it, set by the tap thread.
static SHAKE_TRIPPED: AtomicBool = AtomicBool::new(false);

/// Rolling cursor trace the shake detector reads.
///
/// Lives here rather than in the orchestrator because that is the whole point
/// of this change: detecting a shake needs the cursor sampled continuously at
/// tens of hertz, and the OODA loop polls seconds apart, which cannot see a
/// burst that lasts under a second. The event tap already runs at HID rate.
static SHAKE_SAMPLES: Mutex<VecDeque<CursorSample>> = Mutex::new(VecDeque::new());

/// Monotonic baseline, so samples carry seconds rather than an `Instant` the
/// pure detector would have to know about.
static CLOCK_START: OnceLock<Instant> = OnceLock::new();

/// How many samples the trace keeps. The detector trims by *time* anyway;
/// this only bounds memory. A 0.5s window at HID rate is well under 64.
const SHAKE_TRACE_CAPACITY: usize = 64;

/// One timestamped cursor position (mirrors the orchestrator's `CursorSample`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct CursorSample {
    pub x: f64,
    pub y: f64,
    pub time_s: f64,
}

/// How long the burst may span. Apple's own shake-to-locate detector uses a
/// 500ms timeout (US 8,159,457), so this matches the gesture users already
/// have muscle memory for rather than inventing a looser number.
pub const SHAKE_WINDOW_S: f64 = 0.5;

/// How far the gesture may wander, as the bounding box of the sampled path.
/// A shake stays in one place; someone reaching across the desk reverses too,
/// and a bounding box tells the two apart where a reversal count cannot.
pub const SHAKE_MAX_EXTENT: f64 = 200.0;

/// The least total path length that counts as motion at all, so jitter and a
/// hand resting on the trackpad are never read as a takeover request.
pub const SHAKE_MIN_TRAVEL: f64 = 40.0;

/// Reversals required inside the window.
pub const SHAKE_MIN_REVERSALS: usize = 6;

/// Pure: does this cursor trace show a rapid, bounded back-and-forth?
///
/// Deliberately the same rule, thresholds included, as the orchestrator's
/// `is_mouse_shake` in `security/killswitch.py`. Two detectors that disagree
/// about what a takeover looks like would be worse than one, and this is the
/// copy that can actually see the gesture.
///
/// A false positive yanks control away from the agent mid-workflow, so all
/// three clauses are checked: *rapid* (reversals inside the window), *bounded*
/// (the path's bounding box stays small), and *motion at all* (minimum travel).
pub fn is_mouse_shake(samples: &[CursorSample]) -> bool {
    if samples.len() < SHAKE_MIN_REVERSALS + 1 {
        return false;
    }
    // Only the tail matters: a burst is recent by definition, and letting old
    // samples contribute is how minutes of ordinary work add up to a shake.
    let newest = samples[samples.len() - 1].time_s;
    let recent: Vec<CursorSample> = samples
        .iter()
        .copied()
        .filter(|sample| newest - sample.time_s <= SHAKE_WINDOW_S)
        .collect();
    if recent.len() < SHAKE_MIN_REVERSALS + 1 {
        return false;
    }
    let travelled: f64 = recent
        .windows(2)
        .map(|pair| (pair[1].x - pair[0].x).abs() + (pair[1].y - pair[0].y).abs())
        .sum();
    if travelled < SHAKE_MIN_TRAVEL {
        return false;
    }
    let extent_x = span(recent.iter().map(|s| s.x));
    let extent_y = span(recent.iter().map(|s| s.y));
    if extent_x.max(extent_y) > SHAKE_MAX_EXTENT {
        // It went somewhere. Someone reaching across the screen reverses too.
        return false;
    }
    // Either axis may dominate depending on how the desk is arranged, so
    // reversals are counted per axis and the combined count is the verdict.
    let x_deltas: Vec<f64> = deltas(recent.iter().map(|s| s.x));
    let y_deltas: Vec<f64> = deltas(recent.iter().map(|s| s.y));
    reversals(&x_deltas) + reversals(&y_deltas) >= SHAKE_MIN_REVERSALS
}

/// Pure: the spread of a coordinate series.
fn span(values: impl Iterator<Item = f64> + Clone) -> f64 {
    let max = values.clone().fold(f64::NEG_INFINITY, f64::max);
    let min = values.fold(f64::INFINITY, f64::min);
    max - min
}

/// Pure: consecutive differences, dropping ones too small to have a direction.
fn deltas(values: impl Iterator<Item = f64>) -> Vec<f64> {
    let collected: Vec<f64> = values.collect();
    collected
        .windows(2)
        .map(|pair| pair[1] - pair[0])
        .filter(|delta| delta.abs() > 1e-6)
        .collect()
}

/// Pure: how many times the direction flips.
fn reversals(deltas: &[f64]) -> usize {
    deltas.windows(2).filter(|pair| pair[0] * pair[1] < 0.0).count()
}

/// Feed one observed cursor position to the detector; report a fresh trip.
///
/// Separated from the tap callback so the whole channel — trace, trimming and
/// verdict — is exercisable without an event tap or a display server.
pub(crate) fn observe_cursor(sample: CursorSample) -> bool {
    let mut trace = SHAKE_SAMPLES
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if trace.len() == SHAKE_TRACE_CAPACITY {
        trace.pop_front();
    }
    trace.push_back(sample);
    let window: Vec<CursorSample> = trace.iter().copied().collect();
    drop(trace);
    if is_mouse_shake(&window) {
        // Only the first trip of a run is news; the flag latches until the
        // process ends, exactly like the hotkey one.
        return !SHAKE_TRIPPED.swap(true, Ordering::SeqCst);
    }
    false
}

/// Seconds since this process started sampling (monotonic).
fn now_seconds() -> f64 {
    CLOCK_START
        .get_or_init(Instant::now)
        .elapsed()
        .as_secs_f64()
}

/// What the tap callback should do about one delivered event (SYS-01).
///
/// Pure and total over the event type plus whether the key payload (when the
/// event carries one) matched the kill combo — no tap handle, no atomics, no
/// display server — so the re-arm rule is unit-testable without hardware:
/// * ``Rearm`` — macOS switched the tap off and is telling us so; the tap
///   must be switched back on or the kill hotkey dies silently (Law 5.2).
/// * ``Trip`` — a real kill-combo keypress; consume it and flag the takeover.
/// * ``Pass`` — anything else; let it through untouched.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum TapAction {
    Pass,
    Rearm,
    Trip,
}

/// Pure: map one tap event to its action (SYS-01 decision rule).
///
/// The disable notifications outrank everything — even a kill combo arriving
/// in the same instant must not shadow the re-arm, because a dead tap makes
/// every later press (kill or not) unobservable. ``is_kill_combo`` is only
/// meaningful for ``KeyDown``; the callback computes it from the key payload
/// and passes ``false`` for anything else.
pub(crate) fn handle_tap_event_type(
    event_type: CGEventType,
    is_kill_combo: bool,
) -> TapAction {
    if matches!(
        event_type,
        CGEventType::TapDisabledByTimeout | CGEventType::TapDisabledByUserInput
    ) {
        TapAction::Rearm
    } else if matches!(event_type, CGEventType::KeyDown) && is_kill_combo {
        TapAction::Trip
    } else {
        TapAction::Pass
    }
}

/// Did this driver post the event itself?
///
/// The whole point of the shake channel is to notice a *human* grabbing the
/// mouse, and this process moves the cursor constantly along Bezier paths. A
/// tap sees its own synthetic events, so without this filter the agent could
/// trip its own kill switch and stop itself mid-run — a false positive that
/// costs the user their work.
///
/// ``kCGEventSourceUnixProcessID`` carries the pid of whoever posted the
/// event; real HID input does not come from this process.
fn is_self_posted(event: &CGEvent) -> bool {
    let poster = event.get_integer_value_field(EventField::EVENT_SOURCE_UNIX_PROCESS_ID);
    poster == i64::from(std::process::id())
}

/// Read an event's modifier flags. The crate exposes ``set_flags`` but no
/// getter; ``CGEventGetFlags`` is a stable CoreGraphics symbol.
fn event_flags(event: &CGEvent) -> CGEventFlags {
    extern "C" {
        fn CGEventGetFlags(event: *const core::ffi::c_void) -> u64;
    }
    let raw = event.as_ptr() as *const core::ffi::c_void;
    CGEventFlags::from_bits_truncate(unsafe { CGEventGetFlags(raw) })
}

/// Switch the tap back on after macOS disabled it.
///
/// Declared locally for the same reason ``CGEventGetFlags`` is: the crate uses
/// the symbol internally but exposes it only as a method on a tap value the
/// callback cannot reach. A null port means the notification arrived before
/// the tap finished installing, which cannot happen — but reading it as "no
/// tap to re-arm" is the only safe interpretation if it ever does.
fn rearm_tap() {
    extern "C" {
        fn CGEventTapEnable(tap: *const c_void, enable: bool);
    }
    let port = TAP_PORT.load(Ordering::SeqCst);
    if port.is_null() {
        return;
    }
    // SAFETY: the pointer is the CFMachPortRef of the tap owned by this
    // thread's listener, which outlives the run loop the callback runs on.
    unsafe { CGEventTapEnable(port, true) };
}

/// Install the kill-hotkey listener on a background thread (real backend only).
///
/// The tap's run loop blocks that thread until the process exits. Only called
/// by the shell when the real backend is live — the simulated backend must
/// never touch the host's event system (Law 1: no accidental actuation).
///
/// Built from :meth:`CGEventTap::new` plus an explicit run-loop source rather
/// than the crate's ``with_enabled`` helper, because re-arming a disabled tap
/// needs the tap's mach port and ``with_enabled`` never lets go of it.
///
/// Returns whether the tap actually armed, and *blocks until that is known*
/// (bounded by [`LISTENER_SETTLE_TIMEOUT`]). The caller binds the socket
/// afterwards, so by the time anything can ask ``health`` whether the kill
/// switch is live, the answer is final rather than a race against thread
/// start-up.
pub fn spawn_listener() -> bool {
    // The verdict travels back on a channel so startup can *wait* for it.
    // Without that wait the socket is already accepting connections while the
    // tap is still installing, so the first ``health`` of every run reports an
    // unarmed kill switch that is about to arm — an alarm that resolves
    // itself, which is the kind that gets ignored.
    let (settled, verdict) = mpsc::channel::<bool>();
    std::thread::Builder::new()
        .name("kill-hotkey-listener".to_string())
        .spawn(move || {
            let tap = match CGEventTap::new(
                CGEventTapLocation::Session,
                CGEventTapPlacement::HeadInsertEventTap,
                CGEventTapOptions::Default,
                // Only real event types belong in the mask. The two
                // out-of-band disable notifications are delivered to the
                // callback whatever the mask says, and their enum values are
                // 0xFFFFFFFE/0xFFFFFFFF — ``1 << value`` overflows, which
                // panicked the listener thread on every debug ``--real``
                // start and set junk bits 62/63 in release.
                vec![
                    CGEventType::KeyDown,
                    // Pointer motion feeds the shake detector. Dragged
                    // variants are included because a human grabbing a mouse
                    // that is mid-drag still means "stop".
                    CGEventType::MouseMoved,
                    CGEventType::LeftMouseDragged,
                    CGEventType::RightMouseDragged,
                ],
                |_proxy, etype, event| {
                    // Pointer motion is observed, never altered: the arms
                    // below can only ever return Keep for it.
                    if matches!(
                        etype,
                        CGEventType::MouseMoved
                            | CGEventType::LeftMouseDragged
                            | CGEventType::RightMouseDragged
                    ) && !is_self_posted(event)
                    {
                        let location = event.location();
                        if observe_cursor(CursorSample {
                            x: location.x,
                            y: location.y,
                            time_s: now_seconds(),
                        }) {
                            eprintln!(
                                "[driver] kill-switch: cursor shake detected; the human is taking over"
                            );
                        }
                    }
                    // The decision itself is pure (`handle_tap_event_type`);
                    // the arms below only perform the side effects it selects.
                    let is_kill = matches!(etype, CGEventType::KeyDown)
                        && matches_kill_combo(
                            event.get_integer_value_field(EventField::KEYBOARD_EVENT_KEYCODE),
                            event_flags(event),
                        );
                    match handle_tap_event_type(etype, is_kill) {
                        TapAction::Rearm => {
                            // Re-arm unconditionally, including the
                            // by-user-input case Apple describes as
                            // deliberate. This tap is the user's escape hatch;
                            // leaving it off because something asked nicely is
                            // not a trade a kill switch may make. Keystrokes
                            // during the disabled window are genuinely lost,
                            // so a combo pressed exactly then must be pressed
                            // again — the line below is what makes a second
                            // press work at all.
                            rearm_tap();
                            eprintln!(
                                "[driver] kill-hotkey tap was disabled by the system; re-armed"
                            );
                            CallbackResult::Keep
                        }
                        TapAction::Trip => {
                            KILL_TRIPPED.store(true, Ordering::SeqCst);
                            // Consume the combo: it is the kill gesture, not
                            // an app shortcut — nothing else should react.
                            CallbackResult::Drop
                        }
                        TapAction::Pass => CallbackResult::Keep,
                    }
                },
            ) {
                Ok(tap) => tap,
                Err(()) => {
                    eprintln!(
                        "[driver] kill-hotkey tap failed to install (grant Accessibility consent?)"
                    );
                    TAP_ARMED.store(false, Ordering::SeqCst);
                    let _ = settled.send(false);
                    return;
                }
            };
            let Ok(source) = tap.mach_port().create_runloop_source(0) else {
                eprintln!("[driver] kill-hotkey run-loop source creation failed");
                TAP_ARMED.store(false, Ordering::SeqCst);
                let _ = settled.send(false);
                return;
            };
            // Published before the loop starts, so the first disable
            // notification — whenever it arrives — already has a port to
            // re-arm.
            TAP_PORT.store(
                tap.mach_port().as_concrete_TypeRef() as *mut c_void,
                Ordering::SeqCst,
            );
            CFRunLoop::get_current().add_source(&source, unsafe { kCFRunLoopCommonModes });
            tap.enable();
            TAP_ARMED.store(true, Ordering::SeqCst);
            let _ = settled.send(true);
            // Blocks until the process exits; `tap` stays alive for the
            // duration, which is what keeps the port in TAP_PORT valid.
            CFRunLoop::run_current();
            TAP_ARMED.store(false, Ordering::SeqCst);
        })
        .expect("failed to spawn kill-hotkey listener thread");
    match verdict.recv_timeout(LISTENER_SETTLE_TIMEOUT) {
        Ok(armed) => armed,
        Err(_) => {
            // Bounded on purpose: a tap install that has not answered in this
            // long is not going to, and hanging driver startup on it would
            // trade a reportable degradation for an unreportable one.
            eprintln!(
                "[driver] kill-hotkey tap did not report within {}s; treating it as unarmed",
                LISTENER_SETTLE_TIMEOUT.as_secs()
            );
            false
        }
    }
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;
    use core_graphics::event_source::{CGEventSource, CGEventSourceStateID};

    fn key_event(keycode: u16, flags: CGEventFlags) -> CGEvent {
        let source = CGEventSource::new(CGEventSourceStateID::HIDSystemState)
            .expect("event source");
        let event = CGEvent::new_keyboard_event(source, keycode, true).expect("key event");
        event.set_flags(flags);
        event
    }


    /// Build a trace at a fixed sample rate (the tap's own shape).
    fn trace(points: &[(f64, f64)], interval_s: f64) -> Vec<CursorSample> {
        points
            .iter()
            .enumerate()
            .map(|(index, &(x, y))| CursorSample {
                x,
                y,
                time_s: index as f64 * interval_s,
            })
            .collect()
    }

    /// A human forcing the mouse side to side: many reversals, fast, in place.
    #[test]
    fn a_rapid_bounded_oscillation_is_a_shake() {
        let mut points = Vec::new();
        for index in 0..16 {
            points.push((if index % 2 == 0 { 0.0 } else { 60.0 }, 300.0));
        }
        assert!(is_mouse_shake(&trace(&points, 0.02)));
    }

    /// The three clauses, each denied on its own.
    #[test]
    fn ordinary_pointer_work_is_not_a_shake() {
        // Same oscillation, spread over minutes instead of half a second.
        let mut slow = Vec::new();
        for index in 0..16 {
            slow.push((if index % 2 == 0 { 0.0 } else { 60.0 }, 300.0));
        }
        assert!(
            !is_mouse_shake(&trace(&slow, 10.0)),
            "reversals spread over minutes are ordinary work, not a takeover"
        );

        // Reversals, but travelling across the screen: someone reaching, not
        // shaking. The bounding box is what tells the two apart.
        let mut wandering = Vec::new();
        for index in 0..16 {
            let base = index as f64 * 40.0;
            wandering.push((base + if index % 2 == 0 { 0.0 } else { 20.0 }, 300.0));
        }
        assert!(!is_mouse_shake(&trace(&wandering, 0.02)));

        // Micro-jitter: a hand resting on the trackpad reverses constantly and
        // must never reclaim control.
        let mut jitter = Vec::new();
        for index in 0..16 {
            jitter.push((if index % 2 == 0 { 0.0 } else { 0.5 }, 300.0));
        }
        assert!(!is_mouse_shake(&trace(&jitter, 0.02)));

        // A straight sweep has no reversals at all.
        let sweep: Vec<(f64, f64)> = (0..16).map(|i| (i as f64 * 10.0, 300.0)).collect();
        assert!(!is_mouse_shake(&trace(&sweep, 0.02)));

        // Too few samples to judge.
        assert!(!is_mouse_shake(&trace(&[(0.0, 0.0), (10.0, 0.0)], 0.02)));
    }

    /// The channel latches on the first trip and reports it exactly once, so
    /// a run cannot be told "the human took over" on every later sample.
    #[test]
    fn observe_cursor_latches_and_announces_once() {
        // Fresh process state is assumed by the assertions below; this test
        // owns the statics because cargo runs it in its own binary.
        assert!(!shake_tripped());
        let mut announcements = 0;
        for index in 0..16 {
            if observe_cursor(CursorSample {
                x: if index % 2 == 0 { 0.0 } else { 60.0 },
                y: 300.0,
                time_s: index as f64 * 0.02,
            }) {
                announcements += 1;
            }
        }
        assert_eq!(announcements, 1, "a latched trip is news exactly once");
        assert!(shake_tripped());
        // And the composed switch reports it, which is what the RPC returns.
        assert!(tripped());
    }

    #[test]
    fn kill_combo_matches_only_the_exact_combo() {
        let combo = CGEventFlags::CGEventFlagCommand | CGEventFlags::CGEventFlagShift;
        assert!(matches_kill_combo(53, combo));
        // Escape without the modifiers, or another key with them, must not trip.
        assert!(!matches_kill_combo(53, CGEventFlags::empty()));
        assert!(!matches_kill_combo(12, combo)); // 'Q'
        assert!(!matches_kill_combo(53, CGEventFlags::CGEventFlagCommand));
    }

    #[test]
    fn event_flags_roundtrips_set_flags() {
        let event =
            key_event(53, CGEventFlags::CGEventFlagCommand | CGEventFlags::CGEventFlagShift);
        assert!(event_flags(&event).contains(CGEventFlags::CGEventFlagCommand));
        assert!(event_flags(&event).contains(CGEventFlags::CGEventFlagShift));
        let plain = key_event(53, CGEventFlags::empty());
        assert!(event_flags(&plain).is_empty());
    }

    // SYS-01: the re-arm decision is pure — no tap, no display server, no
    // hardware — so CI pins it deterministically.
    #[test]
    fn tap_disable_notifications_rearm_instead_of_dying() {
        assert_eq!(
            handle_tap_event_type(CGEventType::TapDisabledByTimeout, false),
            TapAction::Rearm
        );
        assert_eq!(
            handle_tap_event_type(CGEventType::TapDisabledByUserInput, false),
            TapAction::Rearm
        );
        // A disable outranks everything: even a kill combo arriving in the
        // same instant must not shadow the re-arm.
        assert_eq!(
            handle_tap_event_type(CGEventType::TapDisabledByTimeout, true),
            TapAction::Rearm
        );
        assert_eq!(
            handle_tap_event_type(CGEventType::TapDisabledByUserInput, true),
            TapAction::Rearm
        );
    }

    #[test]
    fn key_events_never_rearm_and_trip_only_on_the_kill_combo() {
        assert_eq!(
            handle_tap_event_type(CGEventType::KeyDown, false),
            TapAction::Pass
        );
        assert_eq!(
            handle_tap_event_type(CGEventType::KeyDown, true),
            TapAction::Trip
        );
        // Non-key traffic is never a kill gesture and never a disable.
        assert_eq!(
            handle_tap_event_type(CGEventType::Null, false),
            TapAction::Pass
        );
        assert_eq!(
            handle_tap_event_type(CGEventType::Null, true),
            TapAction::Pass
        );
    }
}
