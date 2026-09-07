import Foundation

struct RunSafetyOptions: Sendable {
    let verify: Bool
    let display: Int
    let deadline: String
    let maxCost: String

    func arguments() throws -> [String] {
        guard display >= 0 else { throw RunOptionError.invalid("Display") }
        var args: [String] = ["--display", String(display)]
        if verify { args.append("--verify") }
        args += try budgetArgument(deadline, flag: "--deadline-seconds")
        args += try budgetArgument(maxCost, flag: "--max-cost")
        return args
    }
}

enum RunOptionError: LocalizedError {
    case invalid(String)
    var errorDescription: String? {
        switch self { case .invalid(let field): return "\(field) must be a positive finite number." }
    }
}

private func budgetArgument(_ input: String, flag: String) throws -> [String] {
    let value = input.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !value.isEmpty else { return [] }
    guard let number = Double(value), number.isFinite, number > 0 else { throw RunOptionError.invalid(flag) }
    return [flag, value]
}
