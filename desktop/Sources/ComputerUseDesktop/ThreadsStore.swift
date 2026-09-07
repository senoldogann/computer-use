import Combine
import Foundation

/// Owns the workspace thread list and every timeline mutation (AppState split,
/// part 1/3). Threads are the durable conversation surface: entries, plans,
/// approvals, per-thread telemetry and the user's console preferences.
///
/// Kept `@MainActor` like the rest of the app state; views observe this store
/// directly (not through AppState) so SwiftUI invalidates on thread changes.
@MainActor
final class ThreadsStore: ObservableObject {
    // MARK: - Workspace threads

    @Published var threads: [AgentThread] = []
    @Published var selectedThreadID: UUID?
    @Published var threadPendingDeletion: AgentThread?
    @Published var searchText = ""
    @Published var scrollTargetID: UUID?
    @Published var hideConsole = false
    @Published var consoleFilter = ""

    // MARK: - Derived

    var activeThread: AgentThread? {
        guard let selectedThreadID else { return nil }
        return threads.first { $0.id == selectedThreadID }
    }

    var isEmptyState: Bool {
        activeThread == nil
    }

    var filteredThreads: [AgentThread] {
        if searchText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return threads
        }
        let needle = searchText.lowercased()
        return threads.filter {
            $0.title.lowercased().contains(needle)
                || $0.subtitle.lowercased().contains(needle)
        }
    }

    // MARK: - Selection & lifecycle

    func selectThread(_ id: UUID?) {
        selectedThreadID = id
    }

    func clearSelection() {
        selectedThreadID = nil
    }

    func deleteThread(_ id: UUID) {
        threads.removeAll { $0.id == id }
        if selectedThreadID == id {
            selectedThreadID = threads.first?.id
        }
    }

    /// Creates a fresh conversation for a new goal, or — for a mission
    /// resume — attaches to the thread that already carries that goal.
    /// Returns the thread id the run should attach to.
    ///
    /// A new message ALWAYS becomes visible: either as the new thread's first
    /// entry (new sidebar topic), or appended to the reused thread. The old
    /// behaviour silently reused an idle thread WITHOUT appending the message
    /// — the task started, the user's words appeared nowhere, and no new
    /// topic was created.
    func resolveTargetThread(for goal: String, reuseIdle: Bool = false) -> UUID {
        if reuseIdle, let existing = threadID(forGoal: goal) {
            return existing
        }
        let title = deriveTitle(from: goal)
        let thread = AgentThread(
            title: title,
            subtitle: "In progress…",
            status: .working,
            entries: [
                TimelineEntry(
                    kind: .userMessage,
                    title: goal,
                    detail: ""
                )
            ]
        )
        threads.insert(thread, at: 0)
        return thread.id
    }

    /// The thread whose conversation carries exactly this goal message, or
    /// nil. Used by mission resume to keep working in the conversation that
    /// parked the work instead of scattering it into a new topic.
    func threadID(forGoal goal: String) -> UUID? {
        threads.first { thread in
            thread.entries.contains { $0.kind == .userMessage && $0.title == goal }
        }?.id
    }

    // MARK: - Runner-driven thread updates

    func runnerDidStart(threadID: UUID) {
        guard let index = threads.firstIndex(where: { $0.id == threadID }) else { return }
        threads[index].status = .working
        threads[index].startedAt = Date()
        threads[index].finishedAt = nil
    }

    func runnerDidFinish(
        threadID: UUID,
        summary: String,
        stopped: Bool,
        pendingApproval: Bool,
        success: Bool,
        isPreview: Bool
    ) {
        guard let index = threads.firstIndex(where: { $0.id == threadID }) else { return }
        threads[index].finishedAt = Date()
        // A user stop parks the work (idle, resumable) — it is not a failure.
        if stopped {
            threads[index].status = .idle
            threads[index].subtitle = summary
        } else {
            threads[index].status =
                pendingApproval
                ? .parked
                : (success ? (isPreview ? .draft : .done) : .failed)
            threads[index].subtitle = summary
        }
    }

    // MARK: - Timeline mutation helpers

    func appendTimeline(threadID: UUID, entry: TimelineEntry) {
        guard let index = threads.firstIndex(where: { $0.id == threadID }) else { return }
        threads[index].entries.append(entry)
        if threads[index].entries.count > 2000 {
            threads[index].entries.removeFirst(threads[index].entries.count - 2000)
            threads[index].subtitle = "Showing latest 2,000 entries; full history in agent traces"
        }
    }

    func setEntryExpanded(threadID: UUID, entryID: UUID, expanded: Bool) {
        guard let index = threads.firstIndex(where: { $0.id == threadID }) else { return }
        if let entryIndex = threads[index].entries.firstIndex(where: { $0.id == entryID }) {
            threads[index].entries[entryIndex].isExpanded = expanded
        }
    }

    /// Single-shot collapse/expand for the whole timeline group: sets every
    /// entry that actually has detail to the same state. Entries without
    /// detail have no chevron, so touching them would only dirty the store.
    func setAllEntriesExpanded(threadID: UUID, expanded: Bool) {
        guard let index = threads.firstIndex(where: { $0.id == threadID }) else { return }
        for i in 0..<threads[index].entries.count {
            if !threads[index].entries[i].detail.isEmpty {
                threads[index].entries[i].isExpanded = expanded
            }
        }
    }

    func updateConsole(threadID: UUID, line: String) {
        guard let index = threads.firstIndex(where: { $0.id == threadID }) else { return }
        let line = String(line.prefix(8192))
        let isFailure = line.lowercased().contains("error:") || line.hasPrefix("Traceback (")
        if let last = threads[index].entries.indices.last, threads[index].entries[last].kind == .console,
            threads[index].entries[last].detail.count + line.count + 1 <= 8192
        {
            threads[index].entries[last].isFailure = threads[index].entries[last].isFailure || isFailure
            if threads[index].entries[last].detail.isEmpty {
                threads[index].entries[last].detail = line
            } else {
                threads[index].entries[last].detail += "\n" + line
            }
        } else {
            appendTimeline(
                threadID: threadID,
                entry: TimelineEntry(
                    kind: .console,
                    title: "console",
                    detail: line,
                    isFailure: isFailure
                )
            )
        }
    }

    func collapseOlderThinking(threadID: UUID, except: UUID) {
        guard let index = threads.firstIndex(where: { $0.id == threadID }) else { return }
        for i in 0..<threads[index].entries.count {
            if threads[index].entries[i].kind == .thinking && threads[index].entries[i].id != except {
                threads[index].entries[i].isExpanded = false
            }
        }
    }

    func updateRunTelemetry(threadID: UUID, telemetry: RunTelemetry) {
        guard let index = threads.firstIndex(where: { $0.id == threadID }) else { return }
        threads[index].telemetry = telemetry
    }

    var sessionTelemetry: RunTelemetry {
        let values = threads.map(\.telemetry)
        return RunTelemetry(
            totalTokens: values.reduce(0) { $0 + $1.totalTokens },
            costUSD: values.allSatisfy { $0.costUSD != nil || $0.calls == 0 }
                ? values.reduce(0) { $0 + ($1.costUSD ?? 0) } : nil,
            calls: values.reduce(0) { $0 + $1.calls },
            elapsedSeconds: values.reduce(0) { $0 + $1.elapsedSeconds }
        )
    }

    func updatePlan(threadID: UUID, steps: [PlanStep], goal: String? = nil) {
        guard let index = threads.firstIndex(where: { $0.id == threadID }), !steps.isEmpty else { return }
        let linked = steps.map { step in
            PlanStep(
                id: step.id,
                description: step.description,
                status: step.status,
                targetEntryID: threads[index].entries.last(where: { $0.kind == .thinking && $0.title == step.description })?.id
            )
        }
        if let planIndex = threads[index].entries.lastIndex(where: { $0.kind == .plan }) {
            if let goal { threads[index].entries[planIndex].planGoal = goal }
            threads[index].entries[planIndex].planSteps = linked
        } else {
            var entry = TimelineEntry(kind: .plan, title: goal ?? "Plan")
            entry.planGoal = goal
            entry.planSteps = linked
            appendTimeline(threadID: threadID, entry: entry)
        }
    }

    func syncApprovals(_ requests: [PendingApprovalItem]) {
        for request in requests {
            guard !request.goal.isEmpty,
                let index = threads.firstIndex(where: { thread in
                    thread.entries.contains { $0.kind == .userMessage && $0.title == request.goal }
                })
            else { continue }
            if !threads[index].entries.contains(where: { $0.approvalID == request.id }) {
                var entry = TimelineEntry(kind: .approval, title: request.subGoal)
                entry.approvalID = request.id
                let position = threads[index].entries.lastIndex { $0.title == request.subGoal }
                threads[index].entries.insert(entry, at: position.map { $0 + 1 } ?? threads[index].entries.count)
            }
            if request.decision == "pending" && threads[index].status != .working {
                threads[index].status = .parked
            }
        }
    }

    // MARK: - Private helpers

    private func deriveTitle(from goal: String) -> String {
        let singleLine = goal.components(separatedBy: .newlines).first ?? goal
        let trimmed = singleLine.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.count <= 44 { return trimmed }
        let index = trimmed.index(trimmed.startIndex, offsetBy: 41)
        return String(trimmed[..<index]) + "…"
    }
}