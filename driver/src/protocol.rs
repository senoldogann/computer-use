//! Wire protocol shared between the orchestrator and the driver.
//!
//! Requests mirror the action contract from the Python orchestrator; the driver
//! only responds to well-formed JSON payloads so a malformed message is
//! rejected before it reaches the physical stack (Law 2).

use serde::{Deserialize, Serialize};

use crate::backend::HostElement;

/// Versioned request envelope. Every method carries an explicit `method`, and
/// typed params are validated at parse time.
#[derive(Debug, Deserialize)]
#[serde(tag = "method", content = "params", rename_all = "snake_case")]
pub enum Request {
    Ping,
    Health,
    FocusedWindow,
    ListApps,
    HotkeyState,
    MouseMove(MouseMoveParams),
    MouseClick(MouseClickParams),
    MouseDrag(MouseDragParams),
    MouseScroll(MouseScrollParams),
    PressHotkey(PressHotkeyParams),
    TypeText(TypeTextParams),
    Screenshot(ScreenshotParams),
    AxSnapshot(AxSnapshotParams),
    AxPress(AxPressParams),
    IdleSeconds,
    AxSetValue(AxSetValueParams),
    AppPid(AppPidParams),
    ActivateApp(ActivateAppParams),
    ClipboardPaste(ClipboardPasteParams),
}

/// Ask the element *under a point* to activate itself, rather than sending a
/// click to whatever is frontmost.
#[derive(Debug, Deserialize)]
pub struct AxPressParams {
    /// The application to resolve the point inside. Hit-testing system-wide
    /// would return whatever window is on top instead.
    pub pid: u32,
    pub x: i64,
    pub y: i64,
}

/// Write text into the element at a point, without focusing its application.
#[derive(Debug, Deserialize)]
pub struct AxSetValueParams {
    pub pid: u32,
    pub x: i64,
    pub y: i64,
    pub text: String,
}

/// Resolve a running application's pid from the name the user would type.
#[derive(Debug, Deserialize)]
pub struct AppPidParams {
    pub app: String,
}

#[derive(Debug, Deserialize)]
pub struct MouseMoveParams {
    pub x: i64,
    pub y: i64,
    /// Milliseconds the trajectory should last. Clamped by the driver.
    pub duration_ms: u64,
}

#[derive(Debug, Deserialize)]
pub struct MouseClickParams {
    pub x: i64,
    pub y: i64,
    pub button: String,
    pub click_count: u8,
}

#[derive(Debug, Deserialize)]
pub struct MouseDragParams {
    pub start_x: i64,
    pub start_y: i64,
    pub end_x: i64,
    pub end_y: i64,
    /// Requested total drag time in ms; the driver stretches it for long
    /// distances so a drag never reads as a teleport (Law 1).
    pub duration_ms: u64,
}

#[derive(Debug, Deserialize)]
pub struct MouseScrollParams {
    pub dx: i64,
    pub dy: i64,
}

#[derive(Debug, Deserialize)]
pub struct PressHotkeyParams {
    pub modifiers: Vec<String>,
    pub key: String,
}

#[derive(Debug, Deserialize)]
pub struct TypeTextParams {
    pub text: String,
    /// Human-like typing speed in words per minute (Law 1 cadence).
    pub wpm: u32,
}

#[derive(Debug, Deserialize)]
pub struct ScreenshotParams {
    /// Empty captures the full main display.
    pub display_id: u32,
    /// Photograph this application's frontmost window instead of the whole
    /// display. Absent means the display, which is what every caller wanted
    /// before an agent could act on a window the user keeps behind another.
    #[serde(default)]
    pub pid: Option<u32>,
}

#[derive(Debug, Deserialize)]
pub struct AxSnapshotParams {
    /// Process id of the application whose accessibility tree to read.
    pub pid: u32,
    /// Traversal depth cap (0 == the app root only).
    pub max_depth: u8,
    /// Node-count cap for the whole snapshot, applied web-first (bounded
    /// walk + bounded payload on heavy apps like Chrome with a large page).
    /// Optional for wire-back-compat: old clients that omit it get the
    /// default, so a driver upgrade never breaks an older orchestrator.
    #[serde(default = "default_ax_max_nodes")]
    pub max_nodes: u32,
}

/// Back-compat default for clients that predate the node budget.
pub fn default_ax_max_nodes() -> u32 {
    4096
}

#[derive(Debug, Deserialize)]
pub struct ActivateAppParams {
    /// Display name of the application to bring to the front (e.g. "Google
    /// Chrome"), as LaunchServices resolves it.
    pub app: String,
}

#[derive(Debug, Deserialize)]
pub struct ClipboardPasteParams {
    /// Text to paste into the focused field via clipboard.
    pub text: String,
}

/// Every response carries its own `ok` flag; errors include a human-readable
/// message with context so the orchestrator can log structured diagnostics
/// (Law 6.3).
#[derive(Debug, Serialize)]
#[serde(tag = "ok", rename_all = "snake_case")]
pub enum Response {
    Pong,
    Health {
        backend: String,
        trusted: bool,
    },
    Ack,
    HotkeyState {
        /// Whether the user pressed the global kill combo (Law 5.2).
        tripped: bool,
    },
    AppPid {
        /// The running app's pid, or null when nothing matches that name or
        /// bundle id — an ordinary answer, not an error: the app may simply
        /// not be running yet.
        pid: Option<i32>,
    },
    AxSetValue {
        /// Whether the element accepted the text. `false` means it is not
        /// writable this way and the caller should type instead; `true` means
        /// accepted, not that the application reacted.
        wrote: bool,
    },
    IdleSeconds {
        /// Seconds since any input device was last touched.
        seconds: f64,
    },
    AxPress {
        /// Whether an accessibility element accepted the press. `false` means
        /// nothing under the point exposes a press action, so the caller
        /// should fall back to a synthetic click. `true` means it was
        /// *accepted*, not that it had an effect — a Chromium web view answers
        /// success and leaves the page untouched — so the orchestrator still
        /// verifies against the screen.
        pressed: bool,
    },
    FocusedWindow {
        /// Process id of the frontmost application (feeds ``ax_snapshot``).
        pid: i32,
        /// Application name of the frontmost app. **Localized** — macOS
        /// translates it, so "Calculator" reports as "Hesap Makinesi" on a
        /// Turkish desktop. Pair it with ``bundle_id`` before deciding the
        /// frontmost app is a different one.
        app_name: String,
        /// The app's `CFBundleIdentifier`, "" when it has no bundle. The
        /// locale-independent identity.
        bundle_id: String,
        /// Title of the focused window ("" when the app has none).
        window_title: String,
        /// Cursor position in global logical points (OBSERVE step).
        cursor_x: f64,
        cursor_y: f64,
    },
    ListApps {
        /// Display names of the running applications with on-screen windows.
        /// Lets the orchestrator infer the goal's target app from what the
        /// user actually runs (autonomous activation, no ``--app`` needed).
        apps: Vec<String>,
    },
    AxSnapshot {
        /// The app's accessibility tree root (ADR-2 primary source).
        root: HostElement,
    },
    Screenshot {
        display_id: u32,
        /// Pixel format of ``data_base64``: currently always ``"bgra8"``.
        format: &'static str,
        width: u32,
        height: u32,
        /// Physical pixels per logical point (Retina == 2.0); lets the
        /// orchestrator map global logical coordinates into this frame.
        scale: f64,
        /// Top-left of the captured display in *global* logical points. The
        /// orchestrator actuates in that global space, so a frame from a
        /// secondary display is only usable with this offset — without it
        /// every coordinate read off it lands on the primary display.
        origin_x: f64,
        origin_y: f64,
        /// BGRA8 row-major pixels, top-down, base64-encoded. The payload can be
        /// megabytes for a real display; base64 over the local Unix socket is
        /// the pragmatic v1 framing (a future optimisation could stream via a
        /// side channel or PNG-compress in Rust).
        data_base64: String,
    },
    Error { message: String },
}

impl Response {
    /// Serialize to the wire (the caller appends the newline).
    ///
    /// ``serde_json::to_string`` validates/escapes every character of the
    /// payload, which costs ~1s for a 40MB base64 screenshot (measured on a
    /// Retina display — the dominant per-step latency). The base64 payload is
    /// pre-encoded ASCII with no characters that need escaping, so manual
    /// construction is memcpy-speed and byte-identical to serde's output
    /// (pinned by the unit test below). All other responses are small and
    /// keep the serde path.
    pub fn to_wire_string(&self) -> String {
        let Response::Screenshot {
            display_id,
            format,
            width,
            height,
            scale,
            origin_x,
            origin_y,
            data_base64,
        } = self
        else {
            return serde_json::to_string(self).expect("response serialization cannot fail");
        };
        // Reuse serde for the f64s alone so each JSON number literal is always
        // valid ("2.0", never the bare "2" that Display produces).
        let scale_json = serde_json::to_string(scale).expect("f64 serialization cannot fail");
        let origin_x_json =
            serde_json::to_string(origin_x).expect("f64 serialization cannot fail");
        let origin_y_json =
            serde_json::to_string(origin_y).expect("f64 serialization cannot fail");
        let mut out = String::with_capacity(data_base64.len() + 160);
        out.push_str("{\"ok\":\"screenshot\",\"display_id\":");
        out.push_str(&display_id.to_string());
        out.push_str(",\"format\":\"");
        out.push_str(format);
        out.push_str("\",\"width\":");
        out.push_str(&width.to_string());
        out.push_str(",\"height\":");
        out.push_str(&height.to_string());
        out.push_str(",\"scale\":");
        out.push_str(&scale_json);
        out.push_str(",\"origin_x\":");
        out.push_str(&origin_x_json);
        out.push_str(",\"origin_y\":");
        out.push_str(&origin_y_json);
        out.push_str(",\"data_base64\":\"");
        out.push_str(data_base64);
        out.push('"');
        out.push('}');
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manual_screenshot_serialization_matches_serde() {
        let response = Response::Screenshot {
            display_id: 0,
            format: "bgra8",
            width: 2,
            height: 1,
            scale: 2.0,
            origin_x: 1512.0,
            origin_y: 0.0,
            data_base64: "AAEC/w==".to_string(),
        };
        assert_eq!(
            response.to_wire_string(),
            serde_json::to_string(&response).expect("serde serialization")
        );
    }

    #[test]
    fn parses_snake_case_mouse_move() {
        let raw = r#"{"method":"mouse_move","params":{"x":10,"y":20,"duration_ms":120}}"#;
        let req: Request = serde_json::from_str(raw).expect("valid request");
        match req {
            Request::MouseMove(p) => {
                assert_eq!(p.x, 10);
                assert_eq!(p.duration_ms, 120);
            }
            other => panic!("unexpected variant {other:?}"),
        }
    }

    #[test]
    fn parses_unit_hotkey_state() {
        let raw = r#"{"method":"hotkey_state"}"#;
        let req: Request = serde_json::from_str(raw).expect("valid request");
        assert!(matches!(req, Request::HotkeyState));
    }

    #[test]
    fn parses_unit_list_apps() {
        let raw = r#"{"method":"list_apps"}"#;
        let req: Request = serde_json::from_str(raw).expect("valid request");
        assert!(matches!(req, Request::ListApps));
    }

    #[test]
    fn parses_unit_focused_window() {
        let raw = r#"{"method":"focused_window"}"#;
        let req: Request = serde_json::from_str(raw).expect("valid request");
        assert!(matches!(req, Request::FocusedWindow));
    }

    #[test]
    fn parses_snake_case_mouse_drag() {
        let raw = r#"{"method":"mouse_drag","params":{"start_x":0,"start_y":0,"end_x":50,"end_y":50,"duration_ms":200}}"#;
        let req: Request = serde_json::from_str(raw).expect("valid request");
        match req {
            Request::MouseDrag(p) => {
                assert_eq!(p.end_x, 50);
                assert_eq!(p.duration_ms, 200);
            }
            other => panic!("unexpected variant {other:?}"),
        }
    }

    #[test]
    fn parses_activate_app() {
        let raw = r#"{"method":"activate_app","params":{"app":"Safari"}}"#;
        let req: Request = serde_json::from_str(raw).expect("valid request");
        match req {
            Request::ActivateApp(p) => assert_eq!(p.app, "Safari"),
            other => panic!("unexpected variant {other:?}"),
        }
    }

    #[test]
    fn parses_clipboard_paste() {
        let raw = r#"{"method":"clipboard_paste","params":{"text":"https://example.com"}}"#;
        let req: Request = serde_json::from_str(raw).expect("valid request");
        match req {
            Request::ClipboardPaste(p) => assert_eq!(p.text, "https://example.com"),
            other => panic!("unexpected variant {other:?}"),
        }
    }

    #[test]
    fn parses_unit_health() {
        let raw = r#"{"method":"health"}"#;
        let req: Request = serde_json::from_str(raw).expect("valid request");
        assert!(matches!(req, Request::Health));
    }

    #[test]
    fn serializes_health_response() {
        let resp = Response::Health {
            backend: "simulated".to_string(),
            trusted: true,
        };
        let wire = resp.to_wire_string();
        assert!(wire.contains(r#""ok":"health""#));
        assert!(wire.contains(r#""backend":"simulated""#));
        assert!(wire.contains(r#""trusted":true"#));
    }

    #[test]
    fn rejects_bad_method() {
        let raw = r#"{"method":"teleport","params":{}}"#;
        assert!(serde_json::from_str::<Request>(raw).is_err());
    }
}