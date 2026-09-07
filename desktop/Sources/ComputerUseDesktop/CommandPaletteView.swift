import SwiftUI

struct CommandPaletteView: View {
    @ObservedObject var state: AppState
    @ObservedObject private var store = StoreDataService.shared
    @State private var query = ""
    @FocusState private var focused: Bool

    private let titles: [String] = ["Yeni görev", "Görevi devam ettir", "Bekleyen onayı onayla", "Verify aç/kapat", "Acil Durdur"]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            TextField("Komut ara…", text: $query).textFieldStyle(.plain).focused($focused)
            ForEach(titles.filter { query.isEmpty || $0.localizedCaseInsensitiveContains(query) }, id: \.self) { title in
                Button { perform(title) } label: {
                    HStack { Text(title); Spacer(); Image(systemName: "return") }
                        .padding(9).contentShape(Rectangle())
                }
                .buttonStyle(.plain).hoverPointer(radius: 6)
                .disabled((title == titles[1] && (store.blockedMissions.isEmpty || state.runStore.isRunning))
                    || (title == titles[2] && store.pendingApprovals.isEmpty)
                    || (title == titles[4] && !state.runStore.isRunning))
            }
            if let approval = store.pendingApprovals.first {
                Text("Onay: " + approval.subGoal + " · " + approval.targetLabel + " · " + approval.risk)
                    .font(.system(size: 10)).foregroundStyle(Theme.textSecondary)
            }
        }
        .font(.system(size: 12)).foregroundStyle(Theme.textPrimary)
        .padding(20).frame(width: 480).background(Theme.canvas)
        .onAppear { focused = true; store.loadControlRecords() }
        .onExitCommand { state.panelsStore.showCommandPalette = false }
    }

    private func perform(_ title: String) {
        state.panelsStore.showCommandPalette = false
        switch title {
        case titles[0]: state.newTask()
        case titles[1]: if let mission = store.blockedMissions.first { store.resumeMission(id: mission.id) }
        case titles[2]: if let approval = store.pendingApprovals.first { store.decideApproval(id: approval.id, approve: true, always: false) }
        case titles[3]: state.verifyEnabled.toggle()
        case titles[4]: state.emergencyStop()
        default: break
        }
    }
}
