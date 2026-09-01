//! Global kill-switch hotkey listener (Law 5.2).
//!
//! The user must always be able to reclaim physical control instantly; besides
//! the mouse-shake monitor and the SIGINT catcher (both orchestrator-side), a
//! *global hotkey* — Command+Shift+Escape — is registered on the host via a
//! CGEventTap. A tap is the only way to see the user's real keystrokes (the
//! driver only observes events it posts itself), and ADR-1 keeps every host
//! API behind the socket: the tap lives in this process, and the orchestrator
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
//! This file is macOS-only (gated in ``lib.rs``).

use core::sync::atomic::{AtomicBool, Ordering};

use core_foundation::runloop::CFRunLoop;
use foreign_types::ForeignType;
use core_graphics::event::{
    CGEvent, CGEventFlags, CGEventTap, CGEventTapLocation, CGEventTapOptions,
    CGEventTapPlacement, CGEventType, CallbackResult, EventField,
};

/// Virtual keycode for Escape (kVK_Escape) — the kill combo's key.
const KILL_KEYCODE: i64 = 53;

/// The kill-hotkey flag, set by the tap thread and read by the RPC.
static KILL_TRIPPED: AtomicBool = AtomicBool::new(false);

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

/// Poll the kill-hotkey state (backing the ``hotkey_state`` RPC).
pub fn tripped() -> bool {
    KILL_TRIPPED.load(Ordering::SeqCst)
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

/// Install the kill-hotkey listener on a background thread (real backend only).
///
/// The tap's run loop blocks that thread until the process exits. Only called
/// by the shell when the real backend is live — the simulated backend must
/// never touch the host's event system (Law 1: no accidental actuation).
pub fn spawn_listener() {
    std::thread::spawn(|| {
        let result = CGEventTap::with_enabled(
            CGEventTapLocation::Session,
            CGEventTapPlacement::HeadInsertEventTap,
            CGEventTapOptions::Default,
            vec![CGEventType::KeyDown],
            |_proxy, _etype, event| {
                if matches_kill_combo(
                    event.get_integer_value_field(EventField::KEYBOARD_EVENT_KEYCODE),
                    event_flags(event),
                ) {
                    KILL_TRIPPED.store(true, Ordering::SeqCst);
                    // Consume the combo: it is the kill gesture, not an app
                    // shortcut — nothing else should react to it.
                    CallbackResult::Drop
                } else {
                    CallbackResult::Keep
                }
            },
            CFRunLoop::run_current,
        );
        if result.is_err() {
            eprintln!(
                "[driver] kill-hotkey tap failed to install (grant Accessibility consent?)"
            );
        }
    });
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
}
