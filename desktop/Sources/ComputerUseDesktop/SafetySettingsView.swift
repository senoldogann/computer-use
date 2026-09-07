import AppKit
import SwiftUI

struct SafetySettingsView: View {
    @ObservedObject var state: AppState
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Toggle("Visual verification", isOn: $state.verifyEnabled).tint(Theme.accent)
            Picker("Display", selection: $state.displayIndex) {
                Text("0 · Main display").tag(0)
                ForEach(Array(NSScreen.screens.enumerated()), id: \.offset) { index, screen in
                    Text("\(index + 1) · \(screen.localizedName)").tag(index + 1)
                }
            }
            TextField("Deadline (seconds, optional)", text: $state.deadlineSeconds)
            TextField("Max cost $ (optional)", text: $state.maxCostUSD)
        }
        .font(.system(size: 12)).foregroundStyle(Theme.textPrimary)
        .textFieldStyle(.plain).padding(14).cardStyle(cornerRadius: 10)
    }
}
