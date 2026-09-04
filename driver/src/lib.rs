//! Pure computation for the actuation driver.
//!
//! Per Law 6, all deterministic math (Bezier sampling, easing) lives here as
//! pure functions with concrete types. The OS-facing I/O (mouse events, socket
//! serving) stays behind the shell in `main.rs`/`events`.

pub mod backend;
pub mod bezier;
pub mod protocol;

#[cfg(target_os = "macos")]
pub mod ax;
#[cfg(target_os = "macos")]
pub mod hotkey;
#[cfg(target_os = "macos")]
pub mod indicator;
#[cfg(target_os = "macos")]
pub mod menu;
#[cfg(target_os = "macos")]
pub mod quartz;
#[cfg(target_os = "macos")]
pub mod vision;

/// Re-export the backend so callers pick a live backend uniformly.
pub use backend::SimulatedBackend;
#[cfg(target_os = "macos")]
pub use quartz::QuartzBackend;