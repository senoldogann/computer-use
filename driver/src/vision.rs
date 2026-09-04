//! Text recognition via Vision.framework (ADR-2's OCR fallback).
//!
//! ADR-2 makes the accessibility tree the primary source of grounding and
//! pixels the verifier, and names OCR as the fallback "for apps with no AX
//! tree". That fallback was described for a long time and never written, which
//! left a hard hole rather than a soft one: marks are derived from AX elements
//! alone, so in a window that exposes none the model has no indexed target at
//! all and can only guess coordinates off a downscaled screenshot. Games,
//! virtual machines, remote desktops, a drawing canvas, and Electron apps with
//! poor accessibility are all in that set.
//!
//! This module is the narrow bridge to `VNRecognizeTextRequest`, and nothing
//! else. The coordinate conversion — the genuinely dangerous part — lives in
//! `backend::vision_box_to_points` as a pure, tested function, because an
//! axis flip that compiles cleanly and silently sends every click to the wrong
//! place is exactly the failure `scroll_wheels` already documents.
//!
//! Three things about the bridge are worth knowing:
//!
//! * **Two `CGImage` types.** `core-graphics 0.25` wraps it with
//!   `foreign_type!`, `objc2-core-graphics 0.3` with `cf_type!`. They are the
//!   same CoreFoundation object behind different Rust wrappers, so crossing
//!   between them is a pointer cast.
//! * **An autorelease pool is required.** `topCandidates`/`string` return
//!   autoreleased temporaries, and this runs on a connection thread with no
//!   run loop to drain them.
//! * **Vision does not need the main thread**, which matters because in real
//!   mode AppKit owns it and the socket accept loop is a detached worker.
//!
//! This file is macOS-only (gated in `lib.rs`).

#![cfg(target_os = "macos")]

use foreign_types::ForeignType;
use objc2::rc::{autoreleasepool, Retained};
use objc2::AnyThread;
use objc2_foundation::{NSArray, NSDictionary};
use objc2_vision::{
    VNImageBasedRequest, VNImageRequestHandler, VNRecognizeTextRequest, VNRequest,
    VNRequestTextRecognitionLevel,
};

use crate::backend::{vision_box_to_points, BackendError, CaptureGeometry, NormalizedBox, TextLine};

/// Candidates to ask Vision for per observation. One: the alternatives are
/// the same line spelled less confidently, and a mark list wants the reading
/// the model would actually see on screen.
const TOP_CANDIDATES: usize = 1;

/// Recognise the text in a captured image, in global logical points.
///
/// `frame` comes from the same capture the screenshot path uses, so an OCR
/// rectangle and a screenshot pixel describe the same place by construction
/// rather than by two derivations that happen to agree.
pub fn recognize(
    image: &core_graphics::image::CGImage,
    frame: CaptureGeometry,
    min_confidence: f32,
    max_lines: u32,
) -> Result<Vec<TextLine>, BackendError> {
    autoreleasepool(|_pool| {
        // SAFETY: `core-graphics`'s CGImage and `objc2-core-graphics`'s are two
        // Rust wrappers over the same CoreFoundation object; the pointer is
        // valid for as long as `image` is borrowed here, and Vision only reads
        // it during the synchronous `performRequests` below.
        let cg = unsafe { &*(image.as_ptr() as *const objc2_core_graphics::CGImage) };

        let request = VNRecognizeTextRequest::new();
        {
            // Accurate over Fast: this runs when the accessibility tree gave
            // us nothing, so a missed control has no second source to fall
            // back on. The cost is a few hundred milliseconds on one frame,
            // against an LLM turn measured in seconds.
            request.setRecognitionLevel(VNRequestTextRecognitionLevel::Accurate);
            // Language correction rewrites what it reads toward dictionary
            // words. A UI label is not prose — "Sil", "Cmd+W", a filename —
            // and a corrected reading is a control the agent then cannot find.
            request.setUsesLanguageCorrection(false);
        }

        let handler = unsafe {
            VNImageRequestHandler::initWithCGImage_options(
                VNImageRequestHandler::alloc(),
                cg,
                &NSDictionary::new(),
            )
        };
        // The handler takes an array of the base request type. The class
        // chain is VNRecognizeTextRequest -> VNImageBasedRequest -> VNRequest,
        // so widening takes two hops, not one.
        let image_based: Retained<VNImageBasedRequest> = Retained::into_super(request.clone());
        let base: Retained<VNRequest> = Retained::into_super(image_based);
        let requests = NSArray::from_retained_slice(&[base]);
        handler
            .performRequests_error(&requests)
            .map_err(|err| BackendError(format!("text recognition failed: {err}")))?;

        let Some(results) = request.results() else {
            return Ok(Vec::new());
        };

        let mut lines: Vec<TextLine> = Vec::new();
        for observation in results.iter() {
            if lines.len() as u32 >= max_lines {
                break;
            }
            let candidates = observation.topCandidates(TOP_CANDIDATES);
            let Some(best) = candidates.iter().next() else {
                continue;
            };
            let confidence = best.confidence();
            if confidence < min_confidence {
                continue;
            }
            let text = best.string().to_string();
            if text.trim().is_empty() {
                continue;
            }
            // SAFETY: reading the observation's own normalised rect; no
            // preconditions beyond the observation being alive, which it is.
            let bounds = unsafe { observation.boundingBox() };
            let (x, y, width, height) = vision_box_to_points(
                NormalizedBox {
                    x: bounds.origin.x,
                    y: bounds.origin.y,
                    width: bounds.size.width,
                    height: bounds.size.height,
                },
                frame,
            );
            lines.push(TextLine {
                text,
                confidence,
                x,
                y,
                width,
                height,
            });
        }
        Ok(lines)
    })
}
