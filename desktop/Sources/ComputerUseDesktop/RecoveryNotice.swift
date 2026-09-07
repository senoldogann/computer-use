import Foundation

func recoveryNotice(event: IDEEvent, consecutiveFailures: Int) -> String? {
    if let rung = event.fields["recovery"]?.text ?? event.fields["escalation"]?.text
        ?? event.fields["recovery"]?.object?["action"]?.text {
        return "Recovery · " + rung
    }
    guard event.error != nil else { return nil }
    let rung: String
    switch consecutiveFailures {
    case ...1: rung = "retry"
    case 2: rung = "alternate"
    case 3: rung = "replan"
    default: rung = "abort threshold"
    }
    return "\(consecutiveFailures)× başarısız · \(rung) (IDE çıkarımı)"
}
