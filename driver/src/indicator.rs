//! Active-agent indicator (macOS only, real backend only): a menu-bar status
//! icon plus a floating cursor halo that follows the agent's real cursor.
//!
//! Law 5.2's "the user must always know what the agent is doing" is, at its
//! most literal, a *visual* promise: while the driver runs there is a status
//! item in the menu bar, and the agent's intended pointer is surrounded by a
//! translucent sea-blue halo so a human can see where the next click will
//! land — without touching the OS cursor itself (the real pointer still moves
//! via CGEvent; this is a HUD overlay, not a synthetic bypass).
//!
//! AppKit requires its UI on the main thread, so this module owns the
//! driver's main thread: the Unix-socket accept loop moves to a worker thread
//! when the real backend starts the indicator (see ``main.rs``). The halo
//! follows the cursor by polling its location on a 30 Hz timer on the main
//! run loop — cheap, and it tracks *any* cursor motion, including moves the
//! agent posts.

#![cfg(target_os = "macos")]

use core::ptr::NonNull;

use block2::StackBlock;
use objc2::rc::Retained;
use objc2::runtime::Bool;
use objc2::{define_class, msg_send, ClassType, MainThreadMarker, MainThreadOnly};
use objc2_app_kit::{
    NSApplication, NSApplicationActivationPolicy, NSBackingStoreType, NSColor, NSGraphicsContext,
    NSImage, NSStatusBar, NSStatusBarButton, NSView, NSWindow, NSWindowCollectionBehavior,
    NSWindowStyleMask, NSStatusWindowLevel,
};
use objc2_core_foundation::{CGRect, CGPoint, CGSize};
use objc2_core_graphics::{CGColor, CGContext};
use objc2_foundation::{NSTimer, NSString};

/// Side length of the halo panel, in logical points. The halo is deliberately
/// larger than a cursor so it reads as the agent's "presence", not a second
/// pointer.
const PANEL: f64 = 72.0;

/// Emerald green (#50A574): the agent's visual signature.
const BRAND_GREEN: (f64, f64, f64) = (80.0 / 255.0, 165.0 / 255.0, 116.0 / 255.0);

// A borderless, transparent, click-through window that draws the cursor
// halo. The custom view always draws the cursor centred in its bounds; the
// panel itself is repositioned to follow the cursor, so the view needs no
// shared state (Law 6: the drawing is a pure function of the view size).
define_class!(
    #[unsafe(super(NSView))]
    pub struct CursorHaloView;

    impl CursorHaloView {
        #[unsafe(method(drawRect:))]
        fn draw_rect(&self, _rect: CGRect) {
            let Some(gc) = NSGraphicsContext::currentContext() else {
                return;
            };
            let ctx = gc.CGContext();
            let bounds = self.bounds();
            let cx = bounds.size.width / 2.0;
            let cy = bounds.size.height / 2.0;
            draw_cursor(&ctx, cx, cy);
        }
    }
);

impl CursorHaloView {
    /// Create the halo view sized to the panel (main-thread only).
    fn new(frame: CGRect, mtm: MainThreadMarker) -> Retained<Self> {
        let this = Self::alloc(mtm);
        let this: Retained<Self> = unsafe { msg_send![this, initWithFrame: frame] };
        this
    }
}

// SAFETY: -[NSStatusBarButton setImage:] and -[NSStatusBarButton setToolTip:]
// are inherited from NSButton and take exactly these argument types; both are
// stable AppKit selectors. (Rust sees no inheritance for inherent methods, so
// the raw message send is the pragmatic way to reach a superclass selector
// from a third-party crate.)
fn attach_icon(button: &NSStatusBarButton, icon: &NSImage) {
    let tip = NSString::from_str("Computer Use agent active");
    unsafe {
        let _: () = msg_send![button, setImage: icon];
        let _: () = msg_send![button, setToolTip: &*tip];
    }
}

/// Entry point: run the AppKit main loop with the indicator installed.
/// Never returns (``NSApplication::run`` blocks until the process is killed,
/// which is exactly the driver's lifetime under ``--real``).
pub fn run() -> ! {
    run_loop(true)
}

/// Run the indicator loop without the menu-bar status item. Used when the
/// driver is spawned by the menu-bar launcher app (``actuation-menu``), which
/// owns the single status icon — the halo overlay stays, so the caller still
/// sees exactly where the agent is acting (Law 5.2), without a second icon.
pub fn run_halo() -> ! {
    run_loop(false)
}

/// Shared AppKit main loop. ``show_status`` controls whether the busy menu-bar
/// item is installed; the cursor halo runs regardless.
fn run_loop(show_status: bool) -> ! {
    let mtm = MainThreadMarker::new().expect("indicator must run on the main thread");
    let app = NSApplication::sharedApplication(mtm);
    // Accessory: a menu-bar utility — no Dock icon, no focus stealing. The
    // agent must never yank the user's focus just by running.
    let _ = app.setActivationPolicy(NSApplicationActivationPolicy::Accessory);

    if show_status {
        let icon = status_icon();
        let status = NSStatusBar::systemStatusBar().statusItemWithLength(30.0);
        if let Some(button) = status.button(mtm) {
            attach_icon(&button, &icon);
        }
    }

    let panel = build_panel(mtm);
    let panel_ref = panel.clone();
    let timer_block = StackBlock::new(move |_: NonNull<NSTimer>| {
        follow_cursor(&panel_ref);
    });
    // 30 Hz polling tracks every cursor motion (agent-posted or human) with
    // zero event-tap complexity; a scheduled timer runs on the current run
    // loop, which is the AppKit main loop here.
    //
    // SAFETY: the block captures only the halo panel, which lives and is
    // invoked on the main thread (the timer fires on the main run loop), so
    // the "block must be sendable" requirement is satisfied in practice.
    let _timer = unsafe {
        NSTimer::scheduledTimerWithTimeInterval_repeats_block(1.0 / 30.0, true, &timer_block)
    };

    app.run();
    std::process::exit(0);
}

/// Build the borderless, transparent halo panel above everything else.
fn build_panel(mtm: MainThreadMarker) -> Retained<NSWindow> {
    let frame = CGRect::new(CGPoint::new(0.0, 0.0), CGSize::new(PANEL, PANEL));
    // SAFETY: window creation requires the caller to manage release-on-close;
    // we keep the Retained handle for the driver's lifetime and the panel is
    // never released early (it lives as long as the process).
    let panel = unsafe {
        NSWindow::initWithContentRect_styleMask_backing_defer(
            NSWindow::alloc(mtm),
            frame,
            NSWindowStyleMask::Borderless,
            NSBackingStoreType::Buffered,
            false,
        )
    };
    // Status-window level: above normal windows *and* full-screen apps, so
    // the halo stays visible no matter what the agent brings to the front.
    panel.setLevel(NSStatusWindowLevel);
    panel.setOpaque(false);
    panel.setBackgroundColor(Some(&NSColor::clearColor()));
    // The halo must never intercept clicks — it is pure presentation.
    panel.setIgnoresMouseEvents(true);
    panel.setHasShadow(false);
    panel.setCollectionBehavior(
        NSWindowCollectionBehavior::CanJoinAllSpaces | NSWindowCollectionBehavior::FullScreenAuxiliary,
    );
    let view = CursorHaloView::new(frame, mtm);
    panel.setContentView(Some(&view));
    panel
}

/// Reposition the halo panel so its centre sits on the cursor. AppKit window
/// coordinates grow from the *bottom*-left of the main screen, while the
/// driver's global coordinates grow from the top-left — flip Y once.
fn follow_cursor(panel: &NSWindow) {
    let Some((x, y)) = cursor_location() else {
        return;
    };
    let screen_height = screen_height();
    let origin = CGPoint::new(x - PANEL / 2.0, screen_height - y - PANEL / 2.0);
    panel.setFrameOrigin(origin);
}

/// Current cursor position in the driver's global logical space (top-left
/// origin, Y grows down) — the same probe CGEvent trick as ``ax.rs``.
fn cursor_location() -> Option<(f64, f64)> {
    use core_graphics::event_source::{CGEventSource, CGEventSourceStateID};
    use core_graphics::event::CGEvent;

    let source = CGEventSource::new(CGEventSourceStateID::HIDSystemState).ok()?;
    let event = CGEvent::new(source).ok()?;
    let location = event.location();
    Some((location.x, location.y))
}

/// Height of the main display in logical points (for the Y-axis flip).
fn screen_height() -> f64 {
    core_graphics::display::CGDisplay::main().bounds().size.height
}

/// Draw the cursor indicator matching the top-bar brand (#50A574 squircle
/// with white paper airplane pointer) at the panel centre.
fn draw_cursor(ctx: &CGContext, cx: f64, cy: f64) {
    // Soft outer ambient glow
    draw_disc(ctx, cx, cy, 32.0, 0.15);
    draw_disc(ctx, cx, cy, 22.0, 0.25);

    // #50A574 squircle matching top-bar mark (22x22, radius 6.5)
    let size = 22.0;
    let rect = CGRect::new(
        CGPoint::new(cx - size / 2.0, cy - size / 2.0),
        CGSize::new(size, size),
    );
    CGContext::begin_path(Some(ctx));
    add_rounded_rect_path(ctx, rect, 6.5);
    CGContext::set_fill_color_with_color(
        Some(ctx),
        Some(&CGColor::new_srgb(BRAND_GREEN.0, BRAND_GREEN.1, BRAND_GREEN.2, 0.95)),
    );
    CGContext::fill_path(Some(ctx));

    // Subtle white specular border on the squircle
    CGContext::begin_path(Some(ctx));
    add_rounded_rect_path(ctx, rect, 6.5);
    CGContext::set_stroke_color_with_color(
        Some(ctx),
        Some(&CGColor::new_srgb(1.0, 1.0, 1.0, 0.40)),
    );
    CGContext::set_line_width(Some(ctx), 0.75);
    CGContext::stroke_path(Some(ctx));

    // White paper airplane pointer inside the squircle
    CGContext::begin_path(Some(ctx));
    CGContext::move_to_point(Some(ctx), cx + 5.5, cy + 0.0); // airplane nose / tip
    CGContext::add_line_to_point(Some(ctx), cx - 5.5, cy - 5.0); // bottom wing
    CGContext::add_line_to_point(Some(ctx), cx - 2.5, cy + 0.0); // inner notch
    CGContext::add_line_to_point(Some(ctx), cx - 5.5, cy + 5.0); // top wing
    CGContext::close_path(Some(ctx));
    CGContext::set_fill_color_with_color(
        Some(ctx),
        Some(&CGColor::new_srgb(1.0, 1.0, 1.0, 0.98)),
    );
    CGContext::fill_path(Some(ctx));
}

/// Helper to add a rounded rectangle path to a CGContext.
fn add_rounded_rect_path(ctx: &CGContext, rect: CGRect, radius: f64) {
    let min_x = rect.origin.x;
    let min_y = rect.origin.y;
    let max_x = rect.origin.x + rect.size.width;
    let max_y = rect.origin.y + rect.size.height;
    let mid_x = rect.origin.x + rect.size.width / 2.0;
    let mid_y = rect.origin.y + rect.size.height / 2.0;

    CGContext::move_to_point(Some(ctx), min_x, mid_y);
    CGContext::add_arc_to_point(Some(ctx), min_x, min_y, mid_x, min_y, radius);
    CGContext::add_arc_to_point(Some(ctx), max_x, min_y, max_x, mid_y, radius);
    CGContext::add_arc_to_point(Some(ctx), max_x, max_y, mid_x, max_y, radius);
    CGContext::add_arc_to_point(Some(ctx), min_x, max_y, min_x, mid_y, radius);
    CGContext::close_path(Some(ctx));
}

/// Fill a translucent emerald disc of the given radius around (cx, cy).
fn draw_disc(ctx: &CGContext, cx: f64, cy: f64, radius: f64, alpha: f64) {
    CGContext::begin_path(Some(ctx));
    CGContext::add_ellipse_in_rect(
        Some(ctx),
        CGRect::new(
            CGPoint::new(cx - radius, cy - radius),
            CGSize::new(radius * 2.0, radius * 2.0),
        ),
    );
    CGContext::set_fill_color_with_color(
        Some(ctx),
        Some(&CGColor::new_srgb(BRAND_GREEN.0, BRAND_GREEN.1, BRAND_GREEN.2, alpha)),
    );
    CGContext::fill_path(Some(ctx));
}

/// The menu-bar icon: an SF Symbol loaded straight from the OS. Drawing an
/// ``NSImage`` programmatically for a status item proved unreliable in
/// practice (the CGContext block can produce an empty image → invisible
/// icon), so we let AppKit supply a guaranteed template glyph instead. The
/// sleeping vs. active distinction is a dash/dot suffixed symbol name, but
/// both render adaptively on any menu bar.
fn status_icon() -> Retained<NSImage> {
    let name = NSString::from_str("cursorarrow");
    let desc = NSString::from_str("Computer Use agent busy");
    // SAFETY: standard NSImage class factory; returns nil only on pre-11 macOS.
    let symbol: Option<Retained<NSImage>> = unsafe {
        msg_send![NSImage::class(), imageWithSystemSymbolName: &*name, accessibilityDescription: &*desc]
    };
    if let Some(image) = symbol {
        image.setTemplate(true);
        return image;
    }
    // Fallback: a plain filled disc (template) so the busy state never vanishes.
    let size = CGSize::new(18.0, 18.0);
    let block = StackBlock::new(|_rect: CGRect| -> Bool {
        let Some(gc) = NSGraphicsContext::currentContext() else {
            return Bool::new(false);
        };
        let ctx = gc.CGContext();
        draw_disc(&ctx, 9.0, 9.0, 8.0, 1.0);
        Bool::new(true)
    });
    let image = NSImage::imageWithSize_flipped_drawingHandler(size, false, &block);
    image.setTemplate(true);
    image
}
