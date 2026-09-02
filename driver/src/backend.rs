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
    /// Top-left corner of this display in the *global* logical point space
    /// (the primary display sits at 0,0; a second display to its right sits
    /// at its width). Actuation coordinates are global, so without this a
    /// frame captured from a secondary display has no way back to the space
    /// the driver clicks in — every coordinate read off it would land on the
    /// primary display instead.
    pub origin_x: f64,
    pub origin_y: f64,
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
    ///
    /// **Localized.** On a Turkish system Calculator reports "Hesap Makinesi".
    /// Never compare a caller-supplied app name against this alone — use
    /// ``bundle_id``, which does not change with the user's language.
    pub app_name: String,
    /// The app's `CFBundleIdentifier` ("com.apple.calculator"), or "" when the
    /// host has no bundle. This is the app's stable identity: unlike
    /// ``app_name`` it is the same in every locale, which is what lets the
    /// orchestrator tell "the user switched apps" apart from "the same app
    /// under its translated name".
    pub bundle_id: String,
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
/// ``SimulatedBackend`` guards its fixture state with a mutex, ``QuartzBackend`` holds only the
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
    /// Photograph the display, or one application's frontmost window when
    /// ``window_pid`` is given.
    ///
    /// The window variant is what lets an agent work in an application the
    /// user has left behind another one: a display capture would show it the
    /// foreground, so it would reason about one window while acting on
    /// another. The returned frame's origin is the window's own, which is what
    /// turns a coordinate read off it back into a point the driver can click.
    fn capture(&self, display_id: u32, window_pid: Option<u32>) -> Result<CaptureFrame, BackendError>;

    /// Returns the app's accessibility tree root (ADR-2 primary source): the
    /// element that *generates* candidate coordinates, which pixels later
    /// verify. ``pid`` is the target application's process id; ``max_depth``
    /// caps the traversal and ``max_nodes`` caps the total serialized nodes
    /// (web-first — page content is visited before chrome, mirroring the
    /// orchestrator's summary ordering) so a pathological app cannot balloon
    /// either the walk time or the response.
    fn ax_snapshot(&self, pid: u32, max_depth: u8, max_nodes: u32) -> Result<HostElement, BackendError>;

    /// Ask the accessibility element under a point to activate itself.
    ///
    /// Returns whether an element accepted the press. This is the *quiet*
    /// actuation path: it addresses one element directly instead of posting a
    /// click into the global event stream, so it neither moves the user's
    /// cursor nor depends on the target being frontmost. Not every element
    /// supports it — a `false` here is an ordinary answer, and the caller
    /// falls back to a synthetic click.
    fn ax_press(&self, pid: u32, point: Point) -> Result<bool, BackendError>;

    /// The pid of a running application, by the name the user would type.
    fn app_pid(&self, app: &str) -> Result<Option<i32>, BackendError>;

    /// Returns the frontmost app, its focused window, and the cursor — the
    /// OBSERVE step's window/cursor half (and the pid that feeds
    /// ``ax_snapshot`` when the caller did not name an app).
    fn focused_window(&self) -> Result<FocusedWindow, BackendError>;

    /// Returns the display names of running applications that own on-screen
    /// windows. Feeds autonomous target-app inference: the orchestrator can
    /// resolve a goal's implied app ("Excel'de aç") against what the user
    /// actually runs, and pick the running browser for web-service goals.
    /// Best-effort by contract — the caller must treat an empty list (or an
    /// error) as "unknown", never as "no apps are running".
    fn list_apps(&self) -> Result<Vec<String>, BackendError>;

    /// Test hook: whether the backend is real (touches the OS) or simulated.
    /// Returns true when a user cancellation has been requested.
    fn is_cancelled(&self, token: &AtomicBool) -> bool {
        token.load(Ordering::Acquire)
    }

    fn is_real(&self) -> bool {
        false
    }
}

/// A small deterministic Safari window used by the simulated backend, built
/// from the current fixture state. All coordinates are absolute global points
/// (matching AX semantics), so the Python side can query "the Reload button"
/// and get a real location to feed the coordinate/verification pipeline; the
/// focus flag and address value follow what the simulated actions did, which
/// is what makes the orchestrator's verification witnesses meaningful offline.
fn simulated_ax_tree(focused: Option<usize>, address_value: &str) -> HostElement {
    let children = FIXTURE_ELEMENTS
        .iter()
        .enumerate()
        .map(|(index, (role, title, x, y, width, height))| {
            let is_address = index == ADDRESS_FIELD_INDEX;
            HostElement {
                role: role.to_string(),
                // The address field's title mirrors its value on a real
                // browser, and the orchestrator's AXValue witness reads the
                // value — so both must follow what was typed or pasted.
                title: if is_address { address_value.to_string() } else { title.to_string() },
                value: if is_address { address_value.to_string() } else { String::new() },
                focused: focused == Some(index),
                x: *x,
                y: *y,
                width: *width,
                height: *height,
                children: Vec::new(),
            }
        })
        .collect::<Vec<_>>();
    let mut window_children = vec![HostElement {
        role: "Toolbar".to_string(),
        title: String::new(),
        value: String::new(),
        focused: false,
        x: 100.0,
        y: 60.0,
        width: 800.0,
        height: 40.0,
        children: Vec::new(),
    }];
    window_children.extend(children);
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
            children: window_children,
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

/// A two-pool node budget for AX snapshots (pure, unit-testable).
///
/// Heavy apps (Chrome with a large page open) expose tens of thousands of AX
/// nodes; walking and serializing all of them costs 0.5-2s plus a megabyte
/// JSON response the orchestrator immediately trims to its own summary
/// budget. The budget caps *total* nodes, and splits them into:
///
/// * ``web`` — page content ``AXWebArea`` subtrees, which the agent actually
///   interacts with (links, buttons, inputs). Gets the lion's share.
/// * ``other`` — native chrome (tab strip, omnibox, toolbar) that must stay
///   reachable too (tab tracking, address-bar paste verification). Guaranteed
///   a floor so a huge page cannot starve it entirely.
///
/// ``spend`` returns whether the caller may emit another node in the given
/// pool; a ``false`` means "stop descending here" — the walker drops the
/// remaining siblings instead of emitting empty leaves.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NodeBudget {
    remaining_web: u32,
    remaining_other: u32,
}

impl NodeBudget {
    /// The web pool gets 3/4 of the budget; chrome gets a guaranteed 1/4
    /// floor so native UI is never starved by a deep page.
    pub fn new(max_nodes: u32) -> Self {
        let web = (max_nodes * 3) / 4;
        Self {
            remaining_web: web,
            remaining_other: max_nodes - web,
        }
    }

    pub fn remaining(&self) -> u32 {
        self.remaining_web + self.remaining_other
    }

    /// Reserve one node from ``in_web`` pool; False when that pool is spent.
    pub fn spend(&mut self, in_web: bool) -> bool {
        if in_web {
            if self.remaining_web == 0 {
                return false;
            }
            self.remaining_web -= 1;
        } else {
            if self.remaining_other == 0 {
                return false;
            }
            self.remaining_other -= 1;
        }
        true
    }
}

/// Order a node's children web-first (pure, stable): ``WebArea`` subtrees
/// (page content) precede chrome siblings, so a node budget spent in
/// traversal order lands on what the agent interacts with. Non-browser
/// subtrees have no ``WebArea`` and keep their natural order.
pub fn order_children_web_first(children: Vec<HostElement>) -> Vec<HostElement> {
    let (mut web, rest): (Vec<_>, Vec<_>) = children
        .into_iter()
        .partition(|child| child.role == "WebArea");
    web.extend(rest);
    web
}

/// Apply a node budget to a tree, web-first, without emitting empty leaves
/// (pure). Mirrors the walker rule: when the budget for a node's pool is
/// spent, the remaining siblings are dropped entirely — an exhausted budget
/// shrinks the payload, it never pads it with stubs (which is what a naive
/// "emit and then stop" recursion would do, wasting the whole point).
///
/// ``in_web`` marks whether ``root`` lives inside a ``WebArea`` already
/// (children inherit it). Used by the simulated backend so the wire round
/// trip exercises the same node-budget contract as the real walker; the
/// Quartz walker applies the identical rule inline during traversal.
pub fn truncate_nodes(root: HostElement, budget: &mut NodeBudget, in_web: bool) -> HostElement {
    let node_is_web = in_web || root.role == "WebArea";
    let mut children = Vec::new();
    for child in order_children_web_first(root.children) {
        let child_is_web = node_is_web || child.role == "WebArea";
        if !budget.spend(child_is_web) {
            break;
        }
        children.push(truncate_nodes(child, budget, node_is_web));
    }
    HostElement {
        children,
        ..root
    }
}

/// Mutable fixture state: the observable consequences of simulated actions.
#[derive(Debug)]
struct SimState {
    /// Index into ``FIXTURE_ELEMENTS`` of the element holding focus.
    focused: Option<usize>,
    /// Current text of the address field (the fixture's only editable value).
    address_value: String,
    /// Which application the fixture reports as frontmost.
    frontmost_app: String,
    /// Where the last click landed, in logical points.
    last_click: Option<(i64, i64)>,
    /// How far the fixture viewport has been scrolled, in points.
    scroll_offset: i64,
}

impl Default for SimState {
    fn default() -> Self {
        SimState {
            // The fixture starts with the address field focused, matching the
            // long-standing AX fixture the Python tests assert against.
            focused: Some(ADDRESS_FIELD_INDEX),
            address_value: "https://example.com".to_string(),
            frontmost_app: "Safari".to_string(),
            last_click: None,
            scroll_offset: 0,
        }
    }
}

/// The fixture's actionable elements: (role, title, x, y, width, height).
/// Coordinates are absolute logical points, inside the simulated display.
const FIXTURE_ELEMENTS: [(&str, &str, f64, f64, f64, f64); 4] = [
    ("Button", "Back", 120.0, 68.0, 44.0, 24.0),
    ("Button", "Forward", 176.0, 68.0, 44.0, 24.0),
    ("Button", "Reload", 232.0, 68.0, 44.0, 24.0),
    ("TextField", "address", 320.0, 68.0, 400.0, 24.0),
];
const ADDRESS_FIELD_INDEX: usize = 3;

/// Simulated actuation backend: never touches the host, but *does* model what
/// each action would make observable.
///
/// The previous version only logged and ACKed, which made it useless for the
/// property that matters most: the closed OBSERVE -> ACT -> VERIFY loop. With
/// a fixture that never changed, every verified action looked like a miss and
/// every "did the screen move?" question answered no — so the loop's own
/// self-correction could only ever be exercised against a real display, i.e.
/// not in CI at all. Modelling focus, text entry, activation, scrolling and a
/// click marker in the frame is what lets the whole cycle run offline and
/// still mean something. It remains a fixture, not an emulator: it models
/// consequences the orchestrator observes, nothing more (Law 1 still holds —
/// no host API is touched).
#[derive(Debug, Default)]
pub struct SimulatedBackend {
    state: std::sync::Mutex<SimState>,
}

impl SimulatedBackend {
    /// Borrow the fixture state, recovering from a poisoned lock.
    ///
    /// A panic in another connection thread must not turn every later request
    /// into an error: the fixture holds no invariant a panic could corrupt.
    fn state(&self) -> std::sync::MutexGuard<'_, SimState> {
        self.state.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Index of the fixture element containing a point, if any (pure).
    fn element_at(point: Point) -> Option<usize> {
        let (x, y) = (point.x as f64, point.y as f64);
        FIXTURE_ELEMENTS.iter().position(|(_, _, ex, ey, ew, eh)| {
            x >= *ex && x < ex + ew && y >= *ey && y < ey + eh
        })
    }
}

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
        let mut state = self.state();
        // A click moves keyboard focus and leaves a visible mark — the two
        // signals the orchestrator's witnesses actually read.
        state.focused = Self::element_at(at);
        state.last_click = Some((at.x, at.y));
        Ok(())
    }

    fn drag(&self, from: Point, to: Point, duration_ms: u64) -> Result<(), BackendError> {
        eprintln!(
            "[sim] drag ({},{})->({},{}) over {duration_ms}ms",
            from.x, from.y, to.x, to.y
        );
        let mut state = self.state();
        state.last_click = Some((to.x, to.y));
        Ok(())
    }

    fn scroll(&self, dx: i64, dy: i64) -> Result<(), BackendError> {
        eprintln!("[sim] scroll dx={dx} dy={dy}");
        let mut state = self.state();
        state.scroll_offset += dy;
        Ok(())
    }

    fn hotkey(&self, modifiers: &[Modifier], key: &str) -> Result<(), BackendError> {
        eprintln!("[sim] hotkey {:?} + {}", modifiers, key);
        let mut state = self.state();
        // Cmd+L focuses and selects the address bar, exactly as the prompt's
        // navigation recipe assumes; the next paste therefore replaces it.
        if modifiers == [Modifier::Command] && key.eq_ignore_ascii_case("l") {
            state.focused = Some(ADDRESS_FIELD_INDEX);
        }
        Ok(())
    }

    fn activate_app(&self, app_name: &str) -> Result<(), BackendError> {
        eprintln!("[sim] activate_app {app_name}");
        let mut state = self.state();
        state.frontmost_app = app_name.to_string();
        Ok(())
    }

    fn type_text(&self, text: &str, wpm: u32) -> Result<(), BackendError> {
        eprintln!("[sim] type_text {:?} at {wpm}wpm", text);
        let mut state = self.state();
        if state.focused == Some(ADDRESS_FIELD_INDEX) {
            state.address_value.push_str(text);
        }
        Ok(())
    }

    fn clipboard_paste(&self, text: &str) -> Result<(), BackendError> {
        eprintln!("[sim] clipboard_paste {:?}", text);
        let mut state = self.state();
        if state.focused == Some(ADDRESS_FIELD_INDEX) {
            // Paste-over-selection: the recipe selects the field with Cmd+L
            // first, so the pasted text replaces rather than appends.
            state.address_value = text.to_string();
        }
        Ok(())
    }

    fn app_pid(&self, app: &str) -> Result<Option<i32>, BackendError> {
        // The fixture app answers with the same pid its AX tree uses.
        Ok((app == self.state().frontmost_app).then_some(4242))
    }

    fn ax_press(&self, _pid: u32, point: Point) -> Result<bool, BackendError> {
        // The fixture models the same consequence a real press has — focus
        // moves to whatever sits under the point — so the closed loop can be
        // exercised offline. An inert stub would make every pressed element
        // look like a miss to the verification layer.
        let hit = Self::element_at(point).is_some();
        if hit {
            self.click(point, Button::Left, 1)?;
        }
        Ok(hit)
    }

    fn ax_snapshot(&self, _pid: u32, max_depth: u8, max_nodes: u32) -> Result<HostElement, BackendError> {
        // Same deterministic Safari fixture regardless of pid, reflecting the
        // current focus and address value so an orchestrator can *verify* the
        // effect of what it just did. Apply the depth cap first, then the node
        // budget (web-first), matching the real backend's wire contract.
        let state = self.state();
        let root = truncate_depth(
            simulated_ax_tree(state.focused, &state.address_value),
            max_depth,
        );
        let mut budget = NodeBudget::new(max_nodes);
        Ok(truncate_nodes(root, &mut budget, false))
    }

    fn focused_window(&self) -> Result<FocusedWindow, BackendError> {
        // Deterministic frontmost-app fixture, pid consistent with the AX tree
        // (4242) so a caller can discover the pid and snapshot the same app.
        // The app name follows ``activate_app`` so a focus guard can observe
        // an activation actually taking effect.
        let state = self.state();
        Ok(FocusedWindow {
            pid: 4242,
            // A synthetic but well-formed bundle id, so tests exercise the
            // same identity path the real backend uses.
            bundle_id: format!(
                "com.simulated.{}",
                state.frontmost_app.to_lowercase().replace(' ', "-")
            ),
            app_name: state.frontmost_app.clone(),
            window_title: "GitHub — computeruse".to_string(),
            cursor_x: 420.0,
            cursor_y: 300.0,
        })
    }

    fn list_apps(&self) -> Result<Vec<String>, BackendError> {
        // Deterministic fixture matching the AX tree's app; a real host
        // reports its actual running apps (Law 1: the simulated backend
        // never inspects the host).
        Ok(vec!["Safari".to_string(), "Google Chrome".to_string()])
    }

    fn capture(&self, display_id: u32, _window_pid: Option<u32>) -> Result<CaptureFrame, BackendError> {
        // Deterministic checkerboard on a frame that actually *contains* this
        // backend's own AX fixture. The frame used to be 64x36 at scale 2.0 —
        // a 32x18 logical "display" holding a Safari window at (100,60) 800x600.
        // Nothing in the simulated stack was self-consistent: the fail-closed
        // bounds check rejected every AX-grounded coordinate as off-screen, so
        // the simulated backend could not rehearse the one path that matters
        // most (AX generates a coordinate -> the gate converts it -> the driver
        // actuates it). 1024x768 at scale 1.0 contains the fixture window, and
        // maps to the model's 512px screenshot with an exact factor of 2.0 —
        // a round number that keeps the coordinate round-trip readable in tests.
        const WIDTH: u32 = 1024;
        const HEIGHT: u32 = 768;
        const BLOCK: u32 = 8;
        // A click paints a marker large enough to clear the pixel witness's
        // change threshold inside its 48pt inspection region; scrolling shifts
        // the whole checkerboard. Without these the frame was constant, so the
        // pixel witness could only ever say "nothing happened".
        const MARKER: i64 = 40;
        let (last_click, scroll_offset) = {
            let state = self.state();
            (state.last_click, state.scroll_offset)
        };
        let mut bgra = Vec::with_capacity((WIDTH * HEIGHT * 4) as usize);
        for y in 0..HEIGHT {
            for x in 0..WIDTH {
                let marked = last_click.is_some_and(|(cx, cy)| {
                    (x as i64 - cx).abs() < MARKER && (y as i64 - cy).abs() < MARKER
                });
                let shifted_y = (y as i64 + scroll_offset).rem_euclid(HEIGHT as i64) as u32;
                let white = !marked && ((x / BLOCK) + (shifted_y / BLOCK)).is_multiple_of(2);
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
            scale: 1.0,
            // The simulated host is a single display at the global origin.
            origin_x: 0.0,
            origin_y: 0.0,
            bgra,
        })
    }
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;

    #[test]
    fn simulated_backend_never_touches_os() {
        let backend = SimulatedBackend::default();
        assert!(!backend.is_real());
        assert!(
            backend
                .move_along(&[(crate::bezier::point(5, 5), Duration::from_millis(1))])
                .is_ok()
        );
    }

    #[test]
    fn simulated_activate_app_acks_without_host_side_effects() {
        let backend = SimulatedBackend::default();
        // App activation is a host side effect; the simulated backend must
        // ACK (the wire path stays exercisable) while never running `open`.
        assert!(backend.activate_app("Google Chrome").is_ok());
    }

    #[test]
    fn simulated_ax_tree_is_deterministic_and_depth_capped() {
        let root = simulated_ax_tree(Some(ADDRESS_FIELD_INDEX), "https://example.com");
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
        let shallow = truncate_depth(
            simulated_ax_tree(Some(ADDRESS_FIELD_INDEX), "https://example.com"),
            1,
        );
        assert_eq!(shallow.children[0].children.len(), 0);
    }

    #[test]
    fn simulated_click_moves_focus_and_marks_the_frame() {
        // The fixture models the two signals the orchestrator's verification
        // witnesses read: AX focus and pixels. Without them the closed
        // OBSERVE -> ACT -> VERIFY loop could not be exercised offline at all.
        let backend = SimulatedBackend::default();
        let before = backend.capture(0, None).expect("sim capture works");
        backend
            .click(crate::bezier::point(254, 80), Button::Left, 1)
            .expect("sim click works");
        let after = backend.capture(0, None).expect("sim capture works");
        assert_ne!(before.bgra, after.bgra, "a click must change the frame");

        let root = backend.ax_snapshot(4242, 8, 4096).expect("sim ax works");
        let window = &root.children[0];
        let reload = window.children.iter().find(|e| e.title == "Reload").unwrap();
        assert!(reload.focused, "clicking the Reload button must focus it");
    }

    #[test]
    fn simulated_paste_lands_in_the_focused_field() {
        let backend = SimulatedBackend::default();
        backend
            .hotkey(&[Modifier::Command], "l")
            .expect("sim hotkey works");
        backend
            .clipboard_paste("https://github.com")
            .expect("sim paste works");
        let root = backend.ax_snapshot(4242, 8, 4096).expect("sim ax works");
        let window = &root.children[0];
        let field = window.children.iter().find(|e| e.role == "TextField").unwrap();
        assert_eq!(field.value, "https://github.com");
    }

    #[test]
    fn simulated_activation_changes_the_frontmost_app() {
        let backend = SimulatedBackend::default();
        backend.activate_app("Google Chrome").expect("sim activate works");
        let focused = backend.focused_window().expect("sim focused window works");
        assert_eq!(focused.app_name, "Google Chrome");
    }

    #[test]
    fn simulated_focused_window_is_deterministic_and_consistent() {
        let backend = SimulatedBackend::default();
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
        let backend = SimulatedBackend::default();
        let a = backend.capture(0, None).expect("sim capture works");
        let b = backend.capture(0, None).expect("sim capture works");
        // Same frame every time — the OBSERVE step can rely on stability.
        assert_eq!(a.bgra, b.bgra);
        assert_eq!(a.bgra.len(), (a.width * a.height * 4) as usize);
        // The frame must contain the backend's own AX fixture window
        // (100,60 -> 900,660 in logical points), or the orchestrator's
        // fail-closed bounds check rejects every grounded coordinate.
        assert_eq!((a.width, a.height), (1024, 768));
        assert_eq!(a.scale, 1.0);
        let window = simulated_ax_tree(None, "").children[0].clone();
        assert!(window.x + window.width <= a.width as f64 / a.scale);
        assert!(window.y + window.height <= a.height as f64 / a.scale);
        // Top-left 8x8 block is white (BGRA: all 255) — checkerboard invariant
        // that the Python end-to-end test asserts through the luma path.
        assert_eq!(&a.bgra[..4], &[255, 255, 255, 255]);
        // Block right below it (y in [8,16)) is black.
        let row_start = 8 * a.width as usize * 4;
        assert_eq!(&a.bgra[row_start..row_start + 4], &[0, 0, 0, 255]);
    }
}

#[cfg(test)]
mod node_budget_tests {
    // Pure node-budget maths is platform-independent, so it runs on any CI
    // host (the macOS-gated module above is only for backends).
    use super::*;

    fn leaf(role: &str, title: &str) -> HostElement {
        HostElement {
            role: role.to_string(),
            title: title.to_string(),
            value: String::new(),
            focused: false,
            x: 0.0,
            y: 0.0,
            width: 0.0,
            height: 0.0,
            children: Vec::new(),
        }
    }

    #[test]
    fn budget_web_pool_is_three_quarters() {
        let budget = NodeBudget::new(400);
        assert_eq!(budget.remaining(), 400);
        assert_eq!(budget.remaining_web, 300);
        assert_eq!(budget.remaining_other, 100);
    }

    #[test]
    fn budget_spend_tracks_per_pool() {
        let mut budget = NodeBudget::new(400);
        assert!(budget.spend(true));
        assert_eq!(budget.remaining_web, 299);
        assert!(budget.spend(false));
        assert_eq!(budget.remaining_other, 99);
        // Exhaust the chrome pool: spending there returns false.
        for _ in 0..99 {
            assert!(budget.spend(false));
        }
        assert!(!budget.spend(false));
        // The web pool is untouched and still spendable.
        assert!(budget.spend(true));
    }

    #[test]
    fn web_first_ordering_puts_page_before_chrome() {
        let children = vec![
            leaf("Toolbar", ""),
            leaf("WebArea", ""),
            leaf("Button", "Reload"),
        ];
        let ordered = order_children_web_first(children);
        assert_eq!(ordered[0].role, "WebArea");
        // The rest keep their relative order.
        assert_eq!(ordered[1].role, "Toolbar");
        assert_eq!(ordered[2].role, "Button");
    }

    #[test]
    fn truncate_nodes_bounds_total_nodes_web_first() {
        // Window with chrome (Toolbar + 3 Buttons) then a WebArea with 50 links.
        let page_links: Vec<HostElement> = (0..50)
            .map(|i| leaf("Link", &format!("result {i}")))
            .collect();
        let window = HostElement {
            role: "Window".to_string(),
            title: String::new(),
            value: String::new(),
            focused: false,
            x: 0.0,
            y: 0.0,
            width: 0.0,
            height: 0.0,
            children: vec![
                leaf("Toolbar", ""),
                leaf("Button", "Back"),
                leaf("Button", "Forward"),
                leaf("Button", "Reload"),
                HostElement {
                    role: "WebArea".to_string(),
                    title: String::new(),
                    value: String::new(),
                    focused: false,
                    x: 0.0,
                    y: 0.0,
                    width: 0.0,
                    height: 0.0,
                    children: page_links,
                },
            ],
        };
        let mut budget = NodeBudget::new(16); // web 12, chrome 4
        let root = truncate_nodes(window, &mut budget, false);
        // Budget accounting: the root (window) itself is free; every
        // descendant spends one slot from its pool. Chrome = Toolbar + 3
        // buttons = 4 slots; the WebArea consumes the 12 web slots (1 for
        // the WebArea node, 11 of its 50 links). +1 free root = 17 nodes.
        assert_eq!(count_nodes(&root), 17);
        let web = find_role(&root, "WebArea");
        assert!(web.is_some(), "web area must survive the budget");
        // With 12 web slots (1 spent on the WebArea itself), 11 links survive.
        assert_eq!(repr_of(web.unwrap()).children.len(), 11);
        // Chrome survived its own pool: the Reload button is present.
        assert!(find_title(&root, "Reload").is_some());
    }

    #[test]
    fn truncate_nodes_with_tiny_budget_stops_early_no_stubs() {
        let window = HostElement {
            role: "Window".to_string(),
            title: String::new(),
            value: String::new(),
            focused: false,
            x: 0.0,
            y: 0.0,
            width: 0.0,
            height: 0.0,
            children: (0..10).map(|i| leaf("Button", &format!("b{i}"))).collect(),
        };
        // Chrome pool = 6 slots for max_nodes=24 (web gets 3/4). The root is
        // free; the first 6 buttons spend 6 and the rest are dropped — no
        // empty stubs padded into the payload for the exhausted pool.
        let mut budget = NodeBudget::new(24);
        let root = truncate_nodes(window, &mut budget, false);
        assert_eq!(root.children.len(), 6, "exhausted budget drops remaining siblings");
        assert_eq!(root.children[0].title, "b0");
        assert_eq!(root.children[5].title, "b5");
    }

    fn count_nodes(node: &HostElement) -> usize {
        1 + node.children.iter().map(count_nodes).sum::<usize>()
    }

    fn find_role<'a>(node: &'a HostElement, role: &str) -> Option<&'a HostElement> {
        if node.role == role {
            return Some(node);
        }
        node.children.iter().find_map(|c| find_role(c, role))
    }

    fn find_title<'a>(node: &'a HostElement, title: &str) -> Option<&'a HostElement> {
        if node.title == title {
            return Some(node);
        }
        node.children.iter().find_map(|c| find_title(c, title))
    }

    fn repr_of(node: &HostElement) -> &HostElement {
        node
    }
}