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
pub fn spawn_listener() {
    std::thread::spawn(|| {
        let tap = match CGEventTap::new(
            CGEventTapLocation::Session,
            CGEventTapPlacement::HeadInsertEventTap,
            CGEventTapOptions::Default,
            vec![
                CGEventType::KeyDown,
                // Not keystrokes — the two ways macOS tells a callback its tap
                // has been switched off. Without them in the mask the
                // notification is never delivered and the tap stays dead.
                CGEventType::TapDisabledByTimeout,
                CGEventType::TapDisabledByUserInput,
            ],
            |_proxy, etype, event| {
                if matches!(
                    etype,
                    CGEventType::TapDisabledByTimeout | CGEventType::TapDisabledByUserInput
                ) {
                    // Re-arm unconditionally, including the by-user-input case
                    // Apple describes as deliberate. This tap is the user's
                    // escape hatch; leaving it off because something asked
                    // nicely is not a trade a kill switch may make. Keystrokes
                    // during the disabled window are genuinely lost, so a
                    // combo pressed exactly then must be pressed again — the
                    // line below is what makes a second press work at all.
                    rearm_tap();
                    eprintln!("[driver] kill-hotkey tap was disabled by the system; re-armed");
                    return CallbackResult::Keep;
                }
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
        ) {
            Ok(tap) => tap,
            Err(()) => {
                eprintln!(
                    "[driver] kill-hotkey tap failed to install (grant Accessibility consent?)"
                );
                return;
            }
        };
        let Ok(source) = tap.mach_port().create_runloop_source(0) else {
            eprintln!("[driver] kill-hotkey run-loop source creation failed");
            return;
        };
        // Published before the loop starts, so the first disable notification
        // — whenever it arrives — already has a port to re-arm.
        TAP_PORT.store(
            tap.mach_port().as_concrete_TypeRef() as *mut c_void,
            Ordering::SeqCst,
        );
        CFRunLoop::get_current().add_source(&source, unsafe { kCFRunLoopCommonModes });
        tap.enable();
        // Blocks until the process exits; `tap` stays alive for the duration,
        // which is what keeps the port in TAP_PORT valid.
        CFRunLoop::run_current();
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
