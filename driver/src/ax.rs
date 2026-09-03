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
use core_graphics::geometry::{CGPoint, CGRect, CGSize};
use core_graphics::window::{
    copy_window_info, kCGNullWindowID, kCGWindowBounds, kCGWindowLayer,
    kCGWindowListExcludeDesktopElements, kCGWindowListOptionOnScreenOnly, kCGWindowNumber,
    kCGWindowOwnerName, kCGWindowOwnerPID,
};

use objc2_app_kit::NSRunningApplication;

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
    fn AXUIElementSetAttributeValue(
        element: CFTypeRef,
        attribute: CFTypeRef,
        value: CFTypeRef,
    ) -> i32;
    fn AXUIElementCopyElementAtPosition(
        application: CFTypeRef,
        x: f32,
        y: f32,
        element: *mut CFTypeRef,
    ) -> i32;
    fn AXUIElementPerformAction(element: CFTypeRef, action: CFTypeRef) -> i32;
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
        // Create/Copy rule: the copy from AXUIElementCopyAttributeValue is
        // owned (+1 retain count), so wrap under create-rule without bumping.
        (Some(unsafe { CFType::wrap_under_create_rule(value) }), error)
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
            // Three tiers, not two. Web content first (the page is what the
            // agent acts on), then ordinary chrome, and the menu bar dead
            // last: an app's menu bar is a few hundred AXMenuItem nodes that
            // are reachable by keyboard anyway, and letting it go first meant
            // it swallowed the entire node budget before the window subtree
            // was ever visited.
            tagged.sort_by_key(|(role, _)| match role.as_str() {
                "AXWebArea" => 0u8,
                "AXMenuBar" => 2,
                _ => 1,
            });
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

    // A web link's own AXTitle is empty: browsers put the label in a
    // descendant StaticText's AXValue. Without composing it, every one of a
    // page's links summarises as "(untitled)" and the model has no way to
    // name what it wants to click — measured on a news front page: 438 Link
    // nodes, 0 with a title. It then guesses coordinates off a screenshot
    // where body text is ~3px tall, and misses. Composing the name here (once,
    // bottom-up over children already built) is what turns the accessibility
    // tree from a list of anonymous rectangles into something a model can aim
    // with.
    let title = if title.is_empty() && value.is_empty() && names_from_descendants(&role) {
        descendant_text(&children, DERIVED_NAME_MAX_CHARS)
    } else {
        title
    };

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
            bundle_id: bundle_id_for_pid(pid),
            app_name,
            window_title: String::new(),
            cursor_x,
            cursor_y,
        });
    }
    Err(ax_error)
}

/// One application's focused window, whether or not that application is in
/// front.
///
/// ``focused_window`` answers "what is the user looking at", which is the
/// wrong question in background mode: the target is deliberately behind
/// something else, so the system-wide reading describes a window the agent is
/// not acting on and does not change when the agent's actions land. Measured
/// on a real run, that froze the agent's only sense of place for twenty-five
/// consecutive steps and it looped.
///
/// The cursor still comes from the system, because there is only one.
pub fn app_window(pid: u32) -> Result<FocusedWindow, BackendError> {
    let (cursor_x, cursor_y) = cursor_position()?;
    if !trusted() {
        return Err(BackendError(
            "Accessibility consent required to read another application's \
             window. Grant it in System Settings > Privacy & Security > \
             Accessibility, then restart the driver."
                .to_string(),
        ));
    }
    // Create Rule: the application element reference is ours to release.
    let app_ref = unsafe { AXUIElementCreateApplication(pid as i32) };
    if app_ref.is_null() {
        return Err(BackendError(format!(
            "AXUIElementCreateApplication failed for pid {pid}"
        )));
    }
    let app = unsafe { CFType::wrap_under_create_rule(app_ref) };
    // An app that is not frontmost has no *focused* window, so fall back to
    // its main one: that is the window a background agent is acting on.
    let window = copy_attribute(app.as_CFTypeRef(), "AXFocusedWindow")
        .or_else(|| copy_attribute(app.as_CFTypeRef(), "AXMainWindow"));
    let window_title = window
        .and_then(|window| string_attribute(window.as_CFTypeRef(), "AXTitle"))
        .unwrap_or_default();
    Ok(FocusedWindow {
        pid: pid as i32,
        bundle_id: bundle_id_for_pid(pid as i32),
        app_name: string_attribute(app.as_CFTypeRef(), "AXTitle").unwrap_or_default(),
        window_title,
        cursor_x,
        cursor_y,
    })
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
        bundle_id: bundle_id_for_pid(pid),
        app_name,
        window_title,
        cursor_x,
        cursor_y,
    })
}

/// The frontmost normal window belonging to a process: its id and global
/// logical bounds.
///
/// Lets the sensor photograph one application's window instead of the display.
/// Without it, an agent working in a background app is shown a picture of
/// whatever the user has in front — it reasons about the wrong window while
/// acting on the right one.
///
/// The window list is returned front-to-back, so the first layer-0 window
/// owned by the pid is the one the app itself considers frontmost.
pub fn window_for_pid(pid: i32) -> Option<(u32, CGRect)> {
    let windows = copy_window_info(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
        kCGNullWindowID,
    )?;
    let (layer_key, pid_key, number_key, bounds_key) = unsafe {
        (
            CFString::wrap_under_get_rule(kCGWindowLayer),
            CFString::wrap_under_get_rule(kCGWindowOwnerPID),
            CFString::wrap_under_get_rule(kCGWindowNumber),
            CFString::wrap_under_get_rule(kCGWindowBounds),
        )
    };
    for window in windows.iter() {
        let window = unsafe {
            CFDictionary::<CFString, CFType>::wrap_under_get_rule(*window as CFDictionaryRef)
        };
        // Layer 0 is an ordinary application window; menus and the Dock are not.
        if window.find(&layer_key)?.downcast::<CFNumber>()?.to_i32()? != 0 {
            continue;
        }
        if window.find(&pid_key)?.downcast::<CFNumber>()?.to_i32()? != pid {
            continue;
        }
        let number = window.find(&number_key)?.downcast::<CFNumber>()?.to_i32()? as u32;
        let bounds_dict = window.find(&bounds_key)?;
        let bounds = unsafe {
            CGRect::from_dict_representation(&CFDictionary::wrap_under_get_rule(
                bounds_dict.as_CFTypeRef() as CFDictionaryRef,
            ))
        }?;
        return Some((number, bounds));
    }
    None
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
    enable_web_accessibility(app.as_CFTypeRef());
    // The app root itself is always emitted (it does not spend the budget);
    // every descendant spends from the web-first two-pool budget, so the
    // walk and the response stay bounded on heavy pages (YouTube-size trees
    // are tens of thousands of nodes; the orchestrator needs ~dozens).
    let mut budget = NodeBudget::new(max_nodes);
    let root = build_tree(app.as_CFTypeRef(), 0, max_depth, false, &mut budget);
    Ok(root)
}

/// Cap on a derived accessible name, in characters. Long enough to identify a
/// headline or a paragraph link, short enough that a page of them still fits
/// the model's context.
const DERIVED_NAME_MAX_CHARS: usize = 120;

/// Roles whose label is worth reconstructing from descendant text.
///
/// Restricted to elements an agent actually aims at. Deriving names for every
/// unnamed container instead would attach the whole page's text to each of the
/// dozen nested `Group`s wrapping it — pure noise that crowds out the real
/// targets.
fn names_from_descendants(role: &str) -> bool {
    matches!(
        role,
        "Link" | "Button" | "Cell" | "Heading" | "CheckBox" | "RadioButton" | "Tab" | "MenuItem"
    )
}

/// Concatenate the visible text under an element, breadth-first, up to a cap.
///
/// Breadth-first so a link's own text wins over text nested deeper inside it,
/// and bounded so one pathological subtree cannot blow up the response.
fn descendant_text(children: &[HostElement], max_chars: usize) -> String {
    let mut parts: Vec<String> = Vec::new();
    let mut total = 0usize;
    let mut queue: std::collections::VecDeque<&HostElement> = children.iter().collect();
    while let Some(node) = queue.pop_front() {
        if total >= max_chars {
            break;
        }
        let text = if !node.value.is_empty() {
            node.value.trim()
        } else {
            node.title.trim()
        };
        if !text.is_empty() {
            total += text.len();
            parts.push(text.to_string());
        }
        for child in &node.children {
            queue.push_back(child);
        }
    }
    let joined = parts.join(" ");
    let trimmed = joined.trim();
    match trimmed.char_indices().nth(max_chars) {
        Some((cut, _)) => trimmed[..cut].to_string(),
        None => trimmed.to_string(),
    }
}

/// Ask the element under a global point to activate itself.
///
/// The *quiet* actuation path. A synthetic click goes into the system event
/// stream, so it lands on whatever is frontmost and drags the user's real
/// cursor with it; this addresses one element directly, which neither moves
/// the cursor nor requires the target to be in front. That is the difference
/// between an agent that owns the machine while it works and one that can work
/// beside its user.
///
/// Returns whether an element accepted the press. `false` is an ordinary
/// answer, not an error: plenty of elements expose no press action at all, and
/// the caller falls back to a synthetic click. A `true` is *not* proof the
/// press did anything — a Chromium web view is documented to answer
/// `kAXErrorSuccess` and leave the page untouched — which is exactly why the
/// orchestrator verifies every action against the screen afterwards rather
/// than trusting an ACK.
pub fn press_element_at(pid: u32, x: f64, y: f64) -> Result<bool, BackendError> {
    if !trusted() {
        return Err(BackendError(
            "Accessibility consent required to press an element. Grant it in \
             System Settings > Privacy & Security > Accessibility, then restart \
             the driver."
                .to_string(),
        ));
    }
    // Hit-test inside the *target application*, never system-wide. The
    // system-wide element resolves by z-order, so it returns whatever window
    // happens to be on top — which defeats the entire point. Measured: with
    // Chrome covering Calculator, three system-wide presses on Calculator's
    // keypad all answered success, Chrome's own elements absorbed them, and
    // the calculator display never moved. Asking the application resolves
    // within that app whether or not it is in front.
    let app = unsafe { AXUIElementCreateApplication(pid as i32) };
    if app.is_null() {
        return Err(BackendError(format!(
            "AXUIElementCreateApplication failed for pid {pid}"
        )));
    }
    // Create Rule: both the application element and the hit-test result are
    // ours; wrapping them releases each when this returns.
    let app = unsafe { CFType::wrap_under_create_rule(app) };
    let mut element: CFTypeRef = std::ptr::null();
    let hit = unsafe {
        AXUIElementCopyElementAtPosition(app.as_CFTypeRef(), x as f32, y as f32, &mut element)
    };
    if hit != AX_ERROR_SUCCESS || element.is_null() {
        return Ok(false);
    }
    let element = unsafe { CFType::wrap_under_create_rule(element) };
    let action = CFString::from_static_string("AXPress");
    let performed = unsafe {
        AXUIElementPerformAction(element.as_CFTypeRef(), action.as_concrete_TypeRef() as CFTypeRef)
    };
    Ok(performed == AX_ERROR_SUCCESS)
}

/// Put text into the element at a point, without focus and without the cursor.
///
/// The companion to :func:`press_element_at`, and the reason background mode is
/// more than a click-only mode: typing goes through the global event stream, so
/// it lands wherever the user is looking. Setting the value on the element
/// itself does not.
///
/// The element is focused first — inside its own application, which does not
/// bring that application forward. Many text fields refuse a value while
/// unfocused, and the ones that accept it often ignore the change until they
/// are; asking for both is what makes the write stick.
///
/// Returns whether the value was accepted. `false` is an ordinary answer: not
/// every element is writable, and the caller falls back to synthetic typing.
/// `true` is not proof the app *reacted* — a field can hold new text and never
/// fire the change its page listens for — which is why the orchestrator still
/// verifies against the screen.
pub fn set_element_value(pid: u32, x: f64, y: f64, text: &str) -> Result<bool, BackendError> {
    if !trusted() {
        return Err(BackendError(
            "Accessibility consent required to write into an element. Grant it \
             in System Settings > Privacy & Security > Accessibility, then \
             restart the driver."
                .to_string(),
        ));
    }
    let app = unsafe { AXUIElementCreateApplication(pid as i32) };
    if app.is_null() {
        return Err(BackendError(format!(
            "AXUIElementCreateApplication failed for pid {pid}"
        )));
    }
    let app = unsafe { CFType::wrap_under_create_rule(app) };
    let mut element: CFTypeRef = std::ptr::null();
    let hit = unsafe {
        AXUIElementCopyElementAtPosition(app.as_CFTypeRef(), x as f32, y as f32, &mut element)
    };
    if hit != AX_ERROR_SUCCESS || element.is_null() {
        return Ok(false);
    }
    let element = unsafe { CFType::wrap_under_create_rule(element) };
    // Focus within the application first. This moves the app's own keyboard
    // focus; it does not raise the window or disturb the user's foreground.
    let focused_key = CFString::from_static_string("AXFocused");
    unsafe {
        AXUIElementSetAttributeValue(
            element.as_CFTypeRef(),
            focused_key.as_concrete_TypeRef() as CFTypeRef,
            CFBoolean::true_value().as_CFTypeRef(),
        );
    }
    let value_key = CFString::from_static_string("AXValue");
    let value = CFString::new(text);
    let wrote = unsafe {
        AXUIElementSetAttributeValue(
            element.as_CFTypeRef(),
            value_key.as_concrete_TypeRef() as CFTypeRef,
            value.as_concrete_TypeRef() as CFTypeRef,
        )
    };
    Ok(wrote == AX_ERROR_SUCCESS)
}

/// The `CFBundleIdentifier` of a running process, or "" when it has none.
///
/// The app's locale-independent identity. `app_name` is whatever the system
/// shows the user, which is translated: on a Turkish desktop Calculator is
/// "Hesap Makinesi", so an agent told to work in "Calculator" saw a different
/// app in front of it and refused to act, while activating "Hesap Makinesi"
/// failed too because no bundle on disk has that name. Both identities are now
/// carried, and the bundle id is the one that settles the question.
pub fn bundle_id_for_pid(pid: i32) -> String {
    NSRunningApplication::runningApplicationWithProcessIdentifier(pid)
        .and_then(|app| app.bundleIdentifier())
        .map(|id| id.to_string())
        .unwrap_or_default()
}

/// Ask an Electron-shaped app to build its web-content accessibility tree.
///
/// Electron apps (VS Code, Slack, Discord) keep the document tree switched off
/// until a client sets one of these flags; without it their AX walk returns an
/// empty shell. Chromium proper does *not* need this — it exposes `AXWebArea`
/// unconditionally, and answers both attributes with
/// `kAXErrorAttributeUnsupported` / `kAXErrorNotImplemented`. Native apps do
/// the same. Those refusals are the normal case and are deliberately ignored:
/// this is a best-effort hint, never a precondition for the snapshot.
fn enable_web_accessibility(app: CFTypeRef) {
    for attribute in ["AXEnhancedUserInterface", "AXManualAccessibility"] {
        let key = CFString::new(attribute);
        let enabled = CFBoolean::true_value();
        unsafe {
            AXUIElementSetAttributeValue(
                app,
                key.as_concrete_TypeRef() as CFTypeRef,
                enabled.as_CFTypeRef(),
            );
        }
    }
}

/// May this process read the accessibility tree (consent check)?
pub fn trusted() -> bool {
    unsafe { AXIsProcessTrusted() }
}
