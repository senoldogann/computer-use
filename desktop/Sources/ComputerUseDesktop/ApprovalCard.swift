import SwiftUI

struct ApprovalCard: View {
    let requestID: String
    @ObservedObject private var store = StoreDataService.shared

    var body: some View {
        if let request = store.approvalRecords.first(where: { $0.id == requestID }) {
            VStack(alignment: .leading, spacing: 10) {
                Label(request.subGoal, systemImage: "hand.raised")
                    .foregroundStyle(Theme.textPrimary)
                Text(request.targetLabel.isEmpty ? request.actionType : request.targetLabel)
                    .foregroundStyle(Theme.textSecondary)
                HStack {
                    Text(request.risk + " · " + request.decision)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(Theme.accent)
                        .padding(5).background(Theme.cardElevated).clipShape(Capsule())
                    Spacer()
                    if request.decision == "pending" {
                        Button("Onayla") { store.decideApproval(id: request.id, approve: true, always: false) }
                        Button("Reddet") { store.decideApproval(id: request.id, approve: false, always: false) }
                    }
                }
                .buttonStyle(.plain).hoverPointer(radius: 6)
                .disabled(store.decidingApprovalIDs.contains(request.id))
            }
            .font(.system(size: 12)).padding(12).cardStyle(cornerRadius: 10)
        }
    }
}
