import AppKit
import Combine
import Foundation
import SwiftUI

/// Primary navigation tabs:
/// Live Agent, Extensions (MCP Store), History, Control, Analytics
enum NavigationTab: String, CaseIterable, Identifiable {
    case live = "Live Agent"
    case extensions = "Extensions"
    case history = "History"
    case control = "Control"
    case analytics = "Analytics"

    var id: String { rawValue }

    var icon: String {
        switch self {
        case .live: return "bolt.fill"
        case .extensions: return "sparkles"
        case .history: return "clock.arrow.circlepath"
        case .control: return "shield.fill"
        case .analytics: return "chart.bar.xaxis"
        }
    }

    var shortcutNumber: String {
        switch self {
        case .live: return "1"
        case .extensions: return "2"
        case .history: return "3"
        case .control: return "4"
        case .analytics: return "5"
        }
    }
}

/// Central reactive state tree for the desktop application — now a
/// composition root. Threads, run lifecycle and panel visibility live in
/// dedicated stores (views observe them directly); this object keeps the
/// cross-cutting concerns: navigation tab, composer, model/autonomy settings,
/// sensor telemetry, and the agent subsystems.
@MainActor
final class AppState: ObservableObject {
    // MARK: - Stores (split: ThreadsStore / RunStore / PanelsStore)

    let threadsStore = ThreadsStore()
    let runStore = RunStore()
    let panelsStore = PanelsStore()

    private var cancellables = Set<AnyCancellable>()

    // MARK: - Backward-compatible forwarded properties

    var runState: AgentRunState {
        get { runStore.runState }
        set { runStore.runState = newValue }
    }

    var isRightPanelVisible: Bool {
        get { panelsStore.isRightPanelVisible }
        set { panelsStore.isRightPanelVisible = newValue }
    }

    var isSidebarVisible: Bool {
        get { panelsStore.isSidebarVisible }
        set { panelsStore.isSidebarVisible = newValue }
    }

    var showSettings: Bool {
        get { panelsStore.showSettings }
        set { panelsStore.showSettings = newValue }
    }

    var showStats: Bool {
        get { panelsStore.showStats }
        set { panelsStore.showStats = newValue }
    }

    var showModelPalette: Bool {
        get { panelsStore.showModelPalette }
        set { panelsStore.showModelPalette = newValue }
    }

    var showCommandPalette: Bool {
        get { panelsStore.showCommandPalette }
        set { panelsStore.showCommandPalette = newValue }
    }

    var threads: [AgentThread] {
        get { threadsStore.threads }
        set { threadsStore.threads = newValue }
    }

    var selectedThreadID: UUID? {
        get { threadsStore.selectedThreadID }
        set { threadsStore.selectedThreadID = newValue }
    }

    var activeThread: AgentThread? {
        threadsStore.activeThread
    }

    var isEmptyState: Bool {
        threadsStore.isEmptyState
    }

    var searchText: String {
        get { threadsStore.searchText }
        set { threadsStore.searchText = newValue }
    }

    var scrollTargetID: UUID? {
        get { threadsStore.scrollTargetID }
        set { threadsStore.scrollTargetID = newValue }
    }

    var runCount: Int {
        runStore.runCount
    }

    var succeededCount: Int {
        runStore.succeededCount
    }

    var failedCount: Int {
        runStore.failedCount
    }

    var averageLatency: Double {
        runStore.averageLatency
    }

    var sessionTelemetry: RunTelemetry {
        threadsStore.sessionTelemetry
    }

    // MARK: - Navigation Tabs

    @Published var activeTab: NavigationTab = .live

    // MARK: - Composer & config

    @Published var composerText = ""
    @Published var selectedModel: String
    @Published var autonomyLevel: AutonomyLevel = .fullAccess
    /// Incremented to move keyboard focus into the composer (chip taps).
    @Published var focusComposerCounter: Int = 0

    let exampleChips = [
        "Target with AX",
        "Open Chrome",
        "Verify screen",
        "Export report",
    ]

    // MARK: - Telemetry & Hardware

    @Published var cursorX: Double = 0
    @Published var cursorY: Double = 0
    @Published var cursorFX: Double = 0.5
    @Published var cursorFY: Double = 0.5
    @Published var cursorPoint: String = "(0, 0)"
    @Published var activeTarget: String = "—"
    @Published var lastAction: String = "idle"

    // MARK: - Settings state

    @Published var targetFps: Int {
        didSet { UserDefaults.standard.set(targetFps, forKey: "cu.targetFps") }
    }

    @Published var useRealDriver: Bool {
        didSet { UserDefaults.standard.set(useRealDriver, forKey: "cu.useRealDriver") }
    }

    // MARK: - Trust Mode & MCP Mode (from menu.html)

    @Published var trustMode: Bool {
        didSet { UserDefaults.standard.set(trustMode, forKey: "cu.trustMode") }
    }

    @Published var mcpEnabled: Bool {
        didSet { UserDefaults.standard.set(mcpEnabled, forKey: "cu.mcpEnabled") }
    }

    @Published var verifyEnabled: Bool = UserDefaults.standard.bool(forKey: "cu.verify") {
        didSet { UserDefaults.standard.set(verifyEnabled, forKey: "cu.verify") }
    }
    @Published var displayIndex: Int = UserDefaults.standard.integer(forKey: "cu.display") {
        didSet { UserDefaults.standard.set(displayIndex, forKey: "cu.display") }
    }
    @Published var deadlineSeconds: String = UserDefaults.standard.string(forKey: "cu.deadline") ?? "" {
        didSet { UserDefaults.standard.set(deadlineSeconds, forKey: "cu.deadline") }
    }
    @Published var maxCostUSD: String = UserDefaults.standard.string(forKey: "cu.maxCost") ?? "" {
        didSet { UserDefaults.standard.set(maxCostUSD, forKey: "cu.maxCost") }
    }
    var safetyOptions: RunSafetyOptions {
        RunSafetyOptions(verify: verifyEnabled, display: displayIndex, deadline: deadlineSeconds, maxCost: maxCostUSD)
    }

    @Published var targetApp: String? = nil

    // MARK: - Subsystems

    let bridge: AgentBridge
    let runner: AgentRunner

    // MARK: - Initializer

    init() {
        let savedModel = UserDefaults.standard.string(forKey: "cu.defaultModel")
        let model =
            (savedModel != nil && AgentModel.all.contains(savedModel!))
            ? savedModel! : AgentModel.fallback
        self.selectedModel = model

        let savedFps = UserDefaults.standard.integer(forKey: "cu.targetFps")
        self.targetFps = savedFps >= 1 ? min(savedFps, 60) : 60

        self.useRealDriver = UserDefaults.standard.bool(
            forKey: "cu.useRealDriver")

        let savedTrust = UserDefaults.standard.object(forKey: "cu.trustMode") as? Bool
        self.trustMode = savedTrust ?? false

        let savedMcp = UserDefaults.standard.object(forKey: "cu.mcpEnabled") as? Bool
        self.mcpEnabled = savedMcp ?? true

        self.bridge = AgentBridge()
        self.runner = AgentRunner()

        self.bridge.attach(state: self)
        self.runner.attach(state: self)

        threadsStore.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }.store(in: &cancellables)
        runStore.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }.store(in: &cancellables)
        panelsStore.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }.store(in: &cancellables)
    }

    func setUseRealDriver(_ value: Bool) {
        useRealDriver = value
    }

    func toggleTrustMode() {
        trustMode.toggle()
        let msg = trustMode ? "Trust ON: uninterrupted autonomy (--yes)" : "Trust OFF: approvals required"
        StoreDataService.shared.showToast(msg)
    }

    func toggleMcpEnabled() {
        mcpEnabled.toggle()
        let msg = mcpEnabled ? "MCP Servers: Enabled" : "MCP Servers: Disabled"
        StoreDataService.shared.showToast(msg)
    }

    // MARK: - User actions

    func selectThread(_ id: UUID?) {
        threadsStore.selectThread(id)
        activeTab = .live
    }

    func newTask() {
        threadsStore.clearSelection()
        composerText = ""
        focusComposerCounter += 1
        activeTab = .live
    }

    func deleteThread(_ id: UUID) {
        threadsStore.deleteThread(id)
    }

    func applyChip(_ text: String) {
        activeTab = .live
        composerText = text
        focusComposerCounter += 1
    }

    func setSelectedModel(_ model: String) {
        selectedModel = model
        UserDefaults.standard.set(model, forKey: "cu.defaultModel")
    }

    /// Single entry point for both the Hero send and bottom-docked follow-ups.
    func sendComposer() {
        let goal = composerText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !goal.isEmpty else { return }

        activeTab = .live

        // If runner is active, queue the follow-up prompt into the timeline
        if runStore.isRunning {
            if let activeID = threadsStore.selectedThreadID {
                threadsStore.appendTimeline(
                    threadID: activeID,
                    entry: TimelineEntry(
                        kind: .userMessage,
                        title: goal,
                        detail: "Queued (will start after active task finishes)"
                    )
                )
            }
            composerText = ""
            return
        }

        composerText = ""
        startGoal(goal)
    }

    func resumeCheckpoint(mission: BlockedMissionItem, planID: String) {
        guard !runStore.isRunning else { return }
        // Resuming belongs to the conversation that parked the work: reuse
        // the thread carrying this goal instead of starting a new topic.
        let id = threadsStore.resolveTargetThread(for: mission.goal, reuseIdle: true)
        selectThread(id)
        runner.resume(threadID: id, goal: mission.goal, planID: planID)
    }

    func previewComposer() {
        let goal = composerText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !goal.isEmpty, !runStore.isRunning else { return }
        let id = threadsStore.resolveTargetThread(for: goal)
        guard let index = threadsStore.threads.firstIndex(where: { $0.id == id }) else { return }
        threadsStore.threads[index].previewGoal = goal
        composerText = ""
        selectThread(id)
        runner.preview(threadID: id, goal: goal)
    }

    func executePreview() {
        guard !runStore.isRunning, let thread = threadsStore.activeThread,
            let goal = thread.previewGoal, thread.status == .draft,
            let index = threadsStore.threads.firstIndex(where: { $0.id == thread.id })
        else { return }
        threadsStore.threads[index].previewGoal = nil
        runner.executePreview(threadID: thread.id, goal: goal)
    }

    /// Primary task submission.
    func startGoal(_ goal: String) {
        guard !goal.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }

        activeTab = .live
        let threadID = threadsStore.resolveTargetThread(for: goal)
        selectThread(threadID)
        launchRun(threadID: threadID, goal: goal)
    }

    /// User stop button or Cmd+. / Esc shortcut (Law 5).
    func emergencyStop() {
        runStore.markStopping()
        runner.stop()
        bridge.emergencyStop()
    }

    func restoreDefaultSettings() {
        selectedModel = AgentModel.fallback
        autonomyLevel = .fullAccess
        targetFps = 60
        useRealDriver = false
        panelsStore.restoreDefaults()
    }

    func resetStats() {
        runStore.resetStats()
    }

    // MARK: - Runner callbacks

    func runnerDidStart(threadID: UUID) {
        runStore.didStart(threadID: threadID)
        threadsStore.runnerDidStart(threadID: threadID)
    }

    func runnerDidFinish(
        threadID: UUID, success: Bool, summary: String, latency: Double, stopped: Bool
    ) {
        let isPreview = threadsStore.threads.first(where: { $0.id == threadID })?.previewGoal != nil
        runStore.didFinish(
            threadID: threadID,
            recordCompletion: latency > 0 && !isPreview,
            success: success,
            latency: latency
        )
        let pending = StoreDataService.shared.pendingApprovals.contains { request in
            threadsStore.threads.first(where: { $0.id == threadID })?
                .entries.contains { $0.approvalID == request.id } ?? false
        }
        threadsStore.runnerDidFinish(
            threadID: threadID,
            summary: summary,
            stopped: stopped,
            pendingApproval: pending,
            success: success,
            isPreview: isPreview
        )
    }

    // MARK: - Sensor telemetry

    func updateTelemetry(
        x: Double? = nil,
        y: Double? = nil,
        fx: Double? = nil,
        fy: Double? = nil,
        target: String? = nil,
        action: String? = nil
    ) {
        if let x { cursorX = x }
        if let y { cursorY = y }
        if let fx { cursorFX = fx }
        if let fy { cursorFY = fy }
        cursorPoint = "(\(Int(cursorX)), \(Int(cursorY)))"
        if let target { activeTarget = target }
        if let action { lastAction = action }
    }

    // MARK: - Private helpers

    private func launchRun(threadID: UUID, goal: String) {
        runner.start(threadID: threadID, goal: goal)
    }
}
