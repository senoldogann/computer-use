import SwiftUI

struct PermissionStatusFooter: View {
    @ObservedObject var panelsStore: PanelsStore
    @State private var accessibility = SystemStatus.accessibilityStatus().ok
    @State private var recording = SystemStatus.screenRecordingStatus().ok

    init(state: AppState) {
        self.panelsStore = state.panelsStore
    }

    var body: some View {
        HStack(spacing: 6) {
            Label("AX", systemImage: accessibility ? "checkmark.circle" : "exclamationmark.circle")
            Label("Screen", systemImage: recording ? "checkmark.circle" : "exclamationmark.circle")
            Spacer(minLength: 0)
            if !accessibility || !recording {
                Button("Düzelt") {
                    panelsStore.showSettings = true
                    if !recording { CaptureService.shared.openScreenRecordingSettings() }
                }.buttonStyle(.plain).foregroundStyle(Theme.accent).hoverPointer(radius: 4)
            }
        }
        .font(.system(size: 10, design: .monospaced)).foregroundStyle(Theme.textSecondary)
        .padding(.horizontal, 12).padding(.vertical, 6)
        .task {
            while !Task.isCancelled {
                accessibility = SystemStatus.accessibilityStatus().ok
                recording = SystemStatus.screenRecordingStatus().ok
                do { try await Task.sleep(for: .seconds(2)) }
                catch is CancellationError { return }
                catch { StoreDataService.shared.showToast(error.localizedDescription); return }
            }
        }
    }
}
