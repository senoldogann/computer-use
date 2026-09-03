//! Actuation driver binary — the imperative shell around the pure math.
//!
//! This process owns the physical OS input stream (mouse, keyboard, capture).
//! It is intentionally a *separate process* from the Python orchestrator
//! (ADR-1): if a CGEvent tap hangs the OS-level layer, only this binary is at
//! risk, and the orchestrator restarts it. Communication is JSON-RPC over a
//! Unix socket.
//!
//! By default the *simulated* backend runs: it validates and plans every action
//! but never touches a real mouse/keyboard, so development and CI are safe.
//! Pass ``--real`` on macOS (with Accessibility consent) to drive the actual
//! host via Quartz CGEvent (Law 1).

use std::env;
use std::fs;
use std::io::{self, BufRead, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::os::unix::fs::PermissionsExt;
use std::sync::Arc;

use base64::Engine as _;

use actuation_driver::backend::{Backend, BackendError, Button, Modifier, SimulatedBackend};
use actuation_driver::bezier::{human_move_duration, plan_trajectory, point};
use actuation_driver::protocol::{Request, Response};

fn main() {
    let mut args = env::args().skip(1);
    let socket_path = args.next().unwrap_or_else(|| "/tmp/actuation-driver.sock".to_string());
    let real = args.any(|a| a == "--real");

    // Construct the backend. On failure (e.g. no Accessibility consent) we log
    // the precise reason and exit non-zero so the orchestrator sees the driver
    // never came up — better than silently accepting actions that do nothing.
    let backend: Arc<dyn Backend> = match make_backend(real) {
        Ok(backend) => Arc::from(backend),
        Err(e) => {
            eprintln!("[driver] fatal: {e}");
            std::process::exit(1);
        }
    };
    eprintln!(
        "[driver] actuator: {} (socket {socket_path})",
        if backend.is_real() { "quartz/real" } else { "simulated" }
    );
    // Law 5.2: only the real backend may touch the host's event system; the
    // kill-hotkey tap is a host listener, so it belongs to the real driver.
    if backend.is_real() {
        #[cfg(target_os = "macos")]
        actuation_driver::hotkey::spawn_listener();
    }

    // Bind with a fixed socket; a stale socket file from a crashed run is
    // removed first so the bind cannot fail on retry. A bind failure (e.g. a
    // socket path longer than the OS limit) exits with a clean message instead
    // of a Rust panic, so the orchestrator sees *why* the driver never came up
    // (ADR-1: the orchestrator restarts us, but it must know the reason).
    let _ = fs::remove_file(&socket_path);
    let listener = match UnixListener::bind(&socket_path) {
        Ok(listener) => listener,
        Err(e) => {
            eprintln!("[driver] fatal: cannot bind socket {socket_path}: {e}");
            std::process::exit(1);
        }
    };

    if let Err(error) = fs::set_permissions(&socket_path, fs::Permissions::from_mode(0o600)) {
        eprintln!("[driver] fatal: cannot secure socket {socket_path}: {error}");
        std::process::exit(1);
    }
    eprintln!("[driver] listening on {socket_path}");
    if backend.is_real() {
        #[cfg(target_os = "macos")]
        {
            // AppKit owns the main thread (menu-bar icon + cursor halo), so
            // the socket accept loop moves to a worker thread; the indicator
            // run loop keeps the process alive exactly as long as the driver
            // should serve. The worker is intentionally detached: the
            // orchestrator terminates this process when the run ends.
            let worker_backend = Arc::clone(&backend);
            std::thread::spawn(move || accept_loop(listener, worker_backend));
            // When the menu-bar launcher spare the run it owns the status icon,
            // so this driver stays halo-only (one icon total, Law 5.2 clarity).
            if std::env::var_os("COMPUTERUSE_NO_STATUS").is_some() {
                actuation_driver::indicator::run_halo();
            } else {
                actuation_driver::indicator::run();
            }
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = listener;
        }
    } else {
        accept_loop(listener, backend);
    }
}

/// Serve the Unix socket forever: one thread per connection (F4), so a slow
/// or idle client never blocks the accept loop from serving the orchestrator.
fn accept_loop(listener: UnixListener, backend: Arc<dyn Backend>) {
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let backend = connection_backend(&backend);
                std::thread::spawn(move || {
                    if let Err(e) = handle_conn(stream, backend.as_ref()) {
                        eprintln!("[driver] connection error: {e}");
                    }
                });
            }
            Err(e) => eprintln!("[driver] accept error: {e}"),
        }
    }
}

/// The backend one connection should serve from.
///
/// The real backend is *shared*: there is one host, one event source, and one
/// set of consents. The simulated backend is a fixture whose state (focus,
/// field values, frontmost app) models a single client's session — sharing it
/// across connections would let one caller's clicks silently rewrite what
/// another caller observes, which is precisely the non-determinism a fixture
/// exists to eliminate.
fn connection_backend(shared: &Arc<dyn Backend>) -> Arc<dyn Backend> {
    if shared.is_real() {
        Arc::clone(shared)
    } else {
        Arc::new(SimulatedBackend::default())
    }
}

/// Build the active backend based on the `--real` flag (macOS only).
fn make_backend(real: bool) -> Result<Box<dyn Backend>, BackendError> {
    if !real {
        return Ok(Box::new(SimulatedBackend::default()));
    }
    #[cfg(target_os = "macos")]
    {
        Ok(Box::new(actuation_driver::QuartzBackend::new()?))
    }
    #[cfg(not(target_os = "macos"))]
    {
        Err(BackendError(
            "the real actuator is only available on macOS; use the simulated backend".to_string(),
        ))
    }
}

/// Maximum request line size before truncating (16 MiB defensive ceiling).
const MAX_REQUEST_BYTES: u64 = 16 * 1024 * 1024;

fn handle_conn(stream: UnixStream, backend: &dyn Backend) -> io::Result<()> {
    stream.set_nonblocking(false)?;
    let mut reader = io::BufReader::new(stream.try_clone()?);
    let mut writer = stream;
    let mut line = String::new();
    loop {
        line.clear();
        let n = (&mut reader).take(MAX_REQUEST_BYTES).read_line(&mut line)?;
        if n == 0 {
            break;
        }
        let response = dispatch(&line, backend);
        // Manual wire serialization: serde's char-by-char validation costs
        // ~1s for a 40MB screenshot payload; to_wire_string special-cases it
        // (memcpy-speed, byte-identical — pinned by a protocol unit test).
        let mut payload = response.to_wire_string();
        payload.push('\n');
        writer.write_all(payload.as_bytes())?;
        writer.flush()?;
    }
    Ok(())
}

/// Parse and execute a single request line. Malformed input yields an `Error`
/// response rather than killing the process (Law 2 resilience).
fn dispatch(line: &str, backend: &dyn Backend) -> Response {
    let req: Request = match serde_json::from_str(line.trim()) {
        Ok(r) => r,
        Err(e) => {
            return Response::Error {
                message: format!("malformed request: {e}"),
            };
        }
    };
    execute(req, backend)
}

/// Route to the physical actuation layer. Pure trajectory planning happens
/// here; the backend owns the actual OS side effects.
fn execute(req: Request, backend: &dyn Backend) -> Response {
    // Helper: fold a backend failure into a structured Error response.
    let outcome = match req {
        Request::Ping => return Response::Pong,
        Request::Health => {
            #[cfg(target_os = "macos")]
            let trusted = if backend.is_real() {
                actuation_driver::ax::trusted()
            } else {
                true
            };
            #[cfg(not(target_os = "macos"))]
            let trusted = true;

            return Response::Health {
                backend: if backend.is_real() {
                    "quartz/real".to_string()
                } else {
                    "simulated".to_string()
                },
                trusted,
            };
        }
        Request::HotkeyState => {
            #[cfg(target_os = "macos")]
            {
                return Response::HotkeyState {
                    tripped: actuation_driver::hotkey::tripped(),
                };
            }
            #[cfg(not(target_os = "macos"))]
            {
                return Response::HotkeyState { tripped: false };
            }
        }
        Request::FocusedWindow => {
            return match backend.focused_window() {
                Ok(focused) => Response::FocusedWindow {
                    pid: focused.pid,
                    app_name: focused.app_name,
                    bundle_id: focused.bundle_id,
                    window_title: focused.window_title,
                    cursor_x: focused.cursor_x,
                    cursor_y: focused.cursor_y,
                },
                Err(BackendError(message)) => Response::Error { message },
            };
        }
        Request::ListApps => {
            return match backend.list_apps() {
                Ok(apps) => Response::ListApps { apps },
                Err(BackendError(message)) => Response::Error { message },
            };
        }
        Request::AxSnapshot(params) => {
            return match backend.ax_snapshot(params.pid, params.max_depth, params.max_nodes) {
                Ok(root) => Response::AxSnapshot { root },
                Err(BackendError(message)) => Response::Error { message },
            };
        }
        Request::ActivateApp(params) => {
            return match backend.activate_app(&params.app) {
                Ok(()) => Response::Ack,
                Err(BackendError(message)) => Response::Error { message },
            };
        }
        Request::Screenshot(params) => {
            return match backend.capture(params.display_id, params.pid) {
                Ok(frame) => Response::Screenshot {
                        display_id: frame.display_id,
                        format: "bgra8",
                        width: frame.width,
                        height: frame.height,
                        scale: frame.scale,
                        origin_x: frame.origin_x,
                        origin_y: frame.origin_y,
                        data_base64: base64::engine::general_purpose::STANDARD
                            .encode(&frame.bgra),
                },
                Err(BackendError(message)) => Response::Error { message },
            };
        }
        Request::MouseMove(params) => {
            let from = match backend.current_position() {
                Ok(position) => position,
                Err(BackendError(message)) => return Response::Error { message },
            };
            let to = point(params.x, params.y);
            // Distance-adaptive duration: a fixed 180ms default would make a
            // cross-screen sweep read as a teleport (Law 1 human cadence).
            let duration = human_move_duration(from, to, params.duration_ms);
            let trajectory = plan_trajectory(from, to, duration, 16);
            backend.move_along(&trajectory)
        }
        Request::MouseClick(params) => {
            let button = match params.button.as_str() {
                "left" => Button::Left,
                "right" => Button::Right,
                "middle" => Button::Middle,
                other => {
                    return Response::Error {
                        message: format!("unsupported mouse button {other:?}"),
                    };
                }
            };
            backend.click(point(params.x, params.y), button, params.click_count)
        }
        Request::AppPid(params) => {
            return match backend.app_pid(&params.app) {
                Ok(pid) => Response::AppPid { pid },
                Err(error) => Response::Error { message: error.0 },
            };
        }
        Request::AxSetValue(params) => {
            return match backend.ax_set_value(
                params.pid,
                point(params.x, params.y),
                &params.text,
            ) {
                Ok(wrote) => Response::AxSetValue { wrote },
                Err(error) => Response::Error { message: error.0 },
            };
        }
        Request::IdleSeconds => {
            return match backend.idle_seconds() {
                Ok(seconds) => Response::IdleSeconds { seconds },
                Err(error) => Response::Error { message: error.0 },
            };
        }
        Request::AxPress(params) => {
            return match backend.ax_press(params.pid, point(params.x, params.y)) {
                Ok(pressed) => Response::AxPress { pressed },
                Err(error) => Response::Error { message: error.0 },
            };
        }
        Request::MouseDrag(params) => {
            backend.drag(
            point(params.start_x, params.start_y),
            point(params.end_x, params.end_y),
            params.duration_ms,
        )
        }
        Request::MouseScroll(params) => {
            backend.scroll(params.dx, params.dy)
        }
        Request::PressHotkey(params) => {
            let mut modifiers: Vec<Modifier> = Vec::with_capacity(params.modifiers.len());
            for modifier in &params.modifiers {
                let parsed = match modifier.as_str() {
                    "command" => Modifier::Command,
                    "shift" => Modifier::Shift,
                    "alt" => Modifier::Alt,
                    "control" => Modifier::Control,
                    other => {
                        return Response::Error {
                            message: format!("unsupported modifier {other:?}"),
                        };
                    }
                };
                modifiers.push(parsed);
            }
            backend.hotkey(&modifiers, &params.key)
        }
        Request::TypeText(params) => {
            backend.type_text(&params.text, params.wpm)
        },
        Request::ClipboardPaste(params) => {
            backend.clipboard_paste(&params.text)
        },
    };

    match outcome {
        Ok(()) => Response::Ack,
        Err(BackendError(message)) => Response::Error { message },
    }
}

