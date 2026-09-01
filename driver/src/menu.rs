//! Menu-bar launcher (macOS only): a tiny always-on status-bar app that owns a
//! single sea-blue icon. Clicking it drops a Liquid-Glass chat panel built as
//! HTML in a WKWebView; typing a goal and hitting Run spawns the existing
//! agent CLI (``uv run python -m computeruse --real``) as a subprocess and
//! streams its live output back into the panel.
//!
//! Rationale (Law 5.2 + Law 1): a physical computer-use agent must feel like a
//! trustworthy pair of hands, so the launcher is deliberately a first-class
//! native guest — a glass panel that never looks like a terminal, plus the
//! driver's translucent cursor halo during a run (the halo is unaffected; only
//! the *busy status icon* is suppressed for the spawned driver via
//! ``COMPUTERUSE_NO_STATUS`` so exactly one menu-bar icon exists).
//!
//! The panel's UI never touches the OS input stream: all actuation still flows
//! through the Rust driver over its Unix socket. This module is the *imperative
//! shell* (Law 6): AppKit/WebKit hosting, process spawning, and path/env
//! resolution — no pure math lives here.

#![cfg(target_os = "macos")]

use std::collections::VecDeque;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;

use std::ffi::c_uchar;

use objc2::rc::Retained;
use objc2::runtime::{AnyObject, Bool, NSObject, NSObjectProtocol, ProtocolObject};
use objc2::{define_class, msg_send, sel, ClassType, MainThreadMarker, MainThreadOnly};
use objc2_app_kit::{
    NSApplication, NSApplicationActivationPolicy, NSBackingStoreType, NSBitmapImageRep, NSColor,
    NSImage, NSMenu, NSMenuItem, NSStatusBar, NSWindow, NSWindowButton,
    NSWindowCollectionBehavior, NSWindowStyleMask, NSWindowTitleVisibility,
};
use objc2_core_foundation::{CGRect, CGPoint, CGSize};
use objc2_core_graphics::CGContext;
use objc2_foundation::{NSString, NSInteger, NSTimer, NSURL};
use objc2_web_kit::{
    WKScriptMessage, WKScriptMessageHandler, WKUserContentController, WKWebView,
    WKWebViewConfiguration,
};

use crate::ax;

// ---------------------------------------------------------------------------
// Shared cross-thread state: reader threads push streamed lines here; the main
// run-loop timer drains them into the webview (which only the main thread may
// touch). All fields are Send — only ever a string queue, a pid, and an exit
// flag — so ``Mutex`` keeps Law 6's "concrete, typed" shell honest.
// ---------------------------------------------------------------------------

/// How a streamed line should be rendered in the panel.
#[derive(Clone, Copy, PartialEq)]
enum LineKind {
    /// stdout — the CLI's summary block, most important.
    Out,
    /// stderr — the runner's INFO step log, secondary.
    Dim,
    /// a launcher-side error, styled red.
    Err,
}

#[derive(Default)]
struct Shared {
    lines: VecDeque<(LineKind, String)>,
    child_pid: Option<u32>,
    exit: Option<Option<i32>>,
    signalled: bool,
}

static SHARED: Mutex<Shared> = Mutex::new(Shared {
    lines: VecDeque::new(),
    child_pid: None,
    exit: None,
    signalled: false,
});

/// Pointer to the panel so the status-item toggle can reach it without owning
/// it twice. Created on the main thread, mutated on the main thread only; the
/// raw pointer sidesteps AppKit retain semantics (the panel is leaked for the
/// process lifetime, which is exactly the launcher's lifetime). An atomic lets
/// us hand it out without proving Send for the AppKit object (it is a plain
/// address; the panel itself never leaves the main thread).
static PANEL_PTR: core::sync::atomic::AtomicPtr<core::ffi::c_void> =
    core::sync::atomic::AtomicPtr::new(core::ptr::null_mut());

/// Pointer to the actual status item so context menu can anchor under it.
static STATUS_ITEM_PTR: core::sync::atomic::AtomicPtr<core::ffi::c_void> =
    core::sync::atomic::AtomicPtr::new(core::ptr::null_mut());

/// Frontmost app captured just before the panel activates, so a run can target
/// whatever the user was using (a typed app field wins over this).
static CAPTURED_APP: Mutex<Option<String>> = Mutex::new(None);

/// Ensures only one instance of the menu launcher runs at any given time.
fn ensure_single_instance() {
    let pid_file = "/tmp/actuation-menu.pid";
    if let Ok(content) = std::fs::read_to_string(pid_file) {
        if let Ok(old_pid) = content.trim().parse::<i32>() {
            let my_pid = std::process::id() as i32;
            if old_pid != my_pid {
                // Send SIGTERM and wait until the old process is truly gone.
                // A simple 150ms sleep is not enough — macOS launchd or `open`
                // can spawn a second instance before the first exits.
                unsafe {
                    libc::kill(old_pid, libc::SIGTERM);
                }
                for _ in 0..50 {
                    if unsafe { libc::kill(old_pid, 0) } != 0 {
                        break; // Process is gone (ESRCH).
                    }
                    std::thread::sleep(std::time::Duration::from_millis(50));
                }
            }
        }
    }
    let _ = std::fs::write(pid_file, std::process::id().to_string());
}

pub fn run() -> ! {
    ensure_single_instance();
    let mtm = MainThreadMarker::new().expect("menu launcher must run on the main thread");
    let app = NSApplication::sharedApplication(mtm);
    // Accessory: no Dock icon, never steals focus on its own.
    let _ = app.setActivationPolicy(NSApplicationActivationPolicy::Accessory);

    let status = NSStatusBar::systemStatusBar().statusItemWithLength(30.0);
    STATUS_ITEM_PTR.store(
        (&*status as *const objc2_app_kit::NSStatusItem).cast_mut().cast(),
        core::sync::atomic::Ordering::SeqCst,
    );
    let menu_icon = menu_icon(mtm);
    let panel = build_panel(mtm);
    let webview = build_webview(mtm);
    panel.setContentView(Some(&webview));
    PANEL_PTR.store(
        (&*panel as *const NSWindow).cast_mut().cast(),
        core::sync::atomic::Ordering::SeqCst,
    );

    // The status item's click target: handles both left click (toggle) and right click (quit menu).
    let target = ScriptTarget::alloc(mtm);
    let target: Retained<ScriptTarget> = unsafe { msg_send![target, init] };
    let target_ptr: *mut ScriptTarget = Retained::into_raw(target);

    if let Some(button) = status.button(mtm) {
        attach_icon(&button, &menu_icon);
        unsafe {
            // Receive both LeftMouseUp (1 << 1) and RightMouseUp (1 << 3) events
            let mask: NSInteger = (1 << 1) | (1 << 3);
            let _old_mask: NSInteger = msg_send![&*button, sendActionOn: mask];
            let _: () = msg_send![&*button, setTarget: target_ptr];
            let _: () = msg_send![&*button, setAction: sel!(handleClick:)];
        }
    }

    // 30 Hz digital-timer: drain streamed lines into the webview and signal
    // run completion. Runs on the AppKit main loop like the indicator's halo.
    let timer_block = block2::StackBlock::new(move |_: std::ptr::NonNull<NSTimer>| {
        drain_to_webview(&webview);
    });
    let _timer = unsafe {
        NSTimer::scheduledTimerWithTimeInterval_repeats_block(1.0 / 30.0, true, &timer_block)
    };

    app.run();
    std::process::exit(0)
}

// The status-item target whose actions handle clicking and context menus.
define_class!(
    #[unsafe(super(NSObject))]
    #[thread_kind = MainThreadOnly]
    pub struct ScriptTarget;

    impl ScriptTarget {
        #[unsafe(method(handleClick:))]
        fn handle_click(&self, _sender: *mut AnyObject) {
            let mtm = MainThreadMarker::new().expect("menu launcher must run on the main thread");
            let app = NSApplication::sharedApplication(mtm);
            let is_right = if let Some(event) = app.currentEvent() {
                unsafe {
                    let ev_type: usize = msg_send![&*event, type];
                    ev_type == 3 || ev_type == 25 // NSEventTypeRightMouseDown (3) or NSEventTypeRightMouseUp (25)
                }
            } else {
                false
            };

            if is_right {
                show_context_menu(mtm);
            } else {
                toggle_panel_ui();
            }
        }

        #[unsafe(method(togglePanel:))]
        fn toggle_panel(&self, _sender: *mut AnyObject) {
            toggle_panel_ui();
        }

        #[unsafe(method(quitApp:))]
        fn quit_app(&self, _sender: *mut AnyObject) {
            std::process::exit(0);
        }
    }
);

fn show_context_menu(mtm: MainThreadMarker) {
    let menu = NSMenu::new(mtm);

    let title_item = unsafe {
        NSMenuItem::initWithTitle_action_keyEquivalent(
            NSMenuItem::alloc(mtm),
            &NSString::from_str("Computer Use Copilot"),
            None,
            &NSString::from_str(""),
        )
    };
    title_item.setEnabled(false);
    menu.addItem(&title_item);

    menu.addItem(&NSMenuItem::separatorItem(mtm));

    let toggle_item = unsafe {
        NSMenuItem::initWithTitle_action_keyEquivalent(
            NSMenuItem::alloc(mtm),
            &NSString::from_str("Open / Close Panel"),
            Some(sel!(togglePanel:)),
            &NSString::from_str(""),
        )
    };
    let target = ScriptTarget::alloc(mtm);
    let target: Retained<ScriptTarget> = unsafe { msg_send![target, init] };
    let target_ptr: *mut ScriptTarget = Retained::into_raw(target);
    unsafe {
        let _: () = msg_send![&*toggle_item, setTarget: target_ptr];
    }
    menu.addItem(&toggle_item);

    menu.addItem(&NSMenuItem::separatorItem(mtm));

    let quit_item = unsafe {
        NSMenuItem::initWithTitle_action_keyEquivalent(
            NSMenuItem::alloc(mtm),
            &NSString::from_str("Quit Computer Use"),
            Some(sel!(quitApp:)),
            &NSString::from_str("q"),
        )
    };
    unsafe {
        let _: () = msg_send![&*quit_item, setTarget: target_ptr];
    }
    menu.addItem(&quit_item);

    let ptr = STATUS_ITEM_PTR.load(core::sync::atomic::Ordering::SeqCst);
    if !ptr.is_null() {
        let status = unsafe { &*(ptr as *const objc2_app_kit::NSStatusItem) };
        unsafe {
            let _: () = msg_send![status, popUpStatusItemMenu: &*menu];
        }
    }
}

// The WKScriptMessageHandler that receives the HTML's ``postMessage`` calls.
//
// A MainThreadOnly NSObject adopting ``WKScriptMessageHandler`` (WebKit
// dispatches script messages on the main thread). The conformances live
// *inside* ``define_class!`` per objc2's contract, and ``NSObjectProtocol``
// must be adopted explicitly just like the crate's own docs show.
// ``userContentController_didReceiveScriptMessage`` is the exact method the
// generated ``extern_protocol`` requires, so the snake-case waiver sits on the
// impl itself.
define_class!(
    #[unsafe(super(NSObject))]
    #[thread_kind = MainThreadOnly]
    pub struct ScriptBridge;

    unsafe impl NSObjectProtocol for ScriptBridge {}

    #[allow(non_snake_case)]
    unsafe impl WKScriptMessageHandler for ScriptBridge {
        #[unsafe(method(userContentController:didReceiveScriptMessage:))]
        unsafe fn userContentController_didReceiveScriptMessage(
            &self,
            _controller: &WKUserContentController,
            message: &WKScriptMessage,
        ) {
            handle_script_message(message);
        }
    }
);

fn toggle_panel_ui() {
    eprintln!("[menu] toggle fired");
    let ptr = PANEL_PTR.load(core::sync::atomic::Ordering::SeqCst);
    if ptr.is_null() {
        return;
    }
    // SAFETY: the panel is created once on the main thread and leaked; this
    // callback runs on the main thread (MainThreadOnly), so deref is sound.
    let panel = unsafe { &*(ptr as *const NSWindow) };
    // Capture the frontmost app *before* we activate, so auto-targeting picks
    // whatever the user had open (the accessory app itself is never a target).
    *CAPTURED_APP.lock().unwrap() = ax::focused_window()
        .ok()
        .filter(|f| !f.app_name.is_empty())
        .map(|f| f.app_name);
    if panel.isVisible() {
        panel.orderOut(None);
    } else {
        position_near_status_item(panel);
        panel.makeKeyAndOrderFront(None);
        if !panel.isVisible() {
            eprintln!("[menu] WARNING: panel not visible after makeKeyAndOrderFront");
        }
        activate_app();
    }
}

fn activate_app() {
    let mtm = MainThreadMarker::new()
        .expect("activate must be on the main thread");
    let app = NSApplication::sharedApplication(mtm);
    unsafe {
        let _: () = msg_send![&*app, activateIgnoringOtherApps: true];
    }
}

// ---------------------------------------------------------------------------
// WKWebView construction + UI messaging
// ---------------------------------------------------------------------------

fn build_webview(mtm: MainThreadMarker) -> Retained<WKWebView> {
    // SAFETY: standard AppKit/WebKit object graph; the config and controller
    // are owned for the webview's lifetime here.
    unsafe {
        let config = WKWebViewConfiguration::new(mtm);
        let controller = config.userContentController();
        let bridge = ScriptBridge::alloc(mtm);
        let bridge: Retained<ScriptBridge> = msg_send![bridge, init];
        let name = NSString::from_str("bridge");
        let _: () = controller.addScriptMessageHandler_name(ProtocolObject::from_ref(&*bridge), &name);
        let frame = CGRect::new(CGPoint::new(0.0, 0.0), CGSize::new(440.0, 620.0));
        let webview = WKWebView::initWithFrame_configuration(WKWebView::alloc(mtm), frame, &config);
        let html = NSString::from_str(include_str!("../assets/menu.html"));
        let base = NSURL::fileURLWithPath(&NSString::from_str("/"));
        webview.loadHTMLString_baseURL(&html, Some(&base));
        webview
    }
}

fn build_panel(mtm: MainThreadMarker) -> Retained<NSWindow> {
    let frame = CGRect::new(CGPoint::new(0.0, 0.0), CGSize::new(440.0, 620.0));
    // Titled | FullSizeContentView: the title bar is hidden (transparent) so
    // the panel reads as floating glass, but the window stays *key* so the
    // user can actually type into the embedded web page.
    let style = NSWindowStyleMask::Titled
        | NSWindowStyleMask::FullSizeContentView
        | NSWindowStyleMask::Closable;
    let panel = unsafe {
        NSWindow::initWithContentRect_styleMask_backing_defer(
            NSWindow::alloc(mtm),
            frame,
            style,
            NSBackingStoreType::Buffered,
            false,
        )
    };
    unsafe { panel.setReleasedWhenClosed(false) };
    panel.setTitlebarAppearsTransparent(true);
    // Hide the native traffic lights (close / minimize / zoom) and the title:
    // the panel is its own floating-glass chrome — the HTML toolbar carries
    // the brand flush left and the user toggles the panel from the status
    // item, so the window buttons only add visual noise. Titled stays (not
    // Borderless) so the window still becomes *key* and the webview accepts
    // text input without a custom canBecomeKeyWindow subclass.
    panel.setTitleVisibility(NSWindowTitleVisibility::Hidden);
    for button in [
        NSWindowButton::CloseButton,
        NSWindowButton::MiniaturizeButton,
        NSWindowButton::ZoomButton,
    ] {
        if let Some(btn) = panel.standardWindowButton(button) {
            // SAFETY: setHidden: is inherited from NSView; the button is a
            // live subview of the window's titlebar for its whole lifetime.
            unsafe {
                let _: () = msg_send![&*btn, setHidden: true];
            }
        }
    }
    panel.setOpaque(false);
    panel.setBackgroundColor(Some(&NSColor::clearColor()));
    panel.setHasShadow(true);
    // The whole header is a drag region, exactly like a modern macOS app's
    // title bar — grab anywhere on the top edge and the panel follows.
    // Controls keep normal hit-testing.
    panel.setMovableByWindowBackground(true);
    // Don't hide when the agent activates a target app mid-run — the streaming
    // summary must stay on screen for the whole run.
    panel.setHidesOnDeactivate(false);
    panel.setCollectionBehavior(NSWindowCollectionBehavior::CanJoinAllSpaces);
    panel
}

fn position_near_status_item(panel: &NSWindow) {
    use core_graphics::display::CGDisplay;
    let screen = CGDisplay::main().bounds(); // bottom-left origin
    let width = screen.size.width;
    let height = screen.size.height;
    let menu_bar: f64 = 24.0;
    // Top-right, just under the menu bar. AppKit grows Y from the bottom, so
    // "top" = height - menu_bar - panel_height.
    let origin = CGPoint::new(width - 440.0 - 12.0, height - menu_bar - 620.0 - 8.0);
    panel.setFrameOrigin(origin);
}

/// Evaluate a JS snippet in the webview (main thread only).
fn call_js(webview: &WKWebView, js: &str) {
    let js = NSString::from_str(js);
    unsafe {
        let _: () = webview.evaluateJavaScript_completionHandler(&js, None);
    }
}

fn drain_to_webview(webview: &WKWebView) {
    let (batch, finished_now, was_stopped) = {
        let mut s = SHARED.lock().unwrap();
        let batch: Vec<(LineKind, String)> = s.lines.drain(..).collect();
        // A run finishes when: (a) exit is set AND (b) all lines have been
        // drained AND (c) we haven't already signalled completion.
        let done = s.exit.is_some() && !s.signalled && batch.is_empty() && s.lines.is_empty();
        let stopped = s.exit == Some(Some(130));
        if done {
            s.signalled = true;
        }
        (batch, done, stopped)
    };
    if !batch.is_empty() {
        let mut js = String::with_capacity(batch.len() * 64);
        for (kind, line) in &batch {
            if line.trim().is_empty() {
                continue;
            }
            let f = match kind {
                LineKind::Out => "window.Native.log(%s);",
                LineKind::Dim => "window.Native.dim(%s);",
                LineKind::Err => "window.Native.err(%s);",
            };
            js.push_str(&f.replace("%s", &serde_json::to_string(line).unwrap_or_default()));
        }
        if !js.is_empty() {
            call_js(webview, &js);
        }
    }
    if finished_now {
        // Distinguish stopped (user pressed Stop) from completed runs.
        if was_stopped {
            call_js(webview, "window.Native.done(false); window.Native.dim('\\u2014 agent stopped \\u2014');");
        } else {
            call_js(webview, "window.Native.done(true); window.Native.dim('\\u2014 run finished \\u2014');");
        }
        let mut s = SHARED.lock().unwrap();
        s.child_pid = None;
    }
}

fn handle_script_message(message: &WKScriptMessage) {
    let body = unsafe { message.body() };
    let Some(ns) = body.downcast::<NSString>().ok() else {
        return;
    };
    let raw = ns.to_string();
    let Ok(value) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return;
    };
    match value.get("cmd").and_then(serde_json::Value::as_str) {
        Some("run") => {
            let goal = value.get("goal").and_then(serde_json::Value::as_str).unwrap_or("").trim().to_string();
            if goal.is_empty() {
                return;
            }
            let app = value
                .get("app")
                .and_then(serde_json::Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(str::to_string)
                .or_else(|| CAPTURED_APP.lock().unwrap().clone());
            run_agent(&goal, app.as_deref());
        }
        Some("stop") => stop_agent(),
        _ => {}
    }
}

// ---------------------------------------------------------------------------
// Agent subprocess: path/env resolution + spawn + streaming
// ---------------------------------------------------------------------------

/// Build the CLI argument vector for the agent subprocess (pure, testable).
///
/// One place owns the whole decision of what the launcher asks the orchestrator
/// to do, so the UI (Rust) and the CLI (Python) cannot drift about whether the
/// run drives the real model. ``--model openai`` is *mandatory* here:
/// omitting it falls back to the CLI's default scripted demo provider (two
/// fixed clicks, then finish), which on a real host reads as a broken agent.
fn agent_args(
    goal: &str,
    app: Option<&str>,
    driver_bin: &str,
    socket: &str,
    store: &str,
) -> Vec<String> {
    let mut args = vec![
        "run".to_string(),
        "python".to_string(),
        "-m".to_string(),
        "computeruse".to_string(),
        "--goal".to_string(),
        goal.to_string(),
        "--real".to_string(),
        // Never let a real host silently fall into the demo provider.
        "--model".to_string(),
        "openai".to_string(),
        "--driver".to_string(),
        driver_bin.to_string(),
        "--socket".to_string(),
        socket.to_string(),
        "--store".to_string(),
        store.to_string(),
        "--level".to_string(),
        "3".to_string(),
    ];
    if let Some(app_name) = app {
        args.extend(["--app".to_string(), app_name.to_string()]);
    }
    args
}

fn run_agent(goal: &str, app: Option<&str>) {
    // Guard: never double-run while one is in flight.
    let already = SHARED.lock().unwrap().child_pid.is_some();
    if already {
        return;
    }
    let Some(root) = project_root() else {
        push_err("could not locate the project root (pyproject.toml); run from a checkout or ensure ~/.computeruse/root is configured".to_string());
        let mut s = SHARED.lock().unwrap();
        s.exit = Some(Some(1));
        return;
    };
    let Some(uv) = find_uv() else {
        push_err("`uv` not found on PATH or in ~/.local/bin, /opt/homebrew/bin, /usr/local/bin".to_string());
        let mut s = SHARED.lock().unwrap();
        s.exit = Some(Some(1));
        return;
    };
    let key = openai_key();
    if key.is_none() {
        push_err("OPENAI_API_KEY is not set (env or ~/.computeruse/env). The agent cannot call the model without it.".to_string());
        let mut s = SHARED.lock().unwrap();
        s.exit = Some(Some(1));
        return;
    }

    let home = std::env::var("HOME").unwrap_or_else(|_| "/Users/me".to_string());
    let driver_bin = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.join("actuation-driver")))
        .filter(|p| p.is_file())
        .unwrap_or_else(|| {
            root.join("driver")
                .join("target")
                .join("debug")
                .join("actuation-driver")
        });
    let socket = "/tmp/actuation-menu.sock".to_string();
    let store = format!("{home}/.computeruse");

    push_out(format!("starting agent: {goal}"));
    if let Some(a) = app {
        push_out(format!("targeting app: {a}"));
    }

    let mut cmd = Command::new(&uv);
    cmd.current_dir(&root);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    let driver_bin_str = driver_bin.to_string_lossy().into_owned();
    cmd.args(agent_args(goal, app, &driver_bin_str, &socket, &store));
    // The launcher owns the status icon; the spawned driver stays halo-only.
    cmd.env("COMPUTERUSE_NO_STATUS", "1");
    cmd.env("OPENAI_API_KEY", key.expect("checked above"));
    cmd.stdout(Stdio::piped());
    cmd.stderr(Stdio::piped());

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            push_err(format!("failed to start agent: {e}"));
            let mut s = SHARED.lock().unwrap();
            s.exit = Some(Some(1));
            return;
        }
    };
    let pid = child.id();
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    {
        let mut s = SHARED.lock().unwrap();
        s.child_pid = Some(pid);
        s.exit = None;
        s.signalled = false;
    }
    if let Some(reader) = stdout {
        std::thread::spawn(move || pump_lines(BufReader::new(reader), true));
    }
    if let Some(reader) = stderr {
        std::thread::spawn(move || pump_lines(BufReader::new(reader), false));
    }
    std::thread::spawn(move || {
        let code = child.wait().ok().and_then(|s| s.code());
        let mut g = SHARED.lock().unwrap();
        g.exit = Some(code);
    });
}

/// Read a child pipe line-by-line and enqueue each line for the UI.
fn pump_lines(reader: impl BufRead, is_stdout: bool) {
    let kind = if is_stdout { LineKind::Out } else { LineKind::Dim };
    for line in reader.lines().map_while(Result::ok) {
        let trimmed = line.trim_end().to_string();
        if trimmed.is_empty() {
            continue;
        }
        SHARED.lock().unwrap().lines.push_back((kind, trimmed));
    }
}

fn stop_agent() {
    let pid = {
        let mut s = SHARED.lock().unwrap();
        s.exit = Some(Some(130));
        // Do NOT set signalled here — let drain_to_webview flush remaining
        // lines and then signal done(false) to the UI. Setting signalled
        // prematurely would cause drain to skip the final done() callback.
        s.child_pid.take()
    };
    match pid {
        Some(pid) => {
            // SAFETY: immediately terminate the child process and its entire process group
            unsafe {
                libc::killpg(pid as i32, libc::SIGKILL);
                libc::kill(pid as i32, libc::SIGKILL);
            }
            push_dim("— agent stopped by user —".to_string());
        }
        None => push_dim("nothing running to stop".to_string()),
    }
}

/// Push a normal (stdout-style) line into the UI stream.
fn push_out(line: String) {
    SHARED.lock().unwrap().lines.push_back((LineKind::Out, line));
}

/// Push a launcher-side error line into the UI stream (styled red).
fn push_err(line: String) {
    SHARED.lock().unwrap().lines.push_back((LineKind::Err, line));
}

/// Push a secondary (stderr/step-log style) line into the UI stream.
fn push_dim(line: String) {
    SHARED.lock().unwrap().lines.push_back((LineKind::Dim, line));
}

// ---------------------------------------------------------------------------
// Path / env resolution (a GUI app has no shell, so we resolve ourselves)
// ---------------------------------------------------------------------------

fn is_valid_project_root(dir: &Path) -> bool {
    let marker = dir.join("pyproject.toml");
    if marker.is_file() {
        if let Ok(text) = std::fs::read_to_string(&marker) {
            if text.contains("name = \"computeruse\"") {
                return true;
            }
        }
    }
    false
}

/// Walk up from the executable until ``pyproject.toml`` declares this project,
/// with fallback to COMPUTERUSE_ROOT, ~/.computeruse/root, and common dev paths.
fn project_root() -> Option<PathBuf> {
    // 1. Env variable override
    if let Ok(path) = std::env::var("COMPUTERUSE_ROOT") {
        let p = PathBuf::from(path);
        if is_valid_project_root(&p) {
            return Some(p);
        }
    }
    // 2. ~/.computeruse/root config file (written by package_app.sh)
    if let Ok(home) = std::env::var("HOME") {
        let root_file = Path::new(&home).join(".computeruse").join("root");
        if let Ok(text) = std::fs::read_to_string(&root_file) {
            let p = PathBuf::from(text.trim());
            if is_valid_project_root(&p) {
                return Some(p);
            }
        }
    }
    // 3. Walk up from executable (development checkout / cargo run)
    if let Ok(exe) = std::env::current_exe() {
        let mut dir = exe.parent();
        for _ in 0..10 {
            if let Some(d) = dir {
                if is_valid_project_root(d) {
                    return Some(d.to_path_buf());
                }
                dir = d.parent();
            } else {
                break;
            }
        }
    }
    // 4. Common development checkouts
    if let Ok(home) = std::env::var("HOME") {
        let candidates = [
            Path::new(&home).join("Desktop").join("computeruse"),
            Path::new(&home).join("Documents").join("computeruse"),
            Path::new(&home).join("Projects").join("computeruse"),
            Path::new(&home).join("src").join("computeruse"),
            Path::new(&home).join("computeruse"),
        ];
        for candidate in candidates {
            if is_valid_project_root(&candidate) {
                return Some(candidate);
            }
        }
    }
    None
}

fn find_uv() -> Option<PathBuf> {
    if let Ok(path) = std::env::var("PATH") {
        for dir in path.split(':') {
            let candidate = Path::new(dir).join("uv");
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    let home = std::env::var("HOME").ok()?;
    [
        Path::new(&home).join(".local/bin/uv"),
        PathBuf::from("/opt/homebrew/bin/uv"),
        PathBuf::from("/usr/local/bin/uv"),
        PathBuf::from("/opt/local/bin/uv"),
    ]
    .into_iter()
    .find(|candidate| candidate.is_file())
}

/// The agent model key: the launcher's own env first, then ``~/.computeruse/env``
/// (a plain ``OPENAI_API_KEY=...`` file) so the GUI app — which inherits no
/// shell — can still reach the model without a terminal.
fn openai_key() -> Option<String> {
    if let Ok(key) = std::env::var("OPENAI_API_KEY") {
        if !key.trim().is_empty() {
            return Some(key);
        }
    }
    let home = std::env::var("HOME").ok()?;
    let file = Path::new(&home).join(".computeruse").join("env");
    let text = std::fs::read_to_string(&file).ok()?;
    text.lines().find_map(|line| {
        let line = line.trim();
        let value = line.strip_prefix("OPENAI_API_KEY=")?;
        let value = value.trim().trim_matches(['"', '\'']);
        (!value.is_empty()).then(|| value.to_string())
    })
}

// ---------------------------------------------------------------------------
// Status icon: sea-blue disc + white arrow (the app's visual signature)
// ---------------------------------------------------------------------------

fn attach_icon(button: &objc2_app_kit::NSStatusBarButton, icon: &NSImage) {
    let tip = NSString::from_str("Computer Use — click to give the agent a task");
    unsafe {
        let _: () = msg_send![button, setImage: icon];
        let _: () = msg_send![button, setToolTip: &*tip];
    }
}

/// The menu-bar icon: a sea-blue squircle with a white paper airplane — the
/// same mark the panel shows in its toolbar, so the status bar and the panel
/// always read as one product. It is drawn *pixel by pixel*
/// into a 36×36 RGBA buffer backed by an ``NSBitmapImageRep`` (the documented,
/// reliable route — block-based ``drawingHandler`` images rendered empty for a
/// status item in practice) so the colored tile is guaranteed to appear on any
/// menu bar. Falls back to an SF Symbol only if bitmap construction fails.
fn menu_icon(mtm: MainThreadMarker) -> Retained<NSImage> {
    if let Some(image) = pixel_icon(mtm) {
        return image;
    }
    // Fallback: a template SF Symbol (macOS < the version that ships symbols
    // is not a real concern here, but keep the type total). The paper-plane
    // symbol echoes the pixel-drawn mark above.
    let name = NSString::from_str("paperplane.fill");
    let desc = NSString::from_str("Computer Use");
    // SAFETY: standard NSImage class factory; nil on pre-11 macOS.
    let symbol: Option<Retained<NSImage>> = unsafe {
        msg_send![NSImage::class(), imageWithSystemSymbolName: &*name, accessibilityDescription: &*desc]
    };
    match symbol {
        Some(i) => {
            i.setTemplate(true);
            i
        }
        None => {
            // Total fallback (never expected): a plain template disc.
            let size = CGSize::new(18.0, 18.0);
            let block = block2::StackBlock::new(|_rect: CGRect| -> Bool {
                let Some(gc) = objc2_app_kit::NSGraphicsContext::currentContext() else {
                    return Bool::new(false);
                };
                let ctx = gc.CGContext();
                CGContext::begin_path(Some(&ctx));
                CGContext::add_ellipse_in_rect(Some(&ctx), CGRect::new(CGPoint::new(2.0, 2.0), CGSize::new(14.0, 14.0)));
                CGContext::set_fill_color_with_color(
                    Some(&ctx),
                    Some(&objc2_core_graphics::CGColor::new_srgb(1.0, 1.0, 1.0, 1.0)),
                );
                CGContext::fill_path(Some(&ctx));
                Bool::new(true)
            });
            let image = NSImage::imageWithSize_flipped_drawingHandler(size, false, &block);
            image.setTemplate(true);
            image
        }
    }
}

/// Build the colored icon as raw RGBA in a bitmap rep. Returns None if the
/// AppKit bitmap factory declines to allocate (never on macOS in practice).
/// The backing pixel buffer is intentionally leaked: the icon is created once
/// per launch and lives for the process lifetime, so a small leak (~5 KB) is
/// a deliberate, documented trade for keeping the rep's pointer valid forever
/// (avoiding a use-after-free on a static menu-bar icon).
fn pixel_icon(_mtm: MainThreadMarker) -> Option<Retained<NSImage>> {
    const W: usize = 36;
    const H: usize = 36;
    // Sea blue + white arrow are produced by `icon_pixel` (the agent's visual
    // signature, same colours as the halo); this function only bricks the raw
    // RGBA bytes into an NSBitmapImageRep.
    let mut pixels: Vec<u8> = Vec::with_capacity(W * H * 4);
    for y in 0..H {
        for x in 0..W {
            let (r, g, b, a) = icon_pixel(x as f64, y as f64);
            pixels.extend_from_slice(&[r, g, b, (a * 255.0).round() as u8]);
        }
    }
    let mut bytes = Box::leak(pixels.into_boxed_slice()).as_mut_ptr();
    let plane: *mut *mut c_uchar = &mut bytes as *mut *mut c_uchar;
    let cs = NSString::from_str("NSCalibratedRGBColorSpace");
    // The typed `alloc`/init helpers are gated behind a MainThreadOnly bound
    // this class does not satisfy, so build both objects with raw ObjC
    // message sends (alloc + initWith…) — a standard, stable pattern.
    // SAFETY: the bitmap rep borrows the (leaked, thus immortal) pixel buffer.
    let rep: Retained<NSBitmapImageRep> = unsafe {
        let rep: Option<Retained<NSBitmapImageRep>> = msg_send![msg_send![NSBitmapImageRep::class(), alloc],
            initWithBitmapDataPlanes: plane,
            pixelsWide: (W as NSInteger), pixelsHigh: (H as NSInteger),
            bitsPerSample: (8 as NSInteger), samplesPerPixel: (4 as NSInteger),
            hasAlpha: true, isPlanar: false,
            colorSpaceName: &*cs, bytesPerRow: ((W * 4) as NSInteger), bitsPerPixel: (32 as NSInteger),
        ];
        rep?
    };
    let image: Retained<NSImage> = unsafe {
        msg_send![msg_send![NSImage::class(), alloc], initWithSize: CGSize::new(18.0, 18.0)]
    };
    image.addRepresentation(&rep);
    Some(image)
}

/// Colour of a single icon pixel: emerald green #50A574 rounded squircle, white paper
/// airplane on top, transparent elsewhere. ``icon_pixel`` is pure and total
/// (Law 6). The plane polygon is the exact shape of the SVG in menu.html
/// (``M5 4.5 20 12 5 19.5 8.6 12z`` in 24-space) scaled ×1.5 into 36-space.
fn icon_pixel(x: f64, y: f64) -> (u8, u8, u8, f64) {
    const EMERALD: (u8, u8, u8) = (80, 165, 116);
    // Paper-airplane outline (even-odd filled) in 36-space: nose at the
    // right, wing tips top-left / bottom-left, fold at left-centre. Y grows
    // *down* in bitmap pixels, matching the SVG's y-down viewBox.
    const PLANE: [(f64, f64); 4] = [(7.5, 6.75), (30.0, 18.0), (7.5, 29.25), (12.9, 18.0)];
    if in_polygon(x, y, &PLANE) {
        (255, 255, 255, 1.0)
    } else if in_rrect(x, y, 2.0, 2.0, 33.0, 33.0, 8.0) {
        (EMERALD.0, EMERALD.1, EMERALD.2, 1.0)
    } else {
        (0, 0, 0, 0.0)
    }
}

/// Point inside a rounded rectangle (axis-aligned, corners clipped by r).
fn in_rrect(x: f64, y: f64, x0: f64, y0: f64, x1: f64, y1: f64, r: f64) -> bool {
    if x < x0 || x > x1 || y < y0 || y > y1 {
        return false;
    }
    let cx = x.clamp(x0 + r, x1 - r);
    let cy = y.clamp(y0 + r, y1 - r);
    let dx = x - cx;
    let dy = y - cy;
    dx * dx + dy * dy <= r * r
}

/// Even-odd point-in-polygon test for a convex-ish cursor outline.
fn in_polygon(x: f64, y: f64, p: &[(f64, f64)]) -> bool {
    let mut inside = false;
    let mut j = p.len() - 1;
    for i in 0..p.len() {
        let (xi, yi) = p[i];
        let (xj, yj) = p[j];
        if ((yi > y) != (yj > y))
            && x < (xj - xi) * (y - yi) / (yj - yi) + xi
        {
            inside = !inside;
        }
        j = i;
    }
    inside
}

#[cfg(test)]
mod tests {
    use super::{agent_args as args, icon_pixel};

    #[test]
    fn agent_always_uses_the_real_model() {
        let argv = args("open chrome", None, "/bin/driver", "/tmp/x.sock", "/tmp/store");
        assert!(
            argv.windows(2).any(|w| w == ["--model", "openai"]),
            "launcher must pass --model openai, never the demo provider"
        );
    }

    #[test]
    fn agent_passes_required_flags_and_target_app() {
        let argv =
            args("open chrome", Some("Google Chrome"), "/bin/driver", "/tmp/x.sock", "/tmp/store");
        for required in ["--goal", "--real", "--model", "--driver", "--socket", "--store"] {
            assert!(
                argv.iter().any(|a| a == required),
                "missing required flag {required}"
            );
        }
        assert!(argv.windows(2).any(|w| w == ["--app", "Google Chrome"]));
    }

    #[test]
    fn menu_icon_is_sea_blue_squircle_with_white_paper_airplane() {
        // Nose of the plane (right-centre): white glyph on the squircle.
        assert_eq!(icon_pixel(29.0, 18.0), (255, 255, 255, 1.0));
        // Top wing tip: still inside the plane's outline.
        assert_eq!(icon_pixel(9.0, 8.0), (255, 255, 255, 1.0));
        // A squircle corner away from the glyph: emerald green #50A574 (80, 165, 116), opaque.
        let (r, g, b, a) = icon_pixel(6.0, 30.0);
        assert_eq!((r, g, b), (80, 165, 116));
        assert_eq!(a, 1.0);
        // Outside the squircle (top-left corner): fully transparent.
        assert_eq!(icon_pixel(1.0, 1.0), (0, 0, 0, 0.0));
    }
}