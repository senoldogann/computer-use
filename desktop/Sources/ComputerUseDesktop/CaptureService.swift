import AppKit
import Foundation
import ScreenCaptureKit
import VideoToolbox

/// ScreenCaptureKit live-stream service.
///
/// Runs an `SCStream` on the primary display and feeds `CGImage` frames to a
/// `FrameStore`. Automatically excludes ComputerUse's own window so the live
/// preview never feeds back into itself.
///
/// Falls back cleanly:
/// - When Screen Recording permission has not been granted, reports `hasPermission = false`
///   so the UI can present an actionable prompt.
/// - When running in an environment without display hardware (tests, headless VMs),
///   reports `isStreaming = false` with a diagnostic status message.
@MainActor
final class CaptureService: ObservableObject {
    static let shared = CaptureService()

    /// Target capture rate in frames per second. Clamped to [1, 60].
    @Published var targetFps: Int = 60 {
        didSet {
            let clamped = max(1, min(60, targetFps))
            if clamped != targetFps { targetFps = clamped }
            if isStreaming {
                start()
            }
        }
    }

    /// Backing buffer that delivers frames to SwiftUI views.
    let frames = FrameStore()

    @Published private(set) var isStreaming = false
    @Published private(set) var hasPermission = false
    @Published private(set) var statusLine = "Preparing viewport…"
    @Published private(set) var fps = 0

    private let outputQueue = DispatchQueue(label: "com.computeruse.desktop.capture")
    private var stream: SCStream?
    private var handler: FrameHandler?
    private var framesInWindow = 0
    private var windowStart = CFAbsoluteTimeGetCurrent()
    /// The system consent prompt fires only once per launch on its own;
    /// every later attempt comes from the explicit request button.
    private var askedPermission = false
    private var configuredFps = 0
    private var generation = UUID()
    private var startTask: Task<Void, Never>?

    /// Checks the Screen Recording consent and starts streaming when granted.
    /// Called when the right panel first appears and whenever the app becomes
    /// active again — so a grant issued in Settings while the app runs picks
    /// up the moment the user returns, without a manual relaunch.
    private func start(targetFps: Int, generation: UUID) async {
        guard self.generation == generation else { return }
        let fps = max(targetFps, 1)
        if isStreaming, stream != nil, configuredFps == fps { return }
        if stream != nil {
            let running = stream
            stream = nil
            handler = nil
            isStreaming = false
            self.fps = 0
            do { try await running?.stopCapture() }
            catch { statusLine = "Failed to stop stream: \(error.localizedDescription)"; return }
            guard self.generation == generation else { return }
        }
        hasPermission = CGPreflightScreenCaptureAccess()
        guard hasPermission else {
            statusLine = "Screen recording permission required"
            if !askedPermission {
                askedPermission = true
                CGRequestScreenCaptureAccess()
            }
            return
        }
        do {
            try await startStream(targetFps: fps, generation: generation)
        } catch {
            statusLine = "Failed to start stream: \(error.localizedDescription)"
        }
    }

    func stop() {
        generation = UUID()
        startTask?.cancel()
        frames.frame = nil
        let running = stream
        stream = nil
        handler = nil
        isStreaming = false
        fps = 0
        configuredFps = 0
        if let running {
            Task {
                do { try await running.stopCapture() }
                catch { statusLine = "Failed to stop stream: \(error.localizedDescription)" }
            }
        }
    }

    func start() {
        let previous = startTask
        let requestedGeneration = UUID()
        generation = requestedGeneration
        startTask = Task {
            await previous?.value
            await start(targetFps: targetFps, generation: requestedGeneration)
        }
    }

    /// User tapped "Request Permission" — prompts system dialog if not already asked.
    func requestPermission() {
        CGRequestScreenCaptureAccess()
        hasPermission = CGPreflightScreenCaptureAccess()
    }

    /// User tapped "Open Settings" — opens Privacy & Security -> Screen Recording.
    func openScreenRecordingSettings() {
        if let url = URL(
            string:
                "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
        ) {
            NSWorkspace.shared.open(url)
        }
    }

    // MARK: - Private streaming setup

    private func startStream(targetFps: Int, generation: UUID) async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: true)

        guard self.generation == generation else { return }
        guard let display = Self.mainDisplay(in: content) else {
            throw CaptureError.noDisplay
        }

        let filter: SCContentFilter
        let ownPID = ProcessInfo.processInfo.processIdentifier
        let excluded = content.windows.filter { $0.owningApplication?.processID == ownPID }
        if !excluded.isEmpty {
            filter = SCContentFilter(display: display, excludingWindows: excluded)
        } else {
            filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        }

        let config = SCStreamConfiguration()
        config.width = min(display.width, 1920)
        config.height = max(1, display.height * config.width / display.width)
        config.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(targetFps))
        config.queueDepth = 3
        config.pixelFormat = kCVPixelFormatType_32BGRA
        config.showsCursor = true

        let newStream = SCStream(filter: filter, configuration: config, delegate: nil)
        let frameHandler = FrameHandler(parent: self)
        try newStream.addStreamOutput(frameHandler, type: .screen, sampleHandlerQueue: outputQueue)
        try await newStream.startCapture()

        guard self.generation == generation else {
            try await newStream.stopCapture()
            return
        }
        self.stream = newStream
        self.handler = frameHandler
        self.isStreaming = true
        self.configuredFps = targetFps
        self.fps = 0
        self.framesInWindow = 0
        self.windowStart = CFAbsoluteTimeGetCurrent()
        self.statusLine =
            "Live · \(display.width)×\(display.height) · excluding own window (\(excluded.count))"
    }

    fileprivate func publishFrame(_ image: CGImage, pixelSize: CGSize) {
        guard isStreaming else { return }
        frames.frame = image
        frames.pixelSize = pixelSize
        framesInWindow += 1
        let now = CFAbsoluteTimeGetCurrent()
        let elapsed = now - windowStart
        if elapsed >= 1.0 {
            fps = Int(Double(framesInWindow) / elapsed)
            framesInWindow = 0
            windowStart = now
        }
    }

    fileprivate func handleStreamError(_ error: Error) {
        stream = nil
        handler = nil
        isStreaming = false
        fps = 0
        configuredFps = 0
        statusLine = "Stream stopped: \(error.localizedDescription)"
    }

    /// Matches the physical main screen to its ScreenCaptureKit display.
    private static func mainDisplay(in content: SCShareableContent) -> SCDisplay? {
        if let number = NSScreen.main?.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")]
            as? NSNumber
        {
            let id = CGDirectDisplayID(truncating: number)
            if let match = content.displays.first(where: { $0.displayID == id }) {
                return match
            }
        }
        return content.displays.first
    }
}

private enum CaptureError: LocalizedError {
    case noDisplay

    var errorDescription: String? {
        switch self {
        case .noDisplay: return "no usable display found"
        }
    }
}

/// Trampoline that bridges SCStreamOutput delegate callbacks to the CaptureService actor.
private final class FrameHandler: NSObject, SCStreamOutput {
    private weak var parent: CaptureService?

    init(parent: CaptureService) {
        self.parent = parent
    }

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of type: SCStreamOutputType
    ) {
        guard type == .screen,
            let pixelBuffer = sampleBuffer.imageBuffer,
            CMSampleBufferGetNumSamples(sampleBuffer) > 0
        else { return }

        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let size = CGSize(width: width, height: height)

        var cgImage: CGImage?
        VTCreateCGImageFromCVPixelBuffer(pixelBuffer, options: nil, imageOut: &cgImage)
        guard let image = cgImage else { return }

        DispatchQueue.main.async { [weak self] in
            self?.parent?.publishFrame(image, pixelSize: size)
        }
    }
}

