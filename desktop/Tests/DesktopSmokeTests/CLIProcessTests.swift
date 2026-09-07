import Foundation
import XCTest
@testable import ComputerUseDesktop

final class CLIProcessTests: XCTestCase {
    @MainActor
    func testReportAndFailurePropagation() async throws {
        let store = StoreDataService()
        let output = try await store.runCliCommand(args: ["--help"])
        XCTAssertTrue(output.contains("--driver"))
        do {
            _ = try await store.runCliCommand(args: ["--invalid-desktop-smoke-flag"])
            XCTFail("Invalid CLI flag must fail")
        } catch {
            XCTAssertTrue(error.localizedDescription.contains("unrecognized arguments"))
        }
    }

    @MainActor
    func testConsoleRetention() {
        let state = AppState()
        let id = UUID()
        state.threadsStore.threads = [AgentThread(id: id, title: "Smoke")]
        for _ in 0..<2100 {
            state.threadsStore.updateConsole(threadID: id, line: String(repeating: "x", count: 9000))
        }
        XCTAssertEqual(state.threadsStore.threads[0].entries.count, 2000)
        XCTAssertTrue(state.threadsStore.threads[0].entries.allSatisfy { $0.detail.count <= 8192 })
    }

    @MainActor
    func testNewGoalAlwaysCreatesFreshThreadWithVisibleMessage() {
        let state = AppState()
        let firstID = state.threadsStore.resolveTargetThread(for: "first goal")
        XCTAssertEqual(state.threadsStore.threads.count, 1)

        // A second goal in the same chat must NOT silently reuse the thread:
        // the message has to be visible and a new sidebar topic created.
        let secondID = state.threadsStore.resolveTargetThread(for: "second goal")
        XCTAssertNotEqual(firstID, secondID)
        XCTAssertEqual(state.threadsStore.threads.count, 2)
        let second = state.threadsStore.threads[0]
        XCTAssertEqual(second.title, "second goal")
        XCTAssertTrue(second.entries.contains { $0.kind == .userMessage && $0.title == "second goal" })
    }

    @MainActor
    func testResumeReusesTheThreadCarryingTheGoal() {
        let state = AppState()
        _ = state.threadsStore.resolveTargetThread(for: "parked mission")
        let reused = state.threadsStore.resolveTargetThread(for: "parked mission", reuseIdle: true)
        XCTAssertEqual(state.threadsStore.threads.count, 1)
        XCTAssertEqual(state.threadsStore.threads[0].id, reused)
    }

    @MainActor
    func testUpdatePlanCarriesGoalIntoCardHeader() {
        let state = AppState()
        let id = state.threadsStore.resolveTargetThread(for: "open chrome then open youtube")
        state.threadsStore.updatePlan(
            threadID: id,
            steps: [
                PlanStep(id: 0, description: "open chrome", status: "in_progress", targetEntryID: nil),
                PlanStep(id: 1, description: "open youtube", status: "pending", targetEntryID: nil),
            ],
            goal: "open chrome then open youtube"
        )
        let planEntry = state.threadsStore.threads[0].entries.first { $0.kind == .plan }
        XCTAssertNotNil(planEntry)
        XCTAssertEqual(planEntry?.planGoal, "open chrome then open youtube")
        XCTAssertEqual(planEntry?.planSteps.count, 2)
        // A streamed update replaces the steps in place — the live checklist.
        state.threadsStore.updatePlan(
            threadID: id,
            steps: [
                PlanStep(id: 0, description: "open chrome", status: "completed", targetEntryID: nil),
                PlanStep(id: 1, description: "open youtube", status: "in_progress", targetEntryID: nil),
            ],
            goal: "open chrome then open youtube"
        )
        let updated = state.threadsStore.threads[0].entries.first { $0.kind == .plan }
        XCTAssertEqual(updated?.planSteps.first?.status, "completed")
        XCTAssertEqual(updated?.planSteps.last?.status, "in_progress")
    }
}
