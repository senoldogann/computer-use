import Foundation

indirect enum EventValue: Decodable, Sendable {
    case text(String), number(Double), flag(Bool), object([String: EventValue]), array([EventValue]), null

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let text = try? value.decode(String.self) { self = .text(text) }
        else if let number = try? value.decode(Double.self) { self = .number(number) }
        else if let flag = try? value.decode(Bool.self) { self = .flag(flag) }
        else if let object = try? value.decode([String: EventValue].self) { self = .object(object) }
        else { self = .array(try value.decode([EventValue].self)) }
    }
    var text: String? { if case .text(let value) = self { return value }; return nil }
    var number: Double? { if case .number(let value) = self { return value }; return nil }
    var object: [String: EventValue]? { if case .object(let value) = self { return value }; return nil }
    var array: [EventValue]? { if case .array(let value) = self { return value }; return nil }
}

struct PlanStep: Identifiable, Equatable, Sendable {
    let id: Int
    let description: String
    let status: String
    let targetEntryID: UUID?
}

struct IDEEvent: Sendable {
    let fields: [String: EventValue]
    var type: String { fields["type"]?.text ?? "step" }
    var subGoal: String { fields["sub_goal"]?.text ?? "" }
    var error: String? { fields["error"]?.text }
    var action: [String: EventValue] { fields["action"]?.object ?? [:] }
    var plan: [PlanStep] {
        let values = fields["plan"]?.array ?? fields["plan"]?.object?["sub_goals"]?.array ?? []
        return values.enumerated().compactMap { index, value in
            let object = value.object
            guard let text = value.text ?? object?["description"]?.text else { return nil }
            return PlanStep(id: Int(object?["index"]?.number ?? Double(index)), description: text,
                status: object?["status"]?.text ?? "pending", targetEntryID: nil)
        }
    }
    /// The goal a plan event decomposes (``plan.goal`` from the CLI), shown
    /// in the plan card header so the user can recognise the plan at a glance.
    var planGoal: String? { fields["plan"]?.object?["goal"]?.text }
    static func decode(_ json: String) throws -> IDEEvent {
        let fields = try JSONDecoder().decode([String: EventValue].self, from: Data(json.utf8))
        return IDEEvent(fields: fields["step"]?.object ?? fields)
    }
}
