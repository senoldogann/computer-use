import AppKit
import Foundation
import SwiftUI

/// Core domain models for the ComputerUse desktop client.
/// Everything here is value-typed and Sendable where appropriate.

enum AgentRunState: Equatable, Sendable {
    case idle
    case running
    case stopping
    case error(String)

    var isRunning: Bool {
        if case .running = self { return true }
        if case .stopping = self { return true }
        return false
    }

    var label: String {
        switch self {
        case .idle: return "Idle"
        case .running: return "Running"
        case .stopping: return "Stopping…"
        case .error(let msg): return "Error: \(msg)"
        }
    }
}

typealias RunState = AgentRunState

enum ThreadStatus: String, Codable, Sendable {
    case draft
    case working
    case done
    case failed
    case idle
    case parked
}

struct AgentThread: Identifiable, Sendable {
    let id: UUID
    var title: String
    var subtitle: String
    var entries: [TimelineEntry]
    var status: ThreadStatus
    var startedAt: Date
    var telemetry = RunTelemetry()
    var previewGoal: String? = nil
    var finishedAt: Date?

    init(
        id: UUID = UUID(),
        title: String,
        subtitle: String = "Created just now",
        status: ThreadStatus = .draft,
        entries: [TimelineEntry] = [],
        startedAt: Date = Date(),
        finishedAt: Date? = nil
    ) {
        self.id = id
        self.title = title
        self.subtitle = subtitle
        self.status = status
        self.entries = entries
        self.startedAt = startedAt
        self.finishedAt = finishedAt
    }

    var statusText: String {
        switch status {
        case .draft: return "Draft"
        case .working: return "Working"
        case .done: return "Done"
        case .failed: return "Failed"
        case .idle: return "Idle"
        case .parked: return "Parked"
        }
    }

    func elapsedLabel(at now: Date) -> String {
        let end = finishedAt ?? now
        let seconds = max(0, Int(end.timeIntervalSince(startedAt)))
        let m = seconds / 60
        let s = seconds % 60
        if m > 0 {
            return String(format: "%dm %02ds", m, s)
        }
        return String(format: "%ds", s)
    }
}

typealias TaskThread = AgentThread

struct TimelineEntry: Identifiable, Sendable {
    enum Kind: String, Codable, Sendable {
        case thinking
        case plan
        case action
        case console
        case userMessage
        case approval
    }

    let id: UUID
    let kind: Kind
    var title: String
    var detail: String
    var planSteps: [PlanStep] = []
    var approvalID: String? = nil
    var isExpanded: Bool
    var isFailure: Bool
    let createdAt: Date

    init(
        id: UUID = UUID(),
        kind: Kind,
        title: String,
        detail: String = "",
        isExpanded: Bool = false,
        isFailure: Bool = false,
        createdAt: Date = Date()
    ) {
        self.id = id
        self.kind = kind
        self.title = title
        self.detail = detail
        self.isExpanded = isExpanded
        self.isFailure = isFailure
        self.createdAt = createdAt
    }
}

/// Autonomy level matching the CLI's safety levels (Law 4).
enum AutonomyLevel: String, CaseIterable, Identifiable, Sendable {
    case supervised = "Supervised"
    case autoAcceptEdits = "Auto-accept edits"
    case auto = "Auto"
    case fullAccess = "Full autonomy (level 3)"

    var id: String { rawValue }

    var iconName: String {
        switch self {
        case .supervised: return "shield.lefthalf.filled"
        case .autoAcceptEdits: return "pencil.and.outline"
        case .auto: return "sparkles"
        case .fullAccess: return "bolt.shield.fill"
        }
    }

    var subtitle: String {
        switch self {
        case .supervised:
            return "Ask confirmation for every action"
        case .autoAcceptEdits:
            return "Auto-confirm edit and typing actions"
        case .auto:
            return "Autonomous; asks only for critical operations"
        case .fullAccess:
            return "Run unattended; park critical actions for approval"
        }
    }

    /// CLI `--level` value for this selection.
    var cliLevel: Int {
        switch self {
        case .supervised: return 1
        case .autoAcceptEdits: return 1
        case .auto: return 2
        case .fullAccess: return 3
        }
    }
}

/// Model catalogue shared by the composer menu, the palette and Settings.
enum AgentModel {
    static let all = [
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.6-sol",
        "gpt-4o",
        "gpt-4o-mini",
        "o3-mini",
        "o1",
    ]
    static let settingsSubset = ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-4o"]
    static let fallback = "gpt-5.6-terra"

    /// CLI `--model` spec for a catalogue id (`openai:<id>` transport).
    static func cliSpec(for model: String) -> String {
        "openai:\(model)"
    }
}

/// Cross-view UI intents delivered over NotificationCenter (menu commands and
/// keyboard shortcuts cannot hold a reference to AppState).
enum UIIntent {
    static let toggleSidebar = Notification.Name("cu.toggleSidebar")
    static let toggleRightPanel = Notification.Name("cu.toggleRightPanel")
    static let openModelPalette = Notification.Name("cu.openModelPalette")
    static let openSettings = Notification.Name("cu.openSettings")
    static let openCommandPalette = Notification.Name("cu.openCommandPalette")
    static let newTask = Notification.Name("cu.newTask")
    static let emergencyStop = Notification.Name("cu.emergencyStop")
    static let selectTabLive = Notification.Name("cu.selectTabLive")
    static let selectTabExtensions = Notification.Name("cu.selectTabExtensions")
    static let selectTabHistory = Notification.Name("cu.selectTabHistory")
    static let selectTabControl = Notification.Name("cu.selectTabControl")
    static let selectTabAnalytics = Notification.Name("cu.selectTabAnalytics")
    static let toggleTrust = Notification.Name("cu.toggleTrust")
}

extension TimelineEntry {
    /// Convenience matching the previous call sites that omitted flags.
    init(kind: Kind, title: String, detail: String) {
        self.init(kind: kind, title: title, detail: detail, isExpanded: false, isFailure: false)
    }

    init(kind: Kind, title: String) {
        self.init(kind: kind, title: title, detail: "", isExpanded: false, isFailure: false)
    }
}

/// Window accessor to configure macOS window transparency and fullSizeContentView.
struct WindowAccessor: NSViewRepresentable {
    func makeNSView(context: Context) -> WindowChromeView {
        WindowChromeView()
    }

    func updateNSView(_ nsView: WindowChromeView, context: Context) {
        // Re-assert on every SwiftUI update: the scene can reset styleMask
        // (dropping fullSizeContentView) after a one-shot setup, which pushes
        // all content below a 28pt titlebar.
        nsView.apply()
    }
}

/// Zero-size helper view that owns window chrome setup and re-applies it
/// whenever the view joins a window or SwiftUI refreshes the hierarchy.
final class WindowChromeView: NSView {
    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        apply()
        // One deferred pass: the scene may still be settling styleMask.
        DispatchQueue.main.async { [weak self] in self?.apply() }
    }

    func apply() {
        guard let window else { return }
        TitlebarHitTestHelper.installTitlebarHitTestPassThrough()
        guard !window.titlebarAppearsTransparent || window.titleVisibility != .hidden
            || !window.styleMask.contains(.fullSizeContentView) || window.isOpaque
            || window.backgroundColor != .clear else { return }
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        if !window.styleMask.contains(.fullSizeContentView) {
            window.styleMask.insert(.fullSizeContentView)
        }
        window.isOpaque = false
        window.backgroundColor = .clear
        // SwiftUI's hiddenTitleBar style enables background dragging, which
        // makes the whole window move on any mouse-down that isn't claimed by
        // a control — that is what swallows the top-bar toggle clicks. Turn it
        // off; dragging is handled explicitly by TitleBarDragSurface.
        window.isMovableByWindowBackground = false
    }
}
