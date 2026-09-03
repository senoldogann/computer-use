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
    NSImage, NSMenu, NSMenuItem, NSScreen, NSStatusBar, NSWindow, NSWindowButton,
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
    /// A structured step record from the agent (see `trace::EVENT_PREFIX`).
    /// Carried as its own kind so the panel can render the plan, the model's
    /// reasoning and the verification verdict as UI instead of as log text.
    Event,
    /// stdout — the CLI's summary block, most important.
    Out,
    /// stderr — the runner's INFO step log, secondary.
    Dim,
    /// a launcher-side error, styled red.
    Err,
}

struct Shared {
    lines: VecDeque<(LineKind, String)>,
    child_pid: Option<u32>,
    // Piped stdin of the agent child: the panel's Approve/Deny buttons write
    // the human answer here when the CLI is blocked on a Law 5.1 confirmation
    // (M6). Dropped when the run ends or is stopped so a blocked read gets EOF
    // instead of a dangling writer.
    child_stdin: Option<std::process::ChildStdin>,
    exit: Option<Option<i32>>,
    signalled: bool,
}

static SHARED: Mutex<Shared> = Mutex::new(Shared {
    lines: VecDeque::new(),
    child_pid: None,
    child_stdin: None,
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

/// Pointer to the webview so native menu actions can trigger JS callbacks.
static WEBVIEW_PTR: core::sync::atomic::AtomicPtr<core::ffi::c_void> =
    core::sync::atomic::AtomicPtr::new(core::ptr::null_mut());

/// Active autonomy level (3 = Full Autonomy, 2 = Guarded Mode).
static AUTONOMY_LEVEL: core::sync::atomic::AtomicU8 = core::sync::atomic::AtomicU8::new(3);

/// Whether MCP servers are active for agent runs.
static MCP_ENABLED: core::sync::atomic::AtomicBool = core::sync::atomic::AtomicBool::new(true);

/// Whether the panel floats at the bottom-center (Claude style) or near the status item.
static FLOATING_BOTTOM: core::sync::atomic::AtomicBool = core::sync::atomic::AtomicBool::new(true);

/// True while an agent child process is running; cleared by the reaper the
/// moment `wait()` returns.
///
/// The stop watchdog reads this rather than probing the pid with `kill(pid, 0)`,
/// because a pid is only meaningful until the OS reclaims it: a child that
/// exits and is reaped inside the grace window frees its number, and escalating
/// to SIGKILL on a recycled pid would kill a stranger's process. Only one agent
/// runs at a time (the launcher refuses a second), so one flag is the whole
/// state.
static CHILD_ALIVE: core::sync::atomic::AtomicBool = core::sync::atomic::AtomicBool::new(false);

/// How long a stopped agent gets to shut itself down before it is killed.
///
/// Not politeness. SIGINT is what the orchestrator's kill-switch listens for,
/// and catching it is what writes the failed episode and its retrospective —
/// Law 4.1 requires a run ended by takeover to be *recorded*, and SIGKILL
/// cannot be caught, so the previous straight-to-SIGKILL stop threw away
/// everything the run had learned. Five seconds is long enough for the loop to
/// unwind its current step and flush that to disk, short enough that a user
/// who pressed Stop never wonders whether it worked.
const STOP_GRACE_SECONDS: u64 = 5;

/// How often the watchdog re-checks, so a run that unwinds in 200ms is not
/// reported as stopped five seconds later.
const STOP_POLL_MS: u64 = 100;

fn mcp_config_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/Users/me".to_string());
    PathBuf::from(home).join(".computeruse").join("mcp.json")
}

fn read_mcp_servers_json() -> String {
    let path = mcp_config_path();
    if let Ok(content) = std::fs::read_to_string(path) {
        content
    } else {
        "{\"mcpServers\":{}}".to_string()
    }
}

/// Frontmost app captured just before the panel activates, so a run can target
/// whatever the user was using (a typed app field wins over this).
static CAPTURED_APP: Mutex<Option<String>> = Mutex::new(None);

/// True only when ``pid`` names a live process running the *same executable*
/// as this launcher. A crash leaves the pid file behind, and that pid can
/// later be reused by an *unrelated* process — signaling it would terminate
/// the wrong program (M5). ``proc_name`` gives the pid's executable name;
/// SIGTERM is only ever sent after this identity check passes.
///
/// The name is compared against our *own* executable name plus the known
/// launcher names: the packaged app renames the binary to ``ComputerUse``
/// while the dev build runs as ``actuation-menu``, so a dev binary must
/// recognize a running packaged app (and vice versa) or two menu instances
/// could coexist. Comparing to ourselves also keeps the check working if the
/// bundle is renamed again.
fn process_is_menu_launcher(pid: i32) -> bool {
    let mut name = [0u8; 64];
    let len = unsafe { libc::proc_name(pid, name.as_mut_ptr().cast(), name.len() as u32) };
    if len <= 0 {
        return false; // ESRCH or no permission — never signal an unknown pid.
    }
    let candidate = String::from_utf8_lossy(&name[..len as usize])
        .trim_end_matches('\0')
        .trim()
        .to_string();
    let mut self_name = [0u8; 64];
    let self_len = unsafe {
        libc::proc_name(
            std::process::id() as i32,
            self_name.as_mut_ptr().cast(),
            self_name.len() as u32,
        )
    };
    if self_len > 0 {
        let mine_cow = String::from_utf8_lossy(&self_name[..self_len as usize]);
        let mine = mine_cow.trim_end_matches('\0').trim();
        if !mine.is_empty() && candidate == mine {
            return true;
        }
    }
    matches!(candidate.as_str(), "actuation-menu" | "ComputerUse")
}

/// Ensures only one instance of the menu launcher runs at any given time.
fn ensure_single_instance() {
    let pid_file = "/tmp/actuation-menu.pid";
    if let Ok(content) = std::fs::read_to_string(pid_file) {
        if let Ok(old_pid) = content.trim().parse::<i32>() {
            let my_pid = std::process::id() as i32;
            if old_pid != my_pid && process_is_menu_launcher(old_pid) {
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
    WEBVIEW_PTR.store(
        (&*webview as *const WKWebView).cast_mut().cast(),
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

        #[unsafe(method(toggleFloatingMode:))]
        fn toggle_floating_mode(&self, _sender: *mut AnyObject) {
            let current = FLOATING_BOTTOM.load(core::sync::atomic::Ordering::SeqCst);
            FLOATING_BOTTOM.store(!current, core::sync::atomic::Ordering::SeqCst);
            let ptr = PANEL_PTR.load(core::sync::atomic::Ordering::SeqCst);
            if !ptr.is_null() {
                let panel = unsafe { &*(ptr as *const NSWindow) };
                reposition_panel(panel);
            }
            let wv_ptr = WEBVIEW_PTR.load(core::sync::atomic::Ordering::SeqCst);
            if !wv_ptr.is_null() {
                let wv = unsafe { &*(wv_ptr as *const WKWebView) };
                let js = format!("if(window.onPositionModeChanged)window.onPositionModeChanged({});", !current);
                call_js(wv, &js);
            }
        }

        #[unsafe(method(toggleMcpServer:))]
        fn toggle_mcp_server(&self, _sender: *mut AnyObject) {
            let current = MCP_ENABLED.load(core::sync::atomic::Ordering::SeqCst);
            MCP_ENABLED.store(!current, core::sync::atomic::Ordering::SeqCst);
            let wv_ptr = WEBVIEW_PTR.load(core::sync::atomic::Ordering::SeqCst);
            if !wv_ptr.is_null() {
                let wv = unsafe { &*(wv_ptr as *const WKWebView) };
                let js = format!("if(window.onMcpToggled)window.onMcpToggled({});", !current);
                call_js(wv, &js);
            }
        }

        #[unsafe(method(setFullAutonomy:))]
        fn set_full_autonomy(&self, _sender: *mut AnyObject) {
            AUTONOMY_LEVEL.store(3, core::sync::atomic::Ordering::SeqCst);
            let ptr = WEBVIEW_PTR.load(core::sync::atomic::Ordering::SeqCst);
            if !ptr.is_null() {
                let wv = unsafe { &*(ptr as *const WKWebView) };
                call_js(wv, "if(window.setAutonomyLevel)window.setAutonomyLevel(3);");
            }
        }

        #[unsafe(method(setGuardedAutonomy:))]
        fn set_guarded_autonomy(&self, _sender: *mut AnyObject) {
            AUTONOMY_LEVEL.store(2, core::sync::atomic::Ordering::SeqCst);
            let ptr = WEBVIEW_PTR.load(core::sync::atomic::Ordering::SeqCst);
            if !ptr.is_null() {
                let wv = unsafe { &*(ptr as *const WKWebView) };
                call_js(wv, "if(window.setAutonomyLevel)window.setAutonomyLevel(2);");
            }
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

    let target = ScriptTarget::alloc(mtm);
    let target: Retained<ScriptTarget> = unsafe { msg_send![target, init] };
    let target_ptr: *mut ScriptTarget = Retained::into_raw(target);

    let is_full = AUTONOMY_LEVEL.load(core::sync::atomic::Ordering::SeqCst) == 3;
    let full_item = unsafe {
        let item = NSMenuItem::initWithTitle_action_keyEquivalent(
            NSMenuItem::alloc(mtm),
            &NSString::from_str("Full Autonomy (Tam Otonomi)"),
            Some(sel!(setFullAutonomy:)),
            &NSString::from_str(""),
        );
        let _: () = msg_send![&*item, setState: if is_full { 1isize } else { 0isize }];
        let _: () = msg_send![&*item, setTarget: target_ptr];
        item
    };
    menu.addItem(&full_item);

    let guarded_item = unsafe {
        let item = NSMenuItem::initWithTitle_action_keyEquivalent(
            NSMenuItem::alloc(mtm),
            &NSString::from_str("Guarded Mode (Denetimli)"),
            Some(sel!(setGuardedAutonomy:)),
            &NSString::from_str(""),
        );
        let _: () = msg_send![&*item, setState: if !is_full { 1isize } else { 0isize }];
        let _: () = msg_send![&*item, setTarget: target_ptr];
        item
    };
    menu.addItem(&guarded_item);

    menu.addItem(&NSMenuItem::separatorItem(mtm));

    let is_bottom = FLOATING_BOTTOM.load(core::sync::atomic::Ordering::SeqCst);
    let float_item = unsafe {
        let item = NSMenuItem::initWithTitle_action_keyEquivalent(
            NSMenuItem::alloc(mtm),
            &NSString::from_str("Floating Pill (Dock Üstü)"),
            Some(sel!(toggleFloatingMode:)),
            &NSString::from_str(""),
        );
        let _: () = msg_send![&*item, setState: if is_bottom { 1isize } else { 0isize }];
        let _: () = msg_send![&*item, setTarget: target_ptr];
        item
    };
    menu.addItem(&float_item);

    let is_mcp = MCP_ENABLED.load(core::sync::atomic::Ordering::SeqCst);
    let mcp_item = unsafe {
        let item = NSMenuItem::initWithTitle_action_keyEquivalent(
            NSMenuItem::alloc(mtm),
            &NSString::from_str(if is_mcp { "MCP Sunucuları (Açık)" } else { "MCP Sunucuları (Kapalı)" }),
            Some(sel!(toggleMcpServer:)),
            &NSString::from_str(""),
        );
        let _: () = msg_send![&*item, setState: if is_mcp { 1isize } else { 0isize }];
        let _: () = msg_send![&*item, setTarget: target_ptr];
        item
    };
    menu.addItem(&mcp_item);

    menu.addItem(&NSMenuItem::separatorItem(mtm));

    let toggle_item = unsafe {
        NSMenuItem::initWithTitle_action_keyEquivalent(
            NSMenuItem::alloc(mtm),
            &NSString::from_str("Open / Close Panel"),
            Some(sel!(togglePanel:)),
            &NSString::from_str(""),
        )
    };
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
        reposition_panel(panel);
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
        let frame = CGRect::new(CGPoint::new(0.0, 0.0), CGSize::new(480.0, 640.0));
        let webview = WKWebView::initWithFrame_configuration(WKWebView::alloc(mtm), frame, &config);
        let html = NSString::from_str(include_str!("../assets/menu.html"));
        let base = NSURL::fileURLWithPath(&NSString::from_str("/"));
        webview.loadHTMLString_baseURL(&html, Some(&base));
        webview
    }
}

fn build_panel(mtm: MainThreadMarker) -> Retained<NSWindow> {
    let frame = CGRect::new(CGPoint::new(0.0, 0.0), CGSize::new(480.0, 640.0));
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
    unsafe {
        panel.setReleasedWhenClosed(false);
        // Float gracefully above regular windows (NSFloatingWindowLevel = 3).
        let _: () = msg_send![&*panel, setLevel: 3isize];
    };
    panel.setTitlebarAppearsTransparent(true);
    panel.setTitleVisibility(NSWindowTitleVisibility::Hidden);
    for button in [
        NSWindowButton::CloseButton,
        NSWindowButton::MiniaturizeButton,
        NSWindowButton::ZoomButton,
    ] {
        if let Some(btn) = panel.standardWindowButton(button) {
            unsafe {
                let _: () = msg_send![&*btn, setHidden: true];
            }
        }
    }
    panel.setOpaque(false);
    panel.setBackgroundColor(Some(&NSColor::clearColor()));
    panel.setHasShadow(true);
    panel.setMovableByWindowBackground(true);
    panel.setHidesOnDeactivate(false);
    panel.setCollectionBehavior(NSWindowCollectionBehavior::CanJoinAllSpaces);
    panel
}

fn position_bottom_center(panel: &NSWindow) {
    let mtm = MainThreadMarker::new().expect("main thread");
    let (screen_w, dock_y) = if let Some(screen) = NSScreen::mainScreen(mtm) {
        let vf = screen.visibleFrame();
        (screen.frame().size.width, vf.origin.y)
    } else {
        use core_graphics::display::CGDisplay;
        let b = CGDisplay::main().bounds();
        (b.size.width, 80.0)
    };
    let panel_w = 480.0;
    let x = (screen_w - panel_w) / 2.0;
    let y = dock_y + 20.0;
    panel.setFrameOrigin(CGPoint::new(x, y));
}

fn reposition_panel(panel: &NSWindow) {
    if FLOATING_BOTTOM.load(core::sync::atomic::Ordering::SeqCst) {
        position_bottom_center(panel);
    } else {
        position_near_status_item(panel);
    }
}

fn position_near_status_item(panel: &NSWindow) {
    use core_graphics::display::CGDisplay;
    let screen = CGDisplay::main().bounds(); // bottom-left origin
    let width = screen.size.width;
    let height = screen.size.height;
    let menu_bar: f64 = 24.0;
    // Top-right, just under the menu bar.
    let origin = CGPoint::new(width - 480.0 - 12.0, height - menu_bar - 640.0 - 8.0);
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
                LineKind::Event => "window.Native.event(%s);",
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
        // Run over: drop the pipe so any blocked confirmation read gets EOF
        // (the child is gone anyway; this just keeps the handle honest).
        s.child_stdin = None;
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
            let level = value
                .get("level")
                .and_then(serde_json::Value::as_u64)
                .map(|v| v as u8)
                .unwrap_or_else(|| AUTONOMY_LEVEL.load(core::sync::atomic::Ordering::SeqCst));
            AUTONOMY_LEVEL.store(level, core::sync::atomic::Ordering::SeqCst);
            run_agent(&goal, app.as_deref(), level);
        }
        Some("set_level") => {
            if let Some(level) = value.get("level").and_then(serde_json::Value::as_u64) {
                AUTONOMY_LEVEL.store(level as u8, core::sync::atomic::Ordering::SeqCst);
            }
        }
        Some("get_mcp") => {
            let json_str = read_mcp_servers_json();
            let enabled = MCP_ENABLED.load(core::sync::atomic::Ordering::SeqCst);
            let ptr = WEBVIEW_PTR.load(core::sync::atomic::Ordering::SeqCst);
            if !ptr.is_null() {
                let wv = unsafe { &*(ptr as *const WKWebView) };
                let js = format!("if(window.onMcpLoaded)window.onMcpLoaded({json_str},{enabled});");
                call_js(wv, &js);
            }
        }
        Some("save_mcp") => {
            if let Some(id) = value.get("id").and_then(serde_json::Value::as_str) {
                let path = mcp_config_path();
                let mut doc: serde_json::Value = std::fs::read_to_string(&path)
                    .ok()
                    .and_then(|s| serde_json::from_str(&s).ok())
                    .unwrap_or_else(|| serde_json::json!({"mcpServers": {}}));
                if !doc.get("mcpServers").is_some_and(|v| v.is_object()) {
                    doc["mcpServers"] = serde_json::json!({});
                }
                let mut server_obj = serde_json::json!({
                    "command": value.get("command").and_then(serde_json::Value::as_str).unwrap_or("npx"),
                    "args": value.get("args").cloned().unwrap_or_else(|| serde_json::json!([])),
                });
                if let Some(env_obj) = value.get("env") {
                    if env_obj.is_object() && !env_obj.as_object().unwrap().is_empty() {
                        server_obj["env"] = env_obj.clone();
                    }
                }
                doc["mcpServers"][id] = server_obj;
                if let Some(parent) = path.parent() {
                    let _ = std::fs::create_dir_all(parent);
                }
                if let Ok(formatted) = serde_json::to_string_pretty(&doc) {
                    let _ = std::fs::write(&path, formatted);
                }
                let ptr = WEBVIEW_PTR.load(core::sync::atomic::Ordering::SeqCst);
                if !ptr.is_null() {
                    let wv = unsafe { &*(ptr as *const WKWebView) };
                    let js = format!("if(window.onMcpSaved)window.onMcpSaved('{id}');");
                    call_js(wv, &js);
                }
            }
        }
        Some("delete_mcp") => {
            if let Some(id) = value.get("id").and_then(serde_json::Value::as_str) {
                let path = mcp_config_path();
                if let Ok(content) = std::fs::read_to_string(&path) {
                    if let Ok(mut doc) = serde_json::from_str::<serde_json::Value>(&content) {
                        if let Some(servers) = doc.get_mut("mcpServers").and_then(|s| s.as_object_mut()) {
                            servers.remove(id);
                            if let Ok(formatted) = serde_json::to_string_pretty(&doc) {
                                let _ = std::fs::write(&path, formatted);
                            }
                        }
                    }
                }
                let ptr = WEBVIEW_PTR.load(core::sync::atomic::Ordering::SeqCst);
                if !ptr.is_null() {
                    let wv = unsafe { &*(ptr as *const WKWebView) };
                    let js = format!("if(window.onMcpDeleted)window.onMcpDeleted('{id}');");
                    call_js(wv, &js);
                }
            }
        }
        Some("set_mcp_enabled") => {
            if let Some(enabled) = value.get("enabled").and_then(serde_json::Value::as_bool) {
                MCP_ENABLED.store(enabled, core::sync::atomic::Ordering::SeqCst);
            }
        }
        Some("set_position_mode") => {
            if let Some(bottom) = value.get("bottom").and_then(serde_json::Value::as_bool) {
                FLOATING_BOTTOM.store(bottom, core::sync::atomic::Ordering::SeqCst);
                let ptr = PANEL_PTR.load(core::sync::atomic::Ordering::SeqCst);
                if !ptr.is_null() {
                    let panel = unsafe { &*(ptr as *const NSWindow) };
                    reposition_panel(panel);
                }
            }
        }
        Some("stop") => stop_agent(),
        Some("confirm") => {
            // Law 5.1: the panel's Approve/Deny answer for an action the agent
            // paused on. Written to the child's piped stdin; the CLI's confirm
            // handler reads exactly one line. No stdin (no run in flight, or
            // nothing waiting) means the message is a no-op.
            let answer = value
                .get("answer")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("n")
                .to_string();
            use std::io::Write;
            let mut s = SHARED.lock().unwrap();
            if let Some(stdin) = s.child_stdin.as_mut() {
                let _ = writeln!(stdin, "{answer}");
                let _ = stdin.flush();
            }
        }
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
    level: u8,
    mcp: bool,
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
        level.to_string(),
    ];
    if mcp {
        args.push("--mcp".to_string());
    }
    if let Some(app_name) = app {
        args.extend(["--app".to_string(), app_name.to_string()]);
    }
    args
}

fn run_agent(goal: &str, app: Option<&str>, level: u8) {
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
    let mcp = MCP_ENABLED.load(core::sync::atomic::Ordering::SeqCst);
    cmd.args(agent_args(goal, app, &driver_bin_str, &socket, &store, level, mcp));
    // The launcher owns the status icon; the spawned driver stays halo-only.
    cmd.env("COMPUTERUSE_NO_STATUS", "1");
    cmd.env("OPENAI_API_KEY", key.expect("checked above"));
    // Piped stdin is the panel's confirmation channel (M6): the CLI uses the
    // interactive confirm handler whenever COMPUTERUSE_MENU is set, and this
    // pipe carries the Approve/Deny answer back to the blocked read.
    cmd.env("COMPUTERUSE_MENU", "1");
    cmd.stdin(Stdio::piped());
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
    let stdin = child.stdin.take();
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    {
        let mut s = SHARED.lock().unwrap();
        s.child_pid = Some(pid);
        s.child_stdin = stdin;
        s.exit = None;
        s.signalled = false;
    }
    CHILD_ALIVE.store(true, core::sync::atomic::Ordering::SeqCst);
    if let Some(reader) = stdout {
        std::thread::spawn(move || pump_lines(BufReader::new(reader), true));
    }
    if let Some(reader) = stderr {
        std::thread::spawn(move || pump_lines(BufReader::new(reader), false));
    }
    std::thread::spawn(move || {
        let code = child.wait().ok().and_then(|s| s.code());
        // Cleared before the exit code is published: the stop watchdog must
        // never see a reaped pid as still running.
        CHILD_ALIVE.store(false, core::sync::atomic::Ordering::SeqCst);
        let mut g = SHARED.lock().unwrap();
        g.exit = Some(code);
    });
}

/// Read a child pipe line-by-line and enqueue each line for the UI.
/// Marks a stdout line as a structured step record rather than log prose.
/// Must match `computeruse.orchestrator.trace.EVENT_PREFIX`.
const EVENT_PREFIX: &str = "@@CU ";

fn pump_lines(reader: impl BufRead, is_stdout: bool) {
    let kind = if is_stdout { LineKind::Out } else { LineKind::Dim };
    for line in reader.lines().map_while(Result::ok) {
        let trimmed = line.trim_end().to_string();
        if trimmed.is_empty() {
            continue;
        }
        // Structured events arrive on the same stream as the log, tagged so
        // the two can never be confused with each other.
        match trimmed.strip_prefix(EVENT_PREFIX) {
            Some(payload) => SHARED
                .lock()
                .unwrap()
                .lines
                .push_back((LineKind::Event, payload.to_string())),
            None => SHARED.lock().unwrap().lines.push_back((kind, trimmed)),
        }
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
            // Drop the stdin pipe first so a pending confirmation read gets EOF
            // (fail-closed: the blocked agent resolves "no answer" instead of
            // hanging), then terminate the child and its whole process group.
            let mut s = SHARED.lock().unwrap();
            s.child_stdin = None;
            drop(s);
            // SIGINT first, never SIGKILL: the orchestrator installs a SIGINT
            // catcher that trips its kill switch, and *that* path is what
            // records the interrupted run as a failed episode carrying a
            // retrospective (Law 4.1). SIGKILL cannot be caught, so it ends the
            // process with everything the run learned still in memory.
            // SAFETY: signalling the child's process group and the child.
            unsafe {
                libc::killpg(pid as i32, libc::SIGINT);
                libc::kill(pid as i32, libc::SIGINT);
            }
            push_dim("— stopping agent, saving what it learned —".to_string());
            // The escalation runs off the UI thread: this is called from a
            // message handler, and waiting out the grace period here would
            // freeze the panel for as long as the agent takes to unwind.
            std::thread::spawn(move || {
                let polls = STOP_GRACE_SECONDS * 1000 / STOP_POLL_MS;
                for _ in 0..polls {
                    std::thread::sleep(std::time::Duration::from_millis(STOP_POLL_MS));
                    if !CHILD_ALIVE.load(core::sync::atomic::Ordering::SeqCst) {
                        push_dim("— agent stopped by user —".to_string());
                        return;
                    }
                }
                // Out of grace. A run that ignored SIGINT is wedged somewhere
                // it cannot return from, and the user asked for the machine
                // back — which outranks the retrospective.
                // SAFETY: CHILD_ALIVE is still set, so this pid is the child's
                // and has not been recycled.
                unsafe {
                    libc::killpg(pid as i32, libc::SIGKILL);
                    libc::kill(pid as i32, libc::SIGKILL);
                }
                push_err(format!(
                    "agent did not stop within {STOP_GRACE_SECONDS}s; killed"
                ));
            });
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
        let argv = args("open chrome", None, "/bin/driver", "/tmp/x.sock", "/tmp/store", 3, true);
        assert!(
            argv.windows(2).any(|w| w == ["--model", "openai"]),
            "launcher must pass --model openai, never the demo provider"
        );
        assert!(argv.iter().any(|a| a == "--mcp"), "must pass --mcp when enabled");
    }

    #[test]
    fn agent_passes_required_flags_and_target_app() {
        let argv =
            args("open chrome", Some("Google Chrome"), "/bin/driver", "/tmp/x.sock", "/tmp/store", 3, false);
        for required in ["--goal", "--real", "--model", "--driver", "--socket", "--store"] {
            assert!(
                argv.iter().any(|a| a == required),
                "missing required flag {required}"
            );
        }
        assert!(argv.windows(2).any(|w| w == ["--app", "Google Chrome"]));
        assert!(!argv.iter().any(|a| a == "--mcp"), "must not pass --mcp when disabled");
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