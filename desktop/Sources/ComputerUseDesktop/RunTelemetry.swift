import Foundation

struct RunTelemetry: Equatable, Sendable {
    var totalTokens: Int = 0
    var costUSD: Double? = nil
    var calls: Int = 0
    var elapsedSeconds: Double = 0

    var label: String {
        let cost = costUSD.map { String(format: "$%.4f", $0) } ?? "cost unavailable"
        return "\(totalTokens) tokens · \(cost) · \(calls) calls"
    }
}
