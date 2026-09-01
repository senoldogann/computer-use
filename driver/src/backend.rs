//! Abstraction over the physical input stream.
//!
//! Per Law 6, the OS connector is a trait: the orchestrator hands a backend a
//! *planned trajectory* (pure math, testable), and the backend owns the
//! side effects — moving the real cursor, clicking, typing, waiting. Two
//! implementations exist:
//!
//! * :struct:`SimulatedBackend` — logs what it would do and ACKs. This is the
//!   default, so unit tests and CI never touch a real mouse/keyboard.
//! * :struct:`QuartzBackend` (macOS only) — posts real CGEvent to the system.
//!
//! Keeping the trajectory as the trait boundary means the two backends are
//! behaviourally identical from the orchestrator's view; only the physical
//! effect differs.

use core::time::Duration;

use serde::Serialize;
use std::sync::atomic::{AtomicBool, Ordering};

use crate::bezier::Point;

/// Mouse button selection, decoupled from OS button codes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Button {
    Left,
    Right,
    Middle,
}

/// Modifier keys, decoupled from OS flag bits.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Modifier {
    Command,
    Shift,
    Alt,
    Control,
}

/// A single step of a prepared cursor path: an absolute point plus the pause
/// before the next move (derived from the ease-in/out profile).
pub type TrajectoryStep = (Point, Duration);

/// A raw display snapshot: BGRA8 pixels plus the geometry needed to map it
/// back into the orchestrator's logical coordinate space (ADR-2).
///
/// The orchestrator never sees a bitmap in its working state — it decodes this
/// into luma, diffs regions, and keeps only the aggregated verdict. The frame
/// itself is a transient, point-in-time capture.
pub struct CaptureFrame {
    /// Which display was captured; ``0`` means the main display.
    pub display_id: u32,
    /// Pixel width of the frame.
    pub width: u32,
    /// Pixel height of the frame.
    pub height: u32,
    /// Physical pixels per logical point for this display (Retina == 2.0).
    /// Needed to translate global logical coordinates into pixel offsets.
    pub scale: f64,
    /// BGRA8, row-major, top-down; length must equal ``width * height * 4``.
    pub bgra: Vec<u8>,
}

/// The frontmost application, its focused window, and the cursor position.
///
/// §5's OBSERVE step reads two things besides pixels: the *active window
/// title* and the *cursor position*. Both come from this one RPC (system-wide
/// AX + a probe CGEvent on the real backend; a deterministic Safari fixture on
/// the simulated one), so the orchestrator knows what it is looking at without
/// being told.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct FocusedWindow {
    /// Process id of the frontmost application (feeds ``ax_snapshot``).
    pub pid: i32,
    /// Application name (its AXTitle; may be empty for odd hosts).
    pub app_name: String,
    /// Title of the focused window inside that app ("" when none).
    pub window_title: String,
    /// Cursor position in global logical points (Y grows down).
    pub cursor_x: f64,
    pub cursor_y: f64,
}

/// One element of the host's accessibility tree (ADR-2 primary source).
///
/// Coordinates are the same global logical point space the orchestrator's
/// coordinate layer uses (Quartz global display space: origin at the primary
/// display's top-left, Y grows down), so an element's rect feeds directly into
/// ``verification_region``/``crop`` without any transform.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct HostElement {
    /// AX role without the ``AX`` prefix (e.g. ``Button``, ``Window``).
    pub role: String,
    pub title: String,
    /// AXValue: the element's current text content (text fields, sliders).
    /// Lets the orchestrator verify that typed/pasted text actually landed in
    /// the focused input (ADR-2: AX is the state source, pixels verify).
    pub value: String,
    /// Whether this element currently holds keyboard focus (``AXFocused``).
    /// Lets the provider confirm that a click landed — the address bar
    /// reports focused after being clicked — without needing Screen
    /// Recording consent (ADR-2: AX is the state source, pixels verify).
    pub focused: bool,
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
    pub children: Vec<HostElement>,
}

impl std::fmt::Debug for CaptureFrame {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Don't dump megabytes of pixels into a debug log.
        f.debug_struct("CaptureFrame")
            .field("display_id", &self.display_id)
            .field("width", &self.width)
            .field("height", &self.height)
            .field("scale", &self.scale)
            .field("bgra_bytes", &self.bgra.len())
            .finish()
    }
}

/// Error raised by a backend. Carries a human-readable message so the
/// orchestrator can log structured diagnostics (Law 6.3).
#[derive(Debug)]
pub struct BackendError(pub String);

impl std::fmt::Display for BackendError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// The physical input connector (Law 6: class/trait reserved for OS drivers).
///
/// ``Send + Sync`` is part of the contract: the driver serves one thread per
/// connection (F4), so a backend must be shareable across those threads
/// (``Arc<dyn Backend>``). Both shipped implementations satisfy it —
/// ``SimulatedBackend`` is stateless, ``QuartzBackend`` holds only the
/// ``CGEventSource`` which is ``Sync``.
pub trait Backend: Send + Sync {
    /// Returns the current cursor position in virtual pixels.
    fn current_position(&self) -> Result<Point, BackendError>;

    /// Moves the cursor along a pre-planned trajectory, pausing per step.
    fn move_along(&self, steps: &[TrajectoryStep]) -> Result<(), BackendError>;

    /// Clicks at a point with a given button and click count.
    fn click(&self, at: Point, button: Button, click_count: u8) -> Result<(), BackendError>;

    /// Press-drag-release from one point to another (a "mouse_drag").
    /// ``duration_ms`` is the requested total drag time; the backend may
    /// stretch it for long distances (``human_move_duration``) so a drag
    /// reads as a continuous hand motion, never a teleport (Law 1).
    fn drag(&self, from: Point, to: Point, duration_ms: u64) -> Result<(), BackendError>;

    /// Scrolls by a horizontal/vertical delta.
    fn scroll(&self, dx: i64, dy: i64) -> Result<(), BackendError>;

    /// Presses key down/up with the given modifiers (a "press_hotkey").
    fn hotkey(&self, modifiers: &[Modifier], key: &str) -> Result<(), BackendError>;

    /// Brings a named application to the front (e.g. ``open -a`` on macOS).
    ///
    /// This is an OBSERVE *precondition*: when the caller names a target app,
    /// the run must act on that app's foreground window — not the window that
    /// happened to be frontmost (typically the terminal the CLI was launched
    /// from). The simulated backend only logs and ACKs (Law 1: it never
    /// touches the host); the real backend may take a moment for the app to
    /// come forward, so callers should not assume the frontmost app changed
    /// synchronously with the return.
    fn activate_app(&self, app_name: &str) -> Result<(), BackendError>;

    /// Types text with human-like inter-key delays (wpm cadence).
    fn type_text(&self, text: &str, wpm: u32) -> Result<(), BackendError>;

    /// Pastes text via clipboard (Cmd+V on macOS).
    fn clipboard_paste(&self, text: &str) -> Result<(), BackendError>;

    /// Captures the display's current pixels so the orchestrator can verify
    /// actions visually (ADR-2: pixels as *verifier*, not generator).
    /// ``display_id == 0`` means the main display. Diffing two frames is the
    /// orchestrator's job; this method only returns the raw snapshot.
    fn capture(&self, display_id: u32) -> Result<CaptureFrame, BackendError>;

    /// Returns the app's accessibility tree root (ADR-2 primary source): the
    /// element that *generates* candidate coordinates, which pixels later
    /// verify. ``pid`` is the target application's process id; ``max_depth``
    /// caps the traversal so a pathological app cannot balloon the response.
    fn ax_snapshot(&self, pid: u32, max_depth: u8) -> Result<HostElement, BackendError>;

    /// Returns the frontmost app, its focused window, and the cursor — the
    /// OBSERVE step's window/cursor half (and the pid that feeds
    /// ``ax_snapshot`` when the caller did not name an app).
    fn focused_window(&self) -> Result<FocusedWindow, BackendError>;

    /// Test hook: whether the backend is real (touches the OS) or simulated.
    /// Returns true when a user cancellation has been requested.
    fn is_cancelled(&self, token: &AtomicBool) -> bool {
        token.load(Ordering::Acquire)
    }

    fn is_real(&self) -> bool {
        false
    }
}

/// A small deterministic Safari window used by the simulated backend. All
/// coordinates are absolute global points (matching AX semantics), so the
/// Python side can query "the Reload button" and get a real location to
/// feed the coordinate/verification pipeline.
fn simulated_ax_tree() -> HostElement {
    fn element(
        role: &str,
        title: &str,
        focused: bool,
        x: f64,
        y: f64,
        width: f64,
        height: f64,
    ) -> HostElement {
        HostElement {
            role: role.to_string(),
            title: title.to_string(),
            value: String::new(),
            focused,
            x,
            y,
            width,
            height,
            children: Vec::new(),
        }
    }
    HostElement {
        role: "Application".to_string(),
        title: "Safari".to_string(),
        value: String::new(),
        focused: false,
        x: 0.0,
        y: 0.0,
        width: 0.0,
        height: 0.0,
        children: vec![HostElement {
            role: "Window".to_string(),
            title: "Safari".to_string(),
            value: String::new(),
            focused: false,
            x: 100.0,
            y: 60.0,
            width: 800.0,
            height: 600.0,
            children: vec![
                element("Toolbar", "", false, 100.0, 60.0, 800.0, 40.0),
                element("Button", "Back", false, 120.0, 68.0, 44.0, 24.0),
                element("Button", "Forward", false, 176.0, 68.0, 44.0, 24.0),
                element("Button", "Reload", false, 232.0, 68.0, 44.0, 24.0),
                // The address field holds focus in the fixture and its value
                // mirrors the address text — the consent-free "typing/paste
                // landed" signal is deterministic in tests.
                HostElement {
                    value: "https://example.com".to_string(),
                    ..element("TextField", "https://example.com", true, 320.0, 68.0, 400.0, 24.0)
                },
            ],
        }],
    }
}

/// Cut a tree at ``max_depth`` (depth 0 == the root only). Kept pure and
/// separate from the traversal so the simulated backend and the real Quartz
/// traversal both apply the same budget rule.
pub fn truncate_depth(root: HostElement, max_depth: u8) -> HostElement {
    if max_depth == 0 {
        return HostElement {
            children: Vec::new(),
            ..root
        };
    }
    HostElement {
        children: root
            .children
            .into_iter()
            .map(|child| truncate_depth(child, max_depth - 1))
            .collect(),
        ..root
    }
}

/// Default backend: logs planned actuation and ACKs.
///
/// Prevents accidental real input during development/tests while still
/// exercising the full orchestration code path (Law 1's *no accidental
/// actuation* guard at the driver level).
#[derive(Debug)]
pub struct SimulatedBackend;

impl Backend for SimulatedBackend {
    fn current_position(&self) -> Result<Point, BackendError> {
        Ok(crate::bezier::point(0, 0))
    }

    fn move_along(&self, steps: &[TrajectoryStep]) -> Result<(), BackendError> {
        match steps.last() {
            Some((end, _)) => eprintln!(
                "[sim] move_along {} steps -> ({},{})",
                steps.len(),
                end.x,
                end.y
            ),
            None => eprintln!("[sim] move_along <empty>"),
        }
        Ok(())
    }

    fn click(&self, at: Point, button: Button, click_count: u8) -> Result<(), BackendError> {
        eprintln!("[sim] click ({},{}) {:?} x{}", at.x, at.y, button, click_count);
        Ok(())
    }

    fn drag(&self, from: Point, to: Point, duration_ms: u64) -> Result<(), BackendError> {
        eprintln!(
            "[sim] drag ({},{})->({},{}) over {duration_ms}ms",
            from.x, from.y, to.x, to.y
        );
        Ok(())
    }

    fn scroll(&self, dx: i64, dy: i64) -> Result<(), BackendError> {
        eprintln!("[sim] scroll dx={dx} dy={dy}");
        Ok(())
    }

    fn hotkey(&self, modifiers: &[Modifier], key: &str) -> Result<(), BackendError> {
        eprintln!("[sim] hotkey {:?} + {}", modifiers, key);
        Ok(())
    }

    fn activate_app(&self, app_name: &str) -> Result<(), BackendError> {
        // Law 1: a simulated backend never touches the host, so it logs the
        // activation the real backend would perform and ACKs.
        eprintln!("[sim] activate_app {app_name}");
        Ok(())
    }

    fn type_text(&self, text: &str, wpm: u32) -> Result<(), BackendError> {
        eprintln!("[sim] type_text {:?} at {wpm}wpm", text);
        Ok(())
    }

    fn clipboard_paste(&self, text: &str) -> Result<(), BackendError> {
        eprintln!("[sim] clipboard_paste {:?}", text);
        Ok(())
    }

    fn ax_snapshot(&self, _pid: u32, max_depth: u8) -> Result<HostElement, BackendError> {
        // Same deterministic Safari fixture regardless of pid: tests query it
        // through the real driver socket exactly as they would a live app.
        Ok(truncate_depth(simulated_ax_tree(), max_depth))
    }

    fn focused_window(&self) -> Result<FocusedWindow, BackendError> {
        // Deterministic frontmost-app fixture, pid consistent with the AX tree
        // (4242) so a caller can discover the pid and snapshot the same app.
        Ok(FocusedWindow {
            pid: 4242,
            app_name: "Safari".to_string(),
            window_title: "GitHub — computeruse".to_string(),
            cursor_x: 420.0,
            cursor_y: 300.0,
        })
    }

    fn capture(&self, display_id: u32) -> Result<CaptureFrame, BackendError> {
        // Deterministic 8x8 checkerboard (64x36 px, scale 2.0). Tests can rely
        // on exact luma values: every block is uniformly white or black, two
        // consecutive captures are identical, and editing one block changes
        // exactly that region — the whole OBSERVE→ORIENT path is exercisable
        // without a real display (Law 1: no accidental real capture).
        const WIDTH: u32 = 64;
        const HEIGHT: u32 = 36;
        const BLOCK: u32 = 8;
        let mut bgra = Vec::with_capacity((WIDTH * HEIGHT * 4) as usize);
        for y in 0..HEIGHT {
            for x in 0..WIDTH {
                let white = ((x / BLOCK) + (y / BLOCK)).is_multiple_of(2);
                // BGRA byte order: blue, green, red, alpha.
                bgra.extend_from_slice(if white {
                    &[255, 255, 255, 255]
                } else {
                    &[0, 0, 0, 255]
                });
            }
        }
        Ok(CaptureFrame {
            display_id,
            width: WIDTH,
            height: HEIGHT,
            scale: 2.0,
            bgra,
        })
    }
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;

    #[test]
    fn simulated_backend_never_touches_os() {
        let backend = SimulatedBackend;
        assert!(!backend.is_real());
        assert!(
            backend
                .move_along(&[(crate::bezier::point(5, 5), Duration::from_millis(1))])
                .is_ok()
        );
    }

    #[test]
    fn simulated_activate_app_acks_without_host_side_effects() {
        let backend = SimulatedBackend;
        // App activation is a host side effect; the simulated backend must
        // ACK (the wire path stays exercisable) while never running `open`.
        assert!(backend.activate_app("Google Chrome").is_ok());
    }

    #[test]
    fn simulated_ax_tree_is_deterministic_and_depth_capped() {
        let root = simulated_ax_tree();
        assert_eq!(root.role, "Application");
        assert_eq!(root.title, "Safari");
        let window = &root.children[0];
        assert_eq!(window.role, "Window");
        assert_eq!(window.children.len(), 5);
        // Reload button at a known absolute location.
        let reload = window.children.iter().find(|e| e.title == "Reload").unwrap();
        assert_eq!((reload.x, reload.y), (232.0, 68.0));
        // The address field reports focused — the consent-free "click landed"
        // signal the provider uses to confirm a click (ADR-2 state source).
        let field = window.children.iter().find(|e| e.role == "TextField").unwrap();
        assert!(field.focused);
        assert!(!reload.focused);
        // Depth 1 keeps the window but drops its descendants.
        let shallow = truncate_depth(simulated_ax_tree(), 1);
        assert_eq!(shallow.children[0].children.len(), 0);
    }

    #[test]
    fn simulated_focused_window_is_deterministic_and_consistent() {
        let backend = SimulatedBackend;
        let a = backend.focused_window().expect("sim focused window works");
        let b = backend.focused_window().expect("sim focused window works");
        // Same perception every call — the OBSERVE step can rely on stability.
        assert_eq!(a, b);
        assert_eq!(a.pid, 4242);
        assert_eq!(a.app_name, "Safari");
        assert_eq!(a.window_title, "GitHub — computeruse");
        assert_eq!((a.cursor_x, a.cursor_y), (420.0, 300.0));
    }

    #[test]
    fn simulated_capture_is_deterministic_and_sized() {
        let backend = SimulatedBackend;
        let a = backend.capture(0).expect("sim capture works");
        let b = backend.capture(0).expect("sim capture works");
        // Same frame every time — the OBSERVE step can rely on stability.
        assert_eq!(a.bgra, b.bgra);
        assert_eq!(a.bgra.len(), (a.width * a.height * 4) as usize);
        assert_eq!((a.width, a.height), (64, 36));
        // Top-left 8x8 block is white (BGRA: all 255) — checkerboard invariant
        // that the Python end-to-end test asserts through the luma path.
        assert_eq!(&a.bgra[..4], &[255, 255, 255, 255]);
        // Block right below it (y in [8,16)) is black.
        let row_start = 8 * a.width as usize * 4;
        assert_eq!(&a.bgra[row_start..row_start + 4], &[0, 0, 0, 255]);
    }
}