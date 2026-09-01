//! Active-agent indicator (macOS only, real backend only): a menu-bar status
//! icon plus a floating cursor halo that follows the agent's real cursor.
//!
//! Law 5.2's "the user must always know what the agent is doing" is, at its
//! most literal, a *visual* promise: while the driver runs there is a status
//! item in the menu bar, and the agent's intended pointer is surrounded by a
//! translucent emerald-green halo with an animated sparkle ring so a human
//! can see where the next click will land — without touching the OS cursor
//! itself (the real pointer still moves via CGEvent; this is a HUD overlay,
//! not a synthetic bypass).
//!
//! AppKit requires its UI on the main thread, so this module owns the
//! driver's main thread: the Unix-socket accept loop moves to a worker thread
//! when the real backend starts the indicator (see ``main.rs``). The halo
//! follows the cursor by polling its location on a 60 Hz timer on the main
//! run loop — cheap, and it tracks *any* cursor motion, including moves the
//! agent posts. The sparkle ring animates at 60 Hz with a phase counter so
//! the isilti (shimmer) is smooth and continuous.

#![cfg(target_os = "macos")]

use core::ptr::NonNull;
use std::sync::atomic::{AtomicU64, Ordering};

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

/// Side length of the halo panel, in logical points. Larger than a cursor
/// so the ambient glow + sparkle ring read as "agent presence".
const PANEL: f64 = 80.0;

/// Emerald green (#50A574): the agent's visual signature.
const BRAND_GREEN: (f64, f64, f64) = (80.0 / 255.0, 165.0 / 255.0, 116.0 / 255.0);

/// A lighter emerald tint for the sparkle ring's leading edge.
const BRAND_LIGHT: (f64, f64, f64) = (120.0 / 255.0, 200.0 / 255.0, 150.0 / 255.0);

/// Animation phase counter — increments at 60 Hz so the sparkle rotates
/// smoothly. Wrapped to 0 after a full cycle (360 ticks = 6 seconds).
static PHASE: AtomicU64 = AtomicU64::new(0);

// A borderless, transparent, click-through window that draws the cursor
// halo. The custom view always draws the cursor centred in its bounds; the
// panel itself is repositioned to follow the cursor, so the view needs no
// shared state (Law 6: the drawing is a pure function of the view size +
// the global phase counter).
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
            let phase = PHASE.load(Ordering::Relaxed) as f64;
            draw_cursor(&ctx, cx, cy, phase);
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
// stable AppKit selectors.
fn attach_icon(button: &NSStatusBarButton, icon: &NSImage) {
    let tip = NSString::from_str("Computer Use agent active");
    unsafe {
        let _: () = msg_send![button, setImage: icon];
        let _: () = msg_send![button, setToolTip: &*tip];
    }
}

/// Entry point: run the AppKit main loop with the indicator installed.
pub fn run() -> ! {
    run_loop(true)
}

/// Run halo-only (no status item) when spawned by the menu-bar launcher.
pub fn run_halo() -> ! {
    run_loop(false)
}

/// Shared AppKit main loop. ``show_status`` controls the busy menu-bar item;
/// the animated cursor halo runs regardless.
fn run_loop(show_status: bool) -> ! {
    let mtm = MainThreadMarker::new().expect("indicator must run on the main thread");
    let app = NSApplication::sharedApplication(mtm);
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
    // Keep a raw pointer to the content view so the timer block can trigger
    // redraws for the sparkle animation. The panel and view live for the
    // process lifetime (never deallocated), so the pointer stays valid.
    let content = panel.contentView();
    let view_ref: *const NSView = match content {
        Some(ref view) => view.as_ref() as *const NSView,
        None => core::ptr::null(),
    };
    // 60 Hz timer: follow the cursor AND drive the sparkle animation.
    // The phase counter increments every tick so the rotating sparkle ring
    // is smooth (previously 30 Hz made it feel choppy).
    let timer_block = StackBlock::new(move |_: NonNull<NSTimer>| {
        follow_cursor(&panel_ref);
        // Advance the animation phase and mark the view for redraw.
        let phase = PHASE.fetch_add(1, Ordering::Relaxed) + 1;
        if phase.is_multiple_of(360) {
            PHASE.store(0, Ordering::Relaxed);
        }
        // Trigger a redraw so the sparkle animates even when the cursor is still.
        if !view_ref.is_null() {
            let view: &NSView = unsafe { &*view_ref };
            unsafe {
                let _: () = msg_send![view, setNeedsDisplay: true];
            }
        }
    });
    let _timer = unsafe {
        NSTimer::scheduledTimerWithTimeInterval_repeats_block(1.0 / 60.0, true, &timer_block)
    };

    app.run();
    std::process::exit(0)
}

/// Build the borderless, transparent halo panel above everything else.
fn build_panel(mtm: MainThreadMarker) -> Retained<NSWindow> {
    let frame = CGRect::new(CGPoint::new(0.0, 0.0), CGSize::new(PANEL, PANEL));
    let panel = unsafe {
        NSWindow::initWithContentRect_styleMask_backing_defer(
            NSWindow::alloc(mtm),
            frame,
            NSWindowStyleMask::Borderless,
            NSBackingStoreType::Buffered,
            false,
        )
    };
    panel.setLevel(NSStatusWindowLevel);
    panel.setOpaque(false);
    panel.setBackgroundColor(Some(&NSColor::clearColor()));
    panel.setIgnoresMouseEvents(true);
    panel.setHasShadow(false);
    panel.setCollectionBehavior(
        NSWindowCollectionBehavior::CanJoinAllSpaces | NSWindowCollectionBehavior::FullScreenAuxiliary,
    );
    let view = CursorHaloView::new(frame, mtm);
    panel.setContentView(Some(&view));
    panel
}

/// Reposition the halo panel so its centre sits on the cursor.
fn follow_cursor(panel: &NSWindow) {
    let Some((x, y)) = cursor_location() else {
        return;
    };
    let screen_height = screen_height();
    let origin = CGPoint::new(x - PANEL / 2.0, screen_height - y - PANEL / 2.0);
    panel.setFrameOrigin(origin);
}

/// Current cursor position in the driver's global logical space.
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

// ---------------------------------------------------------------------------
// Drawing: emerald squircle + white airplane + animated sparkle ring
// ---------------------------------------------------------------------------

/// Draw the cursor indicator: ambient glow + brand squircle with airplane
/// + a rotating sparkle ring whose phase is driven by ``phase``.
///
/// The sparkle is a 4-arc ring that rotates around the squircle, with
/// the leading arc brightest (BRAND_LIGHT) and trailing arcs fading —
/// creating the "isilti" (shimmer) effect. At rest (phase=0) the ring is
/// a subtle dotted halo; in motion it reads as a rotating sparkle.
fn draw_cursor(ctx: &CGContext, cx: f64, cy: f64, phase: f64) {
    // --- Layer 1: Soft ambient glow (pulsing) ---
    let pulse = 0.5 + 0.5 * (phase * 0.06).sin();
    draw_disc(ctx, cx, cy, 36.0, 0.06 + 0.04 * pulse);
    draw_disc(ctx, cx, cy, 26.0, 0.12 + 0.06 * pulse);

    // --- Layer 2: Sparkle ring (rotating, animated) ---
    draw_sparkle_ring(ctx, cx, cy, 18.0, phase);

    // --- Layer 3: Brand squircle (#50A574, 22x22, radius 6.5) ---
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

    // Subtle white specular border
    CGContext::begin_path(Some(ctx));
    add_rounded_rect_path(ctx, rect, 6.5);
    CGContext::set_stroke_color_with_color(
        Some(ctx),
        Some(&CGColor::new_srgb(1.0, 1.0, 1.0, 0.40)),
    );
    CGContext::set_line_width(Some(ctx), 0.75);
    CGContext::stroke_path(Some(ctx));

    // --- Layer 4: White paper airplane pointer ---
    CGContext::begin_path(Some(ctx));
    CGContext::move_to_point(Some(ctx), cx + 5.5, cy + 0.0);
    CGContext::add_line_to_point(Some(ctx), cx - 5.5, cy - 5.0);
    CGContext::add_line_to_point(Some(ctx), cx - 2.5, cy + 0.0);
    CGContext::add_line_to_point(Some(ctx), cx - 5.5, cy + 5.0);
    CGContext::close_path(Some(ctx));
    CGContext::set_fill_color_with_color(
        Some(ctx),
        Some(&CGColor::new_srgb(1.0, 1.0, 1.0, 0.98)),
    );
    CGContext::fill_path(Some(ctx));
}

/// Draw a rotating sparkle ring: 4 arcs at 90° intervals around (cx,cy).
///
/// The leading arc (at the rotation angle) is brightest, each trailing arc
/// fades — creating a comet-like shimmer that rotates around the cursor at
/// ~6 seconds per revolution. The ring sits at ``radius`` (outside the
/// squircle) so it reads as a halo, not a border.
fn draw_sparkle_ring(ctx: &CGContext, cx: f64, cy: f64, radius: f64, phase: f64) {
    let rotation = (phase * 1.5).to_radians(); // ~6s per revolution at 60Hz
    let _arc_span = 0.5; // ~28° per arc (reserved for future arc-based rendering)
    let dot_count = 6;
    for i in 0..dot_count {
        let frac = i as f64 / dot_count as f64;
        let angle = rotation + frac * std::f64::consts::TAU;
        // Brightness fades from leading (i=0) to trailing dot.
        let brightness = 1.0 - frac;
        let alpha = 0.15 + 0.55 * brightness;
        let dot_r = 1.5 + 1.0 * brightness;

        let dx = cx + radius * angle.cos();
        let dy = cy + radius * angle.sin();

        CGContext::begin_path(Some(ctx));
        CGContext::add_ellipse_in_rect(
            Some(ctx),
            CGRect::new(
                CGPoint::new(dx - dot_r, dy - dot_r),
                CGSize::new(dot_r * 2.0, dot_r * 2.0),
            ),
        );
        let color = if brightness > 0.7 {
            &CGColor::new_srgb(BRAND_LIGHT.0, BRAND_LIGHT.1, BRAND_LIGHT.2, alpha)
        } else {
            &CGColor::new_srgb(BRAND_GREEN.0, BRAND_GREEN.1, BRAND_GREEN.2, alpha)
        };
        CGContext::set_fill_color_with_color(Some(ctx), Some(color));
        CGContext::fill_path(Some(ctx));
    }
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

/// The menu-bar icon: an SF Symbol loaded straight from the OS.
fn status_icon() -> Retained<NSImage> {
    let name = NSString::from_str("cursorarrow");
    let desc = NSString::from_str("Computer Use agent busy");
    let symbol: Option<Retained<NSImage>> = unsafe {
        msg_send![NSImage::class(), imageWithSystemSymbolName: &*name, accessibilityDescription: &*desc]
    };
    if let Some(image) = symbol {
        image.setTemplate(true);
        return image;
    }
    // Fallback: a plain filled disc (template).
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
