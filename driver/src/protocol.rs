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
    FocusedWindow,
    HotkeyState,
    MouseMove(MouseMoveParams),
    MouseClick(MouseClickParams),
    MouseDrag(MouseDragParams),
    MouseScroll(MouseScrollParams),
    PressHotkey(PressHotkeyParams),
    TypeText(TypeTextParams),
    Screenshot(ScreenshotParams),
    AxSnapshot(AxSnapshotParams),
    ActivateApp(ActivateAppParams),
    ClipboardPaste(ClipboardPasteParams),
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
}

#[derive(Debug, Deserialize)]
pub struct AxSnapshotParams {
    /// Process id of the application whose accessibility tree to read.
    pub pid: u32,
    /// Traversal depth cap (0 == the app root only).
    pub max_depth: u8,
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
    Ack,
    HotkeyState {
        /// Whether the user pressed the global kill combo (Law 5.2).
        tripped: bool,
    },
    FocusedWindow {
        /// Process id of the frontmost application (feeds ``ax_snapshot``).
        pid: i32,
        /// Application name of the frontmost app.
        app_name: String,
        /// Title of the focused window ("" when the app has none).
        window_title: String,
        /// Cursor position in global logical points (OBSERVE step).
        cursor_x: f64,
        cursor_y: f64,
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
        /// BGRA8 row-major pixels, top-down, base64-encoded. The payload can be
        /// megabytes for a real display; base64 over the local Unix socket is
        /// the pragmatic v1 framing (a future optimisation could stream via a
        /// side channel or PNG-compress in Rust).
        data_base64: String,
    },
    Error { message: String },
}

#[cfg(test)]
mod tests {
    use super::*;

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
    fn rejects_bad_method() {
        let raw = r#"{"method":"teleport","params":{}}"#;
        assert!(serde_json::from_str::<Request>(raw).is_err());
    }
}