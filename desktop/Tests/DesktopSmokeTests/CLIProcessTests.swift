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
}
