import Combine
import Foundation

/// Owns the live run state and the session diagnostics counters (AppState
/// split, part 2/3). The run store knows *that* a run is active and what it
/// cost; the thread store knows *which* thread carries it.
@MainActor
final class RunStore: ObservableObject {
    @Published var runState: AgentRunState = .idle
    @Published private(set) var runningThreadID: UUID?
    @Published private(set) var runStartedAt: Date?

    // MARK: - Diagnostics & Stats

    @Published private(set) var runCount: Int = 0
    @Published private(set) var succeededCount: Int = 0
    @Published private(set) var failedCount: Int = 0
    @Published private(set) var totalLatency: Double = 0.0

    var isRunning: Bool {
        runState.isRunning
    }

    var averageLatency: Double {
        runCount > 0 ? totalLatency / Double(runCount) : 0.0
    }

    // MARK: - Lifecycle

    func didStart(threadID: UUID) {
        runState = .running
        runningThreadID = threadID
        runStartedAt = Date()
    }

    func didFinish(threadID: UUID, recordCompletion: Bool, success: Bool, latency: Double) {
        if recordCompletion {
            recordRunCompletion(success: success, latency: latency)
        }
        runState = .idle
        runningThreadID = nil
        runStartedAt = nil
    }

    func markStopping() {
        runState = .stopping
    }

    func recordRunCompletion(success: Bool, latency: Double) {
        runCount += 1
        if success { succeededCount += 1 } else { failedCount += 1 }
        totalLatency += latency
    }

    func resetStats() {
        runCount = 0
        succeededCount = 0
        failedCount = 0
        totalLatency = 0.0
    }
}