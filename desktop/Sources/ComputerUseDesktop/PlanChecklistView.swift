import SwiftUI

struct PlanChecklistView: View {
    @ObservedObject var state: AppState
    let entry: TimelineEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Plan · \(entry.planSteps.filter { $0.status == "completed" }.count)/\(entry.planSteps.count)")
                .font(.system(size: 11, design: .monospaced)).foregroundStyle(Theme.textSecondary)
            if state.activeThread?.previewGoal != nil && state.activeThread?.status == .draft {
                Button("Çalıştır", systemImage: "play.fill") { state.executePreview() }
                    .foregroundStyle(Theme.accent).buttonStyle(.plain).hoverPointer(radius: 5)
            }
            ForEach(entry.planSteps) { step in
                Button {
                    state.scrollTargetID = step.targetEntryID ?? entry.id
                } label: {
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: step.status == "completed" ? "checkmark.circle.fill" : "circle")
                            .foregroundStyle(step.status == "completed" ? Theme.accent : Theme.textMuted)
                        Text(step.description).foregroundStyle(Theme.textPrimary)
                        Spacer()
                        Text(step.status).font(.system(size: 10, design: .monospaced)).foregroundStyle(Theme.textSecondary)
                    }
                }
                .buttonStyle(.plain).hoverPointer(radius: 5)
            }
        }
        .font(.system(size: 12)).padding(12).cardStyle(cornerRadius: 10)
    }
}
