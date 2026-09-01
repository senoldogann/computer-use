//! Real macOS actuation via CoreGraphics CGEvent (Law 1).
//!
//! This is the physical connector behind the orchestrator: it maps the
//! platform-agnostic :struct:`Backend` trait onto Quartz event posting, so the
//! OODA loop and the pure Bezier planner never know which backend is live.
//!
//! CGEvent posting requires the host process to hold *Accessibility* consent
//! (System Settings > Privacy & Security > Accessibility). Without it the OS
//! silently drops synthesized events, so we gate on it up-front via
//! ``CGRequestPostEventAccess`` and fail loudly rather than pretending to
//! actuate (Law 6.3 explicit errors; Law 5 consent boundaries).
//!
//! This file is macOS-only (gated in ``lib.rs``) and compiled out elsewhere.

use core::time::Duration;

use core_graphics::event::{
    CGEvent, CGEventFlags, CGEventTapLocation, CGEventType, CGMouseButton, EventField, KeyCode as QK,
    ScrollEventUnit,
};
use core_graphics::event_source::{CGEventSource, CGEventSourceStateID};
use core_graphics::geometry::CGPoint;

use crate::backend::{
    Backend, BackendError, Button, CaptureFrame, FocusedWindow, HostElement, Modifier, TrajectoryStep,
};
use crate::bezier::Point;

/// Maps our platform-neutral mouse button to the Quartz code.
fn quartz_button(button: Button) -> CGMouseButton {
    match button {
        Button::Left => CGMouseButton::Left,
        Button::Right => CGMouseButton::Right,
        Button::Middle => CGMouseButton::Center,
    }
}

/// Quartz keycodes for the modifier keys themselves (kVK_Command,
/// kVK_Shift, kVK_Option, kVK_Control) — used to post real flagsChanged
/// press/release events around a hotkey so the HID system state tracks
/// the press *and the release*.
fn modifier_keycode(modifier: Modifier) -> u16 {
    match modifier {
        Modifier::Command => 55,
        Modifier::Shift => 56,
        Modifier::Alt => 58,
        Modifier::Control => 59,
    }
}

/// Maps a modifier to its Quartz flag bit.
fn modifier_flag(modifier: Modifier) -> CGEventFlags {
    match modifier {
        Modifier::Command => CGEventFlags::CGEventFlagCommand,
        Modifier::Shift => CGEventFlags::CGEventFlagShift,
        Modifier::Alt => CGEventFlags::CGEventFlagAlternate,
        Modifier::Control => CGEventFlags::CGEventFlagControl,
    }
}

/// Errors from event construction are silently returned by the FFI as `()`;
/// we wrap them with the operation name so the log has context (Law 6.3).
fn event_err(what: &str, x: i64, y: i64) -> BackendError {
    BackendError(format!("failed to create {what} at ({x},{y})"))
}

/// Real mouse/keyboard backend.
pub struct QuartzBackend {
    /// Source anchored to the HID system state so events are treated as
    /// hardware-born; many apps reject input that lacks a HID source.
    source: CGEventSource,
}

// The `core-graphics` crate does not mark `CGEventSource` as Send/Sync even
// though CoreFoundation objects are thread-safe for retain/release and we only
// ever *clone* the source (each event construction clones it). Threading the
// backend per-connection (F4) therefore needs this documented marker.
// SAFETY: the only operations on `source` are `clone()` (CFRetain/CFRelease)
// and `as_ptr()` at event-construction time; both are safe to call from
// multiple threads simultaneously per CoreFoundation's memory model.
unsafe impl Send for QuartzBackend {}
unsafe impl Sync for QuartzBackend {}

impl std::fmt::Debug for QuartzBackend {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("QuartzBackend").finish_non_exhaustive()
    }
}

impl QuartzBackend {
    /// Build the backend, requiring Accessibility consent first.
    pub fn new() -> Result<Self, BackendError> {
        let source = CGEventSource::new(CGEventSourceStateID::HIDSystemState)
            .map_err(|()| BackendError("cannot create CGEventSource".to_string()))?;
        if !post_event_access() {
            return Err(BackendError(
                "Accessibility consent required. Grant it in System Settings \
                 > Privacy & Security > Accessibility, then restart the driver."
                    .to_string(),
            ));
        }
        Ok(QuartzBackend { source })
    }

    /// Move the cursor instantly to a point by posting a MouseMoved event.
    fn move_instant(&self, to: Point) -> Result<(), BackendError> {
        use core_graphics::display::CGDisplay;

        let location = CGPoint::new(to.x as f64, to.y as f64);
        let _ = CGDisplay::warp_mouse_cursor_position(location);
        let mov = CGEvent::new_mouse_event(
            self.source.clone(),
            CGEventType::MouseMoved,
            location,
            CGMouseButton::Left,
        )
        .map_err(|()| event_err("MouseMoved", to.x, to.y))?;
        mov.post(CGEventTapLocation::HID);
        Ok(())
    }

    /// Press and release a button at a position, repeating for click_count.
    ///
    /// Each pair carries the Quartz click-state field (1 for a single click,
    /// 2 for the second click of a double) so AppKit recognizes a true
    /// double-click instead of two independent single clicks (F5).
    fn button_sequence(
        &self,
        at: Point,
        down: CGEventType,
        up: CGEventType,
        button: CGMouseButton,
        click_count: u8,
    ) -> Result<(), BackendError> {
        use core_graphics::display::CGDisplay;

        let location = CGPoint::new(at.x as f64, at.y as f64);
        let _ = CGDisplay::warp_mouse_cursor_position(location);
        self.move_instant(at)?;
        std::thread::sleep(Duration::from_millis(30));

        let total = click_count.max(1);
        for index in 0..total {
            // 1-based click position; a double click's second press is state 2.
            let click_state: i64 = index as i64 + 1;
            let down_event =
                CGEvent::new_mouse_event(self.source.clone(), down, location, button)
                    .map_err(|()| event_err("mouse-down", at.x, at.y))?;
            down_event.set_integer_value_field(EventField::MOUSE_EVENT_CLICK_STATE, click_state);
            down_event.post(CGEventTapLocation::HID);
            std::thread::sleep(Duration::from_millis(40));

            let up_event = CGEvent::new_mouse_event(self.source.clone(), up, location, button)
                .map_err(|()| event_err("mouse-up", at.x, at.y))?;
            up_event.set_integer_value_field(EventField::MOUSE_EVENT_CLICK_STATE, click_state);
            up_event.post(CGEventTapLocation::HID);

            if total > 1 && index + 1 < total {
                std::thread::sleep(Duration::from_millis(60));
            }
        }
        Ok(())
    }

    /// Post a real modifier press/release (flagsChanged) event.
    ///
    /// Labeling only the letter key-down/up with modifier flags is not
    /// enough: the HID system's held-modifier state is only updated by
    /// actual modifier key events. Without an explicit release event the OS
    /// keeps reporting the modifier as held, and every *later* mouse event
    /// inherits the flag — a click becomes Cmd+click and Chrome opens the
    /// target in a new background tab instead of navigating (observed as
    /// mystery "Logout" tabs in the field).
    fn post_modifier_event(&self, modifier: Modifier, pressed: bool) -> Result<(), BackendError> {
        let flags = if pressed {
            modifier_flag(modifier)
        } else {
            CGEventFlags::empty()
        };
        let event = CGEvent::new_keyboard_event(
            self.source.clone(),
            modifier_keycode(modifier),
            pressed,
        )
        .map_err(|()| BackendError("failed to create modifier event".to_string()))?;
        event.set_flags(flags);
        event.post(CGEventTapLocation::HID);
        Ok(())
    }
}

/// Thin FFI wrapper: may this process post CGEvents (Accessibility consent)?
fn post_event_access() -> bool {
    #[link(name = "ApplicationServices", kind = "framework")]
    extern "C" {
        fn AXIsProcessTrusted() -> bool;
        fn AXIsProcessTrustedWithOptions(options: core_foundation::dictionary::CFDictionaryRef) -> bool;
        fn CGRequestPostEventAccess() -> bool;
    }
    unsafe {
        if AXIsProcessTrusted() || CGRequestPostEventAccess() {
            return true;
        }
        use core_foundation::base::TCFType;
        use core_foundation::boolean::CFBoolean;
        use core_foundation::dictionary::CFDictionary;
        use core_foundation::string::CFString;
        let key = CFString::new("AXTrustedCheckOptionPrompt");
        let val = CFBoolean::true_value();
        let dict = CFDictionary::from_CFType_pairs(&[(key, val)]);
        AXIsProcessTrustedWithOptions(dict.as_concrete_TypeRef())
    }
}

impl Backend for QuartzBackend {
    fn current_position(&self) -> Result<Point, BackendError> {
        let event = CGEvent::new(self.source.clone())
            .map_err(|()| BackendError("cannot create a probe CGEvent".to_string()))?;
        let location = event.location();
        Ok(crate::bezier::point(location.x as i64, location.y as i64))
    }

    fn move_along(&self, steps: &[TrajectoryStep]) -> Result<(), BackendError> {
        for (pos, wait) in steps {
            // Law 5.2: the user's kill combo must interrupt a long sweep, not
            // wait for the whole trajectory to finish. Poll the tap flag
            // between segments — same channel the orchestrator polls via
            // hotkey_state, so mid-action takeover is immediate.
            if crate::hotkey::tripped() {
                return Err(BackendError(
                    "cancelled by user (kill-switch) during mouse move".to_string(),
                ));
            }
            self.move_instant(*pos)?;
            if !wait.is_zero() {
                std::thread::sleep(*wait);
            }
        }
        Ok(())
    }

    fn click(&self, at: Point, button: Button, click_count: u8) -> Result<(), BackendError> {
        let (down, up) = match button {
            Button::Left => (CGEventType::LeftMouseDown, CGEventType::LeftMouseUp),
            Button::Right => (CGEventType::RightMouseDown, CGEventType::RightMouseUp),
            Button::Middle => (CGEventType::OtherMouseDown, CGEventType::OtherMouseUp),
        };
        self.button_sequence(at, down, up, quartz_button(button), click_count)?;
        // Human dwell: a real hand lingers a moment after a click before the
        // next action starts (and the UI needs that beat to render the
        // response). Without it, consecutive clicks fire back-to-back and the
        // whole run reads mechanical (Law 1 cadence).
        std::thread::sleep(Duration::from_millis(120));
        Ok(())
    }

    fn drag(&self, from: Point, to: Point, duration_ms: u64) -> Result<(), BackendError> {
        // Law 1: a drag is a continuous hand motion, so it follows the same
        // Bezier + cadence plan as a mouse move instead of teleporting to the
        // target (G7) — drag-and-drop UIs expect an event *stream*, and a
        // single LeftMouseDragged jump can be ignored by native apps. The
        // duration is distance-adaptive like a move, so long drags never
        // read as a teleport.
        let duration = crate::bezier::human_move_duration(from, to, duration_ms);
        let plan = crate::bezier::plan_trajectory(from, to, duration, 16);
        let location = CGPoint::new(from.x as f64, from.y as f64);
        let down = CGEvent::new_mouse_event(
            self.source.clone(),
            CGEventType::LeftMouseDown,
            location,
            CGMouseButton::Left,
        )
        .map_err(|()| event_err("drag-down", from.x, from.y))?;
        down.post(CGEventTapLocation::HID);

        // The plan's first point is `from` (the button is already down there);
        // every subsequent sample is posted as a drag move with the plan's
        // natural pause, ending exactly at `to`.
        for (pos, wait) in plan.iter().skip(1) {
            if crate::hotkey::tripped() {
                // A drag holds the button down: release it at the current
                // position before bailing, or the host is left with a stuck
                // mouse-down (Law 5: reclaiming control must not strand the
                // physical device in a half-drag state).
                let up = CGEvent::new_mouse_event(
                    self.source.clone(),
                    CGEventType::LeftMouseUp,
                    CGPoint::new(pos.x as f64, pos.y as f64),
                    CGMouseButton::Left,
                )
                .map_err(|()| event_err("drag-up", pos.x, pos.y))?;
                up.post(CGEventTapLocation::HID);
                return Err(BackendError(
                    "cancelled by user (kill-switch) during drag".to_string(),
                ));
            }
            let dragged = CGEvent::new_mouse_event(
                self.source.clone(),
                CGEventType::LeftMouseDragged,
                CGPoint::new(pos.x as f64, pos.y as f64),
                CGMouseButton::Left,
            )
            .map_err(|()| event_err("drag-move", pos.x, pos.y))?;
            dragged.post(CGEventTapLocation::HID);
            if !wait.is_zero() {
                std::thread::sleep(*wait);
            }
        }

        let up = CGEvent::new_mouse_event(
            self.source.clone(),
            CGEventType::LeftMouseUp,
            CGPoint::new(to.x as f64, to.y as f64),
            CGMouseButton::Left,
        )
        .map_err(|()| event_err("drag-up", to.x, to.y))?;
        up.post(CGEventTapLocation::HID);
        Ok(())
    }

    fn scroll(&self, dx: i64, dy: i64) -> Result<(), BackendError> {
        // A scroll wheel event carries its deltas as fields; the `highsierra`
        // constructor lets us pass pixel units directly.
        let event = CGEvent::new_scroll_event(self.source.clone(), ScrollEventUnit::PIXEL, 2, dx as i32, dy as i32, 0)
        .map_err(|()| BackendError("failed to create scroll event".to_string()))?;
        event.post(CGEventTapLocation::HID);
        Ok(())
    }

    fn hotkey(&self, modifiers: &[Modifier], key: &str) -> Result<(), BackendError> {
        let keycode = keycode_of(key)
            .ok_or_else(|| BackendError(format!("unsupported hotkey key {key:?}")))?;
        let mut flags = CGEventFlags::empty();
        // Press each modifier with a real flagsChanged event first so the
        // HID state records the press (not just per-event flag labels).
        for modifier in modifiers {
            flags.insert(modifier_flag(*modifier));
            self.post_modifier_event(*modifier, true)?;
            std::thread::sleep(Duration::from_millis(20));
        }
        let down = CGEvent::new_keyboard_event(self.source.clone(), keycode, true)
            .map_err(|()| BackendError("failed to create key-down".to_string()))?;
        down.set_flags(flags);
        down.post(CGEventTapLocation::HID);
        std::thread::sleep(Duration::from_millis(30));
        let up = CGEvent::new_keyboard_event(self.source.clone(), keycode, false)
            .map_err(|()| BackendError("failed to create key-up".to_string()))?;
        up.set_flags(flags);
        up.post(CGEventTapLocation::HID);
        // Release the modifiers explicitly — this is the step that clears
        // the held-modifier state so later clicks are plain clicks.
        for modifier in modifiers {
            std::thread::sleep(Duration::from_millis(20));
            self.post_modifier_event(*modifier, false)?;
        }
        Ok(())
    }

    fn activate_app(&self, app_name: &str) -> Result<(), BackendError> {
        // LaunchServices' `open -a` is the canonical way to bring a running
        // app to the front (and launches it if absent). It needs no
        // Accessibility consent and acts on the user's real, already-running
        // app — the opposite of a synthetic bypass (Law 1). The name is
        // passed as a single argv so no shell quoting can inject anything.
        let status = std::process::Command::new("open")
            .args(["-a", app_name])
            .status()
            .map_err(|e| {
                BackendError(format!("failed to launch `open -a {app_name}`: {e}"))
            })?;
        if !status.success() {
            return Err(BackendError(format!(
                "cannot activate app {app_name:?}: `open -a` exited with {:?}. Use \
                 the app's full name as shown in the Dock/Finder (e.g. 'Google \
                 Chrome', not 'Chrome').",
                status.code()
            )));
        }
        // The activated app's window takes a moment to arrive at the front;
        // the OBSERVE probe right after this call should see the app the
        // caller asked for, not the previous frontmost window.
        std::thread::sleep(Duration::from_millis(300));
        Ok(())
    }

    fn type_text(&self, text: &str, wpm: u32) -> Result<(), BackendError> {
        let per_key = keystroke_delay(text.chars().count(), wpm);
        for c in text.chars() {
            // Long pastes take seconds; the kill combo must stop typing
            // immediately rather than finish the whole buffer.
            if crate::hotkey::tripped() {
                return Err(BackendError(
                    "cancelled by user (kill-switch) during typing".to_string(),
                ));
            }
            // Type via the Unicode route so non-ASCII and layout-independent
            // characters work, rather than guessing keycodes per char.
            let down = CGEvent::new_keyboard_event(self.source.clone(), 0, true)
                .map_err(|()| BackendError("failed to create key-down".to_string()))?;
            down.set_string(&String::from(c));
            down.post(CGEventTapLocation::HID);
            let up = CGEvent::new_keyboard_event(self.source.clone(), 0, false)
                .map_err(|()| BackendError("failed to create key-up".to_string()))?;
            up.set_string(&String::from(c));
            up.post(CGEventTapLocation::HID);
            std::thread::sleep(Duration::from_millis(per_key));
        }
        Ok(())
    }

    fn clipboard_paste(&self, text: &str) -> Result<(), BackendError> {
        use std::io::Write;
        use std::process::{Command, Stdio};

        let mut child = Command::new("pbcopy")
            .stdin(Stdio::piped())
            .spawn()
            .map_err(|e| BackendError(format!("failed to spawn pbcopy: {e}")))?;
        if let Some(mut stdin) = child.stdin.take() {
            stdin
                .write_all(text.as_bytes())
                .map_err(|e| BackendError(format!("failed to write to pbcopy: {e}")))?;
        }
        let status = child
            .wait()
            .map_err(|e| BackendError(format!("failed to wait for pbcopy: {e}")))?;
        if !status.success() {
            return Err(BackendError("pbcopy failed to set pasteboard".to_string()));
        }
        std::thread::sleep(Duration::from_millis(50));
        self.hotkey(&[Modifier::Command], "v")
    }

    fn ax_snapshot(&self, pid: u32, max_depth: u8) -> Result<HostElement, BackendError> {
        crate::ax::ax_snapshot(pid, max_depth)
    }

    fn focused_window(&self) -> Result<FocusedWindow, BackendError> {
        crate::ax::focused_window()
    }

    fn capture(&self, display_id: u32) -> Result<CaptureFrame, BackendError> {
        use core_graphics::access::ScreenCaptureAccess;
        use core_graphics::display::CGDisplay;

        // Screen capture is a distinct consent from Accessibility; without it
        // CGDisplayCreateImage returns garbage or nil. Fail loudly (Law 6.3)
        // rather than ship an empty frame the orchestrator would misread as
        // "nothing changed".
        if !ScreenCaptureAccess.preflight() {
            let _ = ScreenCaptureAccess.request();
            return Err(BackendError(
                "Screen Recording consent required. Grant it in System Settings \
                 > Privacy & Security > Screen & System Audio Recording, then \
                 restart the driver."
                    .to_string(),
            ));
        }
        // 0 is the protocol's sentinel for "the main display".
        let display = if display_id == 0 {
            CGDisplay::main()
        } else {
            CGDisplay::new(display_id)
        };
        let image = display
            .image()
            .ok_or_else(|| BackendError(format!("failed to capture display {display_id}")))?;
        let width = image.width();
        let height = image.height();
        let bgra = image_to_bgra(&image, width, height)?;
        // Retina scale: pixels per point, derived from the display's logical
        // bounds. Guards against degenerate 0-height bounds.
        let bounds = display.bounds();
        let scale = if bounds.size.height > 0.0 {
            height as f64 / bounds.size.height
        } else {
            1.0
        };
        Ok(CaptureFrame {
            display_id,
            width: width as u32,
            height: height as u32,
            scale: scale.max(1.0),
            bgra,
        })
    }

    fn is_real(&self) -> bool {
        true
    }
}

/// Convert a CGImage into a top-down BGRA8 buffer.
///
/// A ``CGBitmapContext``'s coordinate origin is its bottom-left corner and
/// ``CGContext::draw_image`` places the image's *bottom* edge at the rect
/// origin — so drawing into ``(0, 0, w, h)`` yields buffer row 0 == image top
/// row. That matches the top-left origin convention of the orchestrator's
/// coordinate layer (``coordinates.py``), keeping region crops consistent
/// between capture and actuation.
fn image_to_bgra(
    image: &core_graphics::image::CGImage,
    width: usize,
    height: usize,
) -> Result<Vec<u8>, BackendError> {
    use core_graphics::base::{kCGImageAlphaPremultipliedFirst, kCGBitmapByteOrder32Little};
    use core_graphics::color_space::CGColorSpace;
    use core_graphics::context::CGContext;
    use core_graphics::geometry::{CGPoint, CGRect, CGSize};

    let bytes_per_row = width * 4;
    let mut buffer = vec![0u8; bytes_per_row * height];
    let color_space = CGColorSpace::create_device_rgb();
    // Alpha-first + 32-bit little-endian is the canonical BGRA byte layout
    // (alpha occupies the high byte of each little-endian 32-bit word).
    let mut context = CGContext::create_bitmap_context(
        Some(buffer.as_mut_ptr() as *mut core::ffi::c_void),
        width,
        height,
        8,
        bytes_per_row,
        &color_space,
        kCGImageAlphaPremultipliedFirst | kCGBitmapByteOrder32Little,
    );
    context.draw_image(
        CGRect::new(
            &CGPoint::new(0.0, 0.0),
            &CGSize::new(width as f64, height as f64),
        ),
        image,
    );
    Ok(context.data().to_vec())
}

/// Maps a printable key name to a virtual keycode for hotkeys (US layout).
/// Type text uses the Unicode route, so this only needs the keys users put in
/// a `press_hotkey`: letters, digits, and the common named keys.
fn keycode_of(key: &str) -> Option<u16> {
    let lower = key.to_lowercase();
    match lower.as_str() {
        "command" | "cmd" => Some(QK::COMMAND),
        "shift" => Some(QK::SHIFT),
        "option" | "alt" => Some(QK::OPTION),
        "control" | "ctrl" => Some(QK::CONTROL),
        "return" | "enter" => Some(QK::RETURN),
        "tab" => Some(QK::TAB),
        "space" | " " => Some(QK::SPACE),
        "delete" | "backspace" => Some(QK::DELETE),
        "escape" | "esc" => Some(QK::ESCAPE),
        "up" => Some(QK::UP_ARROW),
        "down" => Some(QK::DOWN_ARROW),
        "left" => Some(QK::LEFT_ARROW),
        "right" => Some(QK::RIGHT_ARROW),
        "home" => Some(QK::HOME),
        "end" => Some(QK::END),
        "pageup" => Some(QK::PAGE_UP),
        "pagedown" => Some(QK::PAGE_DOWN),
        "a" => Some(QK::ANSI_A),
        "b" => Some(QK::ANSI_B),
        "c" => Some(QK::ANSI_C),
        "d" => Some(QK::ANSI_D),
        "e" => Some(QK::ANSI_E),
        "f" => Some(QK::ANSI_F),
        "g" => Some(QK::ANSI_G),
        "h" => Some(QK::ANSI_H),
        "i" => Some(QK::ANSI_I),
        "j" => Some(QK::ANSI_J),
        "k" => Some(QK::ANSI_K),
        "l" => Some(QK::ANSI_L),
        "m" => Some(QK::ANSI_M),
        "n" => Some(QK::ANSI_N),
        "o" => Some(QK::ANSI_O),
        "p" => Some(QK::ANSI_P),
        "q" => Some(QK::ANSI_Q),
        "r" => Some(QK::ANSI_R),
        "s" => Some(QK::ANSI_S),
        "t" => Some(QK::ANSI_T),
        "u" => Some(QK::ANSI_U),
        "v" => Some(QK::ANSI_V),
        "w" => Some(QK::ANSI_W),
        "x" => Some(QK::ANSI_X),
        "y" => Some(QK::ANSI_Y),
        "z" => Some(QK::ANSI_Z),
        "0" => Some(QK::ANSI_0),
        "1" => Some(QK::ANSI_1),
        "2" => Some(QK::ANSI_2),
        "3" => Some(QK::ANSI_3),
        "4" => Some(QK::ANSI_4),
        "5" => Some(QK::ANSI_5),
        "6" => Some(QK::ANSI_6),
        "7" => Some(QK::ANSI_7),
        "8" => Some(QK::ANSI_8),
        "9" => Some(QK::ANSI_9),
        _ => None,
    }
}

/// Human-like inter-key delay in ms from a target WPM (Law 1 cadence).
fn keystroke_delay(_char_count: usize, wpm: u32) -> u64 {
    // A word is ~5 chars; derive a space between keystrokes, clamped so it
    // reads natural rather than mechanical. A real run adds micro-jitter in
    // the shell, but 20-400ms is a sane human band.
    let wpm = wpm.max(1) as u64;
    (60_000 / (wpm * 5)).clamp(20, 400)
}

