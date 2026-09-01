//! Entry point for the menu-bar launcher app (``actuation-menu``).
//!
//! A tiny status-bar app: click the sea-blue icon, type a goal in the
//! Liquid-Glass chat panel, and it runs the agent CLI as a subprocess while
//! streaming output back. The whole app is macOS/AppKit; on other targets the
//! binary still compiles (so ``cargo build`` in CI stays green) but reports
//! that it is unsupported and exits.

fn main() {
    #[cfg(target_os = "macos")]
    {
        actuation_driver::menu::run()
    }
    #[cfg(not(target_os = "macos"))]
    {
        eprintln!("[actuation-menu] only supported on macOS");
        std::process::exit(1);
    }
}