import SwiftUI

struct ResumeFirstCard: View {
    @ObservedObject var state: AppState
    @ObservedObject private var store = StoreDataService.shared

    var body: some View {
        if let mission = store.blockedMissions.first {
            VStack(alignment: .leading, spacing: 8) {
                Label(mission.goal, systemImage: "pause.circle").foregroundStyle(Theme.textPrimary)
                Text(mission.blockedReason.isEmpty ? "Ready to resume from checkpoint" : mission.blockedReason)
                    .foregroundStyle(Theme.textSecondary)
                HStack {
                    Button("Devam Ettir") { store.resumeMission(id: mission.id) }
                        .disabled(state.runStore.isRunning)
                    if let approval = store.pendingApprovals.first(where: { $0.missionId == mission.id }) {
                        Button("Onayla") { store.decideApproval(id: approval.id, approve: true, always: false) }
                            .disabled(store.decidingApprovalIDs.contains(approval.id))
                        Text(approval.targetLabel + " · " + approval.risk)
                            .font(.system(size: 10, design: .monospaced)).foregroundStyle(Theme.textSecondary)
                    }
                }.foregroundStyle(Theme.accent).buttonStyle(.plain).hoverPointer(radius: 5)
            }
            .font(.system(size: 12)).padding(12).cardStyle(cornerRadius: 10).padding(.horizontal, 16)
        }
    }
}
