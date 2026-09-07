import AppKit
import Foundation

/// Bridge connecting the native desktop app to the machine sensors and the
/// actuation driver's Unix domain socket.
///
/// Dual responsibilities:
/// 1. Sensor ticking: 30 Hz AppKit polls of cursor position, active window,
///    and accessibility elements under cursor. Runs locally and zero-cost.
/// 2. Socket telemetry: non-blocking probes to `/tmp/actuation-driver.sock`
///    (or a fallback `/tmp/cu-desktop-driver.sock` spawned by this app) to
///    reflect driver state, last actions, and health.
///
/// Designed never to crash on driver drops or missing permissions: every
/// sensor reading is optional and falls back to safe placeholders.
@MainActor
final class AgentBridge: ObservableObject {
    private weak var state: AppState?
    private var sensorTimer: Timer?
    private var socketTimer: Timer?
    private var spawnedDriver: Process?

    @Published private(set) var sensorTicks: UInt64 = 0
    @Published private(set) var driverBackend: String = ""
    @Published private(set) var bridgeLine = "local sensors"

    private let primarySocketPath = "/tmp/actuation-driver.sock"
    private let ownSocketPath = "/tmp/cu-desktop-driver.sock"

    func attach(state: AppState) {
        self.state = state
        startSensors()
        startSocketWatcher()
    }

    // MARK: - Sensor tick (Optimized: 10 Hz & conditional on right panel)

    private func startSensors() {
        sensorTimer?.invalidate()
        sensorTimer = Timer.scheduledTimer(
            withTimeInterval: 0.1, repeats: true
        ) { [weak self] _ in
            Task { @MainActor in
                self?.tickSensors()
            }
        }
    }

    private func tickSensors() {
        guard let state else { return }
        sensorTicks &+= 1

        // Only read and publish cursor coordinates when live HUD / viewport is open
        if state.panelsStore.isRightPanelVisible {
            let mouse = NSEvent.mouseLocation
            let primaryScreen = NSScreen.screens.first ?? NSScreen.main
            let screenHeight = primaryScreen?.frame.height ?? 900
            let screenWidth = primaryScreen?.frame.width ?? 1440

            // Invert Y for standard top-left origin (T3/CLI canvas space)
            let topY = max(0, screenHeight - mouse.y)
            let newX = Double(mouse.x)
            let newY = Double(topY)

            // Deduplicate: only publish if coordinates actually changed
            if abs(state.cursorX - newX) >= 1.0 || abs(state.cursorY - newY) >= 1.0 {
                state.cursorX = newX
                state.cursorY = newY
                state.cursorPoint = String(format: "(%.0f, %.0f)", mouse.x, topY)

                if screenWidth > 0, screenHeight > 0 {
                    state.cursorFX = min(max(Double(mouse.x) / Double(screenWidth), 0), 1)
                    state.cursorFY = min(max(Double(topY) / Double(screenHeight), 0), 1)
                }
            }
        }

        // Active frontmost app (read once per second to keep overhead near zero)
        if sensorTicks % 10 == 0 {
            if let front = NSWorkspace.shared.frontmostApplication {
                let name = front.localizedName ?? front.bundleIdentifier ?? "—"
                if state.activeTarget != name && (state.activeTarget == "—" || state.activeTarget.hasPrefix("app:") == false) {
                    state.activeTarget = name
                }
            }
        }
    }

    // MARK: - Socket watcher (1 Hz probe)

    private func startSocketWatcher() {
        socketTimer?.invalidate()
        socketTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) {
            [weak self] _ in
            Task { @MainActor in
                self?.probeSockets()
            }
        }
        probeSockets()
    }

    /// Probes the primary socket first (the one CLI runs against); if absent,
    /// checks our own spawned driver.
    private func probeSockets() {
        let primaryPath = primarySocketPath
        let ownPath = ownSocketPath

        // Async probe on a background thread so the main UI never hitches on a stalled IPC
        Task.detached(priority: .utility) { [weak self] in
            let primary = Self.probeSocket(path: primaryPath)
            await self?.handleProbeResult(primary: primary, ownPath: ownPath)
        }
    }

    private func handleProbeResult(primary: DriverSnapshot, ownPath: String) async {
        if primary.alive {
            applyProbe(primary, label: primarySocketPath, spawned: false)
            return
        }
        let own = await Task.detached(priority: .utility) {
            Self.probeSocket(path: ownPath)
        }.value
        if own.alive {
            applyProbe(own, label: ownPath, spawned: true)
            return
        }
        driverBackend = ""
        bridgeLine = spawnedDriverRunning ? "local sensors · internal driver ready" : "local sensors"
        ensureSpawnedDriver()
    }

    private func applyProbe(_ snapshot: DriverSnapshot, label: String, spawned: Bool) {
        driverBackend = snapshot.backend
        if snapshot.backend.contains("real") {
            bridgeLine = "driver: real (\(label))"
            if !snapshot.appName.isEmpty {
                state?.activeTarget = snapshot.appName
            }
        } else {
            // Simulated (or unknown) backend: attached for lifecycle only,
            // readings stay native — see probeSocket.
            bridgeLine = "driver: simulated (\(spawned ? "internal" : "external")) · local readings"
        }
    }

    // MARK: - Spawned driver lifecycle (SIGINT target)

    private var spawnedDriverRunning: Bool {
        spawnedDriver?.isRunning ?? false
    }

    /// Spawns one simulated driver on the private socket, at most once per
    /// launch, only if the binary is present next to the app.
    private func ensureSpawnedDriver() {
        guard spawnedDriver == nil else { return }
        guard let bin = Self.findDriverBinary() else { return }

        // Clean stale socket file
        try? FileManager.default.removeItem(atPath: ownSocketPath)

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: bin)
        proc.arguments = ["--socket", ownSocketPath]
        proc.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                self?.handleSpawnedExit()
            }
        }
        do {
            try proc.run()
            spawnedDriver = proc
            bridgeLine = "driver: starting"
        } catch {
            bridgeLine = "local sensors"
        }
    }

    private func handleSpawnedExit() {
        if spawnedDriver?.isRunning != true {
            spawnedDriver = nil
        }
        bridgeLine = "local sensors"
    }

    private static func findDriverBinary() -> String? {
        var candidates: [String] = []
        if let env = ProcessInfo.processInfo.environment["COMPUTERUSE_DRIVER_BIN"] {
            candidates.append(env)
        }
        // Dev layout: desktop/ComputerUseDesktop.app next to repo driver/.
        let appDir = Bundle.main.bundleURL.deletingLastPathComponent().path
        candidates.append(appDir + "/driver/target/release/actuation-driver")
        candidates.append(appDir + "/../driver/target/release/actuation-driver")
        for path in candidates {
            if FileManager.default.isExecutableFile(atPath: path) {
                return (path as NSString).standardizingPath
            }
        }
        return nil
    }

    // MARK: - Run session (Emergency stop target)

    /// Emergency stop: parks the run and signals the Rust side with SIGINT.
    /// Only the driver this bridge spawned is ever signalled — an external
    /// CLI-owned driver belongs to its own run and is left alone. The live
    /// CLI run itself is stopped by `AgentRunner.stop()` (same signal).
    func emergencyStop() {
        guard let state else { return }
        if let process = spawnedDriver, process.isRunning {
            process.interrupt()  // SIGINT
            let pid = process.processIdentifier
            state.lastAction = "emergency_stop → SIGINT(\(pid))"
        } else {
            state.lastAction = "emergency_stop (no internal driver)"
        }
    }
}

// MARK: - Socket probe payload

private struct DriverSnapshot {
    let alive: Bool
    let backend: String
    let appName: String
}

extension AgentBridge {
    /// Connects to a Unix domain socket, writes `PING\n`, reads the one-line
    /// JSON response with a 150 ms deadline. Safe to run from detached tasks.
    nonisolated fileprivate static func probeSocket(path: String) -> DriverSnapshot {
        guard FileManager.default.fileExists(atPath: path) else {
            return DriverSnapshot(alive: false, backend: "", appName: "")
        }

        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else {
            return DriverSnapshot(alive: false, backend: "", appName: "")
        }
        defer { close(fd) }

        var timeout = timeval(tv_sec: 0, tv_usec: 150_000)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = path.utf8CString
        guard pathBytes.count < MemoryLayout.size(ofValue: addr.sun_path) else {
            return DriverSnapshot(alive: false, backend: "", appName: "")
        }
        withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
            ptr.withMemoryRebound(to: CChar.self, capacity: pathBytes.count) { dst in
                _ = pathBytes.withUnsafeBufferPointer { src in
                    dst.initialize(from: src.baseAddress!, count: pathBytes.count)
                }
            }
        }

        let addrLen = socklen_t(MemoryLayout<sockaddr_un>.size)
        let connectResult = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                connect(fd, sa, addrLen)
            }
        }
        guard connectResult == 0 else {
            return DriverSnapshot(alive: false, backend: "", appName: "")
        }

        let ping = "{\"action\":\"status\"}\n"
        _ = ping.utf8CString.withUnsafeBufferPointer { buf in
            write(fd, buf.baseAddress, buf.count - 1)
        }

        var buffer = [UInt8](repeating: 0, count: 2048)
        let bytesRead = read(fd, &buffer, buffer.count - 1)
        guard bytesRead > 0 else {
            return DriverSnapshot(alive: true, backend: "connected", appName: "")
        }

        if let json = try? JSONSerialization.jsonObject(
            with: Data(buffer.prefix(bytesRead))
        ) as? [String: Any] {
            let backend = (json["backend"] as? String) ?? "active"
            let app = (json["frontmost_app"] as? String) ?? ""
            return DriverSnapshot(alive: true, backend: backend, appName: app)
        }

        return DriverSnapshot(alive: true, backend: "active", appName: "")
    }
}
