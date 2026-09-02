//! Real macOS Accessibility (AX) tree traversal — ADR-2's *primary* source.
//!
//! The orchestrator's pixels verify, but they cannot *generate*: knowing where
//! a UI element is (its role, title, exact coordinates) is the Accessibility
//! API's job. This module walks ``AXUIElementCreateApplication(pid)`` down to
//! ``max_depth`` and maps the tree into the platform-neutral
//! :struct:`HostElement` shape the orchestrator consumes.
//!
//! AX introspection is read-only (copy attribute values), so unlike event
//! posting it cannot lock up the host — but it lives in the driver process
//! anyway, keeping every macOS host API behind the one ADR-1 socket boundary.
//!
//! Coordinates come back in the *same global logical point space* the
//! orchestrator's coordinate layer uses (origin at the primary display's
//! top-left, Y grows down), so an element's rect plugs straight into
//! ``verification_region`` / capture cropping with no transform.
//!
//! Like event posting, AX reads require Accessibility consent; without it the
//! API returns ``kAXErrorAPIDisabled`` and we fail loudly (Law 5/6.3) rather
//! than returning an empty tree the orchestrator would misread as \"no UI\".
//!
//! This file is macOS-only (gated in ``lib.rs``).

use core::ffi::c_void;

use core_foundation::array::{CFArray, CFArrayRef};
use core_foundation::base::{CFType, TCFType};
use core_foundation::boolean::CFBoolean;
use core_foundation::dictionary::{CFDictionary, CFDictionaryRef};
use core_foundation::number::CFNumber;
use core_foundation::string::CFString;
use core_graphics::event::CGEvent;
use core_graphics::event_source::{CGEventSource, CGEventSourceStateID};
use core_graphics::geometry::{CGPoint, CGSize};
use core_graphics::window::{
    copy_window_info, kCGNullWindowID, kCGWindowLayer, kCGWindowListExcludeDesktopElements,
    kCGWindowListOptionOnScreenOnly, kCGWindowOwnerName, kCGWindowOwnerPID,
};

use crate::backend::{BackendError, FocusedWindow, HostElement, NodeBudget};

// AX C API (ApplicationServices framework). Declared directly — the
// `accessibility` crate adds its own API surface; these seven symbols are
// stable and small enough to own.
#[link(name = "ApplicationServices", kind = "framework")]
extern "C" {
    fn AXUIElementCreateApplication(pid: i32) -> CFTypeRef;
    fn AXUIElementCreateSystemWide() -> CFTypeRef;
    fn AXUIElementCopyAttributeValue(
        element: CFTypeRef,
        attribute: CFTypeRef,
        value: *mut CFTypeRef,
    ) -> i32;
    fn AXUIElementGetPid(element: CFTypeRef, pid: *mut i32) -> i32;
    fn AXValueGetValue(value: CFTypeRef, the_type: u32, value_ptr: *mut c_void) -> bool;
    fn AXIsProcessTrusted() -> bool;
}

type CFTypeRef = *const c_void;

/// AX error codes and value types we depend on (stable ABI constants).
const AX_ERROR_SUCCESS: i32 = 0;
const AX_VALUE_CG_POINT: u32 = 1;
const AX_VALUE_CG_SIZE: u32 = 2;

/// Copy one attribute from an AX element; ``None`` when the attribute is
/// absent/unsupported (normal for e.g. a title-less container) or on error.
fn copy_attribute(element: CFTypeRef, name: &str) -> Option<CFType> {
    copy_attribute_coded(element, name).0
}

/// Copy one attribute and return the raw AX error code alongside the value.
///
/// ``(Some(value), 0)`` on success; ``(None, code)`` when the attribute is
/// absent or the call failed. The code lets a caller distinguish a legitimately
/// empty attribute from an API-level failure — notably ``kAXErrorAPIDisabled``
/// (consent missing/revoked), which a bare ``None`` would otherwise read as
/// "the system really has no such element" and send the user chasing a ghost.
fn copy_attribute_coded(element: CFTypeRef, name: &str) -> (Option<CFType>, i32) {
    let key = CFString::new(name);
    let mut value: CFTypeRef = core::ptr::null();
    let error = unsafe {
        AXUIElementCopyAttributeValue(
            element,
            key.as_concrete_TypeRef() as CFTypeRef,
            &mut value,
        )
    };
    if error == AX_ERROR_SUCCESS && !value.is_null() {
        // Get-rule: the copy is ours, wrap without bumping.
        (Some(unsafe { CFType::wrap_under_get_rule(value) }), error)
    } else {
        (None, error)
    }
}

/// Read a string attribute (role/title) as an owned String.
fn string_attribute(element: CFTypeRef, name: &str) -> Option<String> {
    copy_attribute(element, name).and_then(|value| value.downcast::<CFString>())
        .map(|s| s.to_string())
}

/// Read ``AXPosition``/``AXSize`` (wrapped AXValues) as a CGPoint/CGSize.
fn point_attribute(element: CFTypeRef, name: &str) -> CGPoint {
    copy_attribute(element, name)
        .and_then(|value| {
            let mut point = CGPoint::new(0.0, 0.0);
            let ok = unsafe {
                AXValueGetValue(value.as_CFTypeRef(), AX_VALUE_CG_POINT, &mut point as *mut CGPoint as *mut c_void)
            };
            ok.then_some(point)
        })
        .unwrap_or(CGPoint::new(0.0, 0.0))
}

fn size_attribute(element: CFTypeRef, name: &str) -> CGSize {
    copy_attribute(element, name)
        .and_then(|value| {
            let mut size = CGSize::new(0.0, 0.0);
            let ok = unsafe {
                AXValueGetValue(value.as_CFTypeRef(), AX_VALUE_CG_SIZE, &mut size as *mut CGSize as *mut c_void)
            };
            ok.then_some(size)
        })
        .unwrap_or(CGSize::new(0.0, 0.0))
}

/// Traverse one element into a HostElement, capping depth and total nodes
/// like the simulated backend so a deep app cannot balloon the RPC response
/// (same budget rule: web-first — page content is visited before chrome).
///
/// ``in_web`` tracks whether this element already lives inside an
/// ``AXWebArea`` subtree; children inherit it, so every node below a page
/// element spends from the same web pool. The per-pool ``NodeBudget`` stops
/// descent in a pool once it is spent — an exhausted pool drops the
/// remaining siblings entirely (no empty leaves), bounding both the walk
/// time and the response size. Children are ordered web-first BEFORE
/// recursing, mirroring the orchestrator's summary ordering, so the budget
/// lands on the actionable page content first.
fn build_tree(
    element: CFTypeRef,
    depth: u8,
    max_depth: u8,
    in_web: bool,
    budget: &mut NodeBudget,
) -> HostElement {
    let role = string_attribute(element, "AXRole")
        // Drop the ubiquitous "AX" prefix so roles read "Button", "Window".
        .map(|r| r.trim_start_matches("AX").to_string())
        .unwrap_or_default();
    let node_is_web = in_web || role == "WebArea";
    // Many apps (Chrome's omnibox included) leave AXTitle empty and put the
    // human label in AXDescription; fall back so the element still has a
    // name a provider can reason about ("address bar", "search field").
    let title = string_attribute(element, "AXTitle")
        .or_else(|| string_attribute(element, "AXDescription"))
        .unwrap_or_default();
    // AXValue: the element's current text content (text fields, sliders).
    // Absent on most elements; text inputs report it so the orchestrator can
    // verify that typed/pasted text actually landed in the focused field.
    let value = string_attribute(element, "AXValue").unwrap_or_default();
    // AXFocused: does this element hold keyboard focus right now? A click on
    // a text field/button moves focus to it, so the *next* snapshot reports
    // it focused — a consent-free confirmation that the action landed (the
    // same AX API already feeding ADR-2 coordinates, no Screen Recording
    // needed). The two CFBoolean singletons are global, so ref equality is
    // the correct conversion.
    let focused = copy_attribute(element, "AXFocused")
        .and_then(|value| value.downcast::<CFBoolean>())
        .map(|flag| flag == CFBoolean::true_value())
        .unwrap_or(false);
    let position = point_attribute(element, "AXPosition");
    let size = size_attribute(element, "AXSize");

    let mut children = Vec::new();
    if depth < max_depth {
        if let Some(children_attr) = copy_attribute(element, "AXChildren") {
            // CFArray does not implement ConcreteCFType, so downcast via the
            // raw ref: wrap the array we already own a reference to.
            let array = unsafe {
                CFArray::<CFType>::wrap_under_get_rule(
                    children_attr.as_CFTypeRef() as CFArrayRef,
                )
            };
            // Web-first: collect role-tagged children so page content
            // (AXWebArea subtrees) is visited before chrome (tab strip,
            // omnibox, toolbar). The AXRole read here is cheap and bounds
            // the ordering decision to one attribute per direct child.
            let mut tagged: Vec<(String, CFTypeRef)> = Vec::new();
            for child in array.iter() {
                let role = string_attribute(child.as_CFTypeRef(), "AXRole").unwrap_or_default();
                tagged.push((role, child.as_CFTypeRef()));
            }
            tagged.sort_by_key(|(role, _)| role != "AXWebArea");
            for (child_role, child) in tagged {
                let child_is_web = node_is_web || child_role == "AXWebArea";
                // Budget gate BEFORE recursing: an exhausted pool drops the
                // rest of the siblings — never emit stubs to pad the payload.
                if !budget.spend(child_is_web) {
                    break;
                }
                children.push(build_tree(child, depth + 1, max_depth, node_is_web, budget));
            }
        }
    }

    HostElement {
        role,
        title,
        value,
        focused,
        x: position.x,
        y: position.y,
        width: size.width,
        height: size.height,
        children,
    }
}

/// Frontmost-app perception: the OBSERVE step's window/cursor half.
///
/// Reads the *system-wide* AX element (not per-app) to find the focused
/// application and its focused window, plus the current cursor position via a
/// probe CGEvent — the two non-pixel signals §5's OBSERVE step requires.
/// The returned pid feeds ``ax_snapshot``, so a caller that did not name an
/// app can discover it from the focused window.
///
/// The AX read is the ADR-2 *primary* source (it also yields the focused
/// window's title). When it fails — e.g. the frontmost app does not answer
/// the system-wide query (``kAXErrorCannotComplete``), or consent is missing
/// — we fall back to the window list, which names the frontmost window's
/// owner (pid + app name) *without* any Accessibility consent. Best-effort
/// perception must survive a flaky primary (Law 2/6.3): a run is never
/// aborted because the frontmost-app query hiccupped.
pub fn focused_window() -> Result<FocusedWindow, BackendError> {
    let (cursor_x, cursor_y) = cursor_position()?;
    // ADR-2 primary: the system-wide AX element names the frontmost app and
    // its focused window. Consent-gated; on any failure we fall through to
    // the consent-free window-list route below.
    let ax_error = if trusted() {
        match focused_application_via_ax(cursor_x, cursor_y) {
            Ok(focused) => return Ok(focused),
            Err(error) => error,
        }
    } else {
        BackendError(
            "Accessibility consent required for focused-window reads. Grant it \
             in System Settings > Privacy & Security > Accessibility, then \
             restart the driver."
                .to_string(),
        )
    };
    // Consent-free fallback: the frontmost *normal window's owner* from the
    // window list. Owner pid/name are available without any consent (only
    // window *titles* are redacted without Screen Recording), which is enough
    // to name the app and feed ``ax_snapshot``.
    if let Some((pid, app_name)) = frontmost_window_owner() {
        return Ok(FocusedWindow {
            pid,
            app_name,
            window_title: String::new(),
            cursor_x,
            cursor_y,
        });
    }
    Err(ax_error)
}

/// The AX primary of :func:`focused_window` (system-wide focused application).
fn focused_application_via_ax(cursor_x: f64, cursor_y: f64) -> Result<FocusedWindow, BackendError> {
    // Create Rule: the system-wide element reference is ours to release.
    let system = unsafe { CFType::wrap_under_create_rule(AXUIElementCreateSystemWide()) };
    let (app, app_error) = copy_attribute_coded(system.as_CFTypeRef(), "AXFocusedApplication");
    let Some(app) = app else {
        // Distinguish the one genuinely-consent code (kAXErrorAPIDisabled)
        // from the frontmost app simply not answering the query, so the
        // message points at the right fix instead of always blaming consent.
        let reason = match app_error {
            -25211 => "Accessibility consent revoked for this process \
                       (kAXErrorAPIDisabled); grant it in System Settings > \
                       Privacy & Security > Accessibility, then restart the \
                       driver"
                .to_string(),
            code if code != AX_ERROR_SUCCESS => format!(
                "AXFocusedApplication failed with code {code} (trusted=true); \
                 the frontmost app did not answer the system-wide AX query — \
                 the window-list fallback will name it instead"
            ),
            _ => "the system reported no frontmost application".to_string(),
        };
        return Err(BackendError(format!("focused-window read failed: {reason}")));
    };
    let mut pid: i32 = 0;
    let pid_error = unsafe { AXUIElementGetPid(app.as_CFTypeRef(), &mut pid) };
    if pid_error != AX_ERROR_SUCCESS {
        return Err(BackendError(format!(
            "AXUIElementGetPid failed (code {pid_error})"
        )));
    }
    // The app element's AXTitle is usually the app name; the focused window's
    // AXTitle is the document/page title. Both are best-effort strings.
    let app_name = string_attribute(app.as_CFTypeRef(), "AXTitle").unwrap_or_default();
    let window_title = copy_attribute(app.as_CFTypeRef(), "AXFocusedWindow")
        .and_then(|window| string_attribute(window.as_CFTypeRef(), "AXTitle"))
        .unwrap_or_default();
    Ok(FocusedWindow {
        pid,
        app_name,
        window_title,
        cursor_x,
        cursor_y,
    })
}

/// Frontmost normal-window owner via ``CGWindowListCopyWindowInfo``.
///
/// The window list is ordered front-to-back, so the first layer-0 window's
/// owner is the frontmost app. ``kCGWindowName`` (the title) is redacted
/// without Screen Recording consent, but the owner pid and name are always
/// available — exactly the fields OBSERVE needs to name the app and feed
/// ``ax_snapshot``.
fn frontmost_window_owner() -> Option<(i32, String)> {
    let windows = copy_window_info(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
        kCGNullWindowID,
    )?;
    // The kCGWindow* keys are framework-owned global CFStrings; get-rule is
    // correct and keeps them alive for the lookups below.
    let (layer_key, pid_key, name_key) = unsafe {
        (
            CFString::wrap_under_get_rule(kCGWindowLayer),
            CFString::wrap_under_get_rule(kCGWindowOwnerPID),
            CFString::wrap_under_get_rule(kCGWindowOwnerName),
        )
    };
    for window in windows.iter() {
        // The array's default element type is the raw CFTypeRef; the window
        // list documents each entry as a CFString-keyed dictionary, so wrap
        // the reference with that shape (get-rule: the array owns it).
        let window = unsafe {
            CFDictionary::<CFString, CFType>::wrap_under_get_rule(*window as CFDictionaryRef)
        };
        // Layer 0 = normal application windows; skip the menu bar, Dock, etc.
        let layer = window.find(&layer_key)?;
        if layer.downcast::<CFNumber>()?.to_i32()? != 0 {
            continue;
        }
        let pid = window.find(&pid_key)?;
        let pid = pid.downcast::<CFNumber>()?.to_i32()?;
        let name = window.find(&name_key)?;
        let name = name.downcast::<CFString>()?.to_string();
        return Some((pid, name));
    }
    None
}

/// Distinct owner names of on-screen normal windows (``CGWindowListCopyWindowInfo``).
///
/// Same source and filtering as :func:`frontmost_window_owner`, but collects
/// every layer-0 window's owner name (deduplicated, in front-to-back order of
/// first appearance). This is the driver's answer to "which apps is the user
/// actually running" — used for autonomous target-app inference — and needs no
/// consent: owner names (unlike window titles) are always available.
pub fn window_owner_names() -> Vec<String> {
    let windows = match copy_window_info(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
        kCGNullWindowID,
    ) {
        Some(windows) => windows,
        None => return Vec::new(),
    };
    let (layer_key, name_key) = unsafe {
        (
            CFString::wrap_under_get_rule(kCGWindowLayer),
            CFString::wrap_under_get_rule(kCGWindowOwnerName),
        )
    };
    let mut seen = std::collections::HashSet::new();
    let mut names = Vec::new();
    for window in windows.iter() {
        let window = unsafe {
            CFDictionary::<CFString, CFType>::wrap_under_get_rule(*window as CFDictionaryRef)
        };
        let Some(layer) = window.find(&layer_key) else {
            continue;
        };
        // Layer 0 = normal application windows; skip the menu bar, Dock, etc.
        if layer.downcast::<CFNumber>().and_then(|n| n.to_i32()) != Some(0) {
            continue;
        }
        let Some(name) = window.find(&name_key) else {
            continue;
        };
        let Some(name) = name.downcast::<CFString>() else {
            continue;
        };
        let name = name.to_string();
        if name.is_empty() || !seen.insert(name.clone()) {
            continue;
        }
        names.push(name);
    }
    names
}

/// Current cursor position in global logical points (a probe CGEvent).
///
/// ``CGEvent::new`` with no prior event yields the *current* location — the
/// standard macOS trick, and the event owns its CF ref so nothing leaks.
fn cursor_position() -> Result<(f64, f64), BackendError> {
    let source = CGEventSource::new(CGEventSourceStateID::HIDSystemState)
        .map_err(|()| BackendError("cannot create CGEventSource".to_string()))?;
    let event = CGEvent::new(source)
        .map_err(|()| BackendError("cannot create a probe CGEvent".to_string()))?;
    let location = event.location();
    Ok((location.x, location.y))
}

/// Snapshot the accessibility tree of the app with the given pid.
pub fn ax_snapshot(pid: u32, max_depth: u8, max_nodes: u32) -> Result<HostElement, BackendError> {
    // The same Accessibility consent that gates event posting also gates AX
    // reads; without it every attribute copy returns kAXErrorAPIDisabled.
    if !trusted() {
        return Err(BackendError(
            "Accessibility consent required for element grounding. Grant it in \
             System Settings > Privacy & Security > Accessibility, then restart \
             the driver."
                .to_string(),
        ));
    }
    let app_ref = unsafe { AXUIElementCreateApplication(pid as i32) };
    if app_ref.is_null() {
        return Err(BackendError(format!(
            "AXUIElementCreateApplication failed for pid {pid}"
        )));
    }
    // Create Rule: the returned ref is ours. Wrap it so it is released when
    // the traversal finishes — a raw ref here would leak one app element per
    // snapshot on a polling loop.
    let app = unsafe { CFType::wrap_under_create_rule(app_ref) };
    // The app root itself is always emitted (it does not spend the budget);
    // every descendant spends from the web-first two-pool budget, so the
    // walk and the response stay bounded on heavy pages (YouTube-size trees
    // are tens of thousands of nodes; the orchestrator needs ~dozens).
    let mut budget = NodeBudget::new(max_nodes);
    let root = build_tree(app.as_CFTypeRef(), 0, max_depth, false, &mut budget);
    Ok(root)
}

/// May this process read the accessibility tree (consent check)?
fn trusted() -> bool {
    unsafe { AXIsProcessTrusted() }
}
