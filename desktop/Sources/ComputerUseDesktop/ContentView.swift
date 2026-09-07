import SwiftUI

/// Main container view.
/// Layout:
///   [ Left Sidebar ] [ Center Stage (empty or active task) ] [ Right Panel (live viewport) ]
///
/// Responsive rules:
/// - Window width < 1180 -> right panel automatically collapses
/// - Window width >= 1180 -> right panel restores previous state
/// - Cmd+B toggles left sidebar; Cmd+J toggles right panel
struct ContentView: View {
    @ObservedObject var state: AppState
    @ObservedObject var threadsStore: ThreadsStore
    @ObservedObject var runStore: RunStore
    @ObservedObject var panelsStore: PanelsStore

    init(state: AppState) {
        self.state = state
        self.threadsStore = state.threadsStore
        self.runStore = state.runStore
        self.panelsStore = state.panelsStore
    }

    var body: some View {
        ZStack {
            Theme.canvas.ignoresSafeArea()

            HStack(spacing: 0) {
                if panelsStore.isSidebarVisible {
                    SidebarView(state: state)
                        .frame(width: 245)
                        .transition(.move(edge: .leading))
                }

                CenterStageView(state: state)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)

                if panelsStore.isRightPanelVisible {
                    RightPanelView(state: state)
                        .frame(width: 320)
                        .transition(.move(edge: .trailing))
                }
            }

            if panelsStore.showSettings {
                SettingsView(state: state)
                    .transition(.opacity.combined(with: .scale(scale: 0.98)))
                    .zIndex(100)
            }

            if let thread = threadsStore.threadPendingDeletion {
                DeleteConfirmationModal(
                    thread: thread,
                    onConfirm: {
                        withAnimation(.easeInOut(duration: 0.18)) {
                            threadsStore.deleteThread(thread.id)
                            threadsStore.threadPendingDeletion = nil
                        }
                    },
                    onCancel: {
                        withAnimation(.easeInOut(duration: 0.18)) {
                            threadsStore.threadPendingDeletion = nil
                        }
                    }
                )
                .transition(.opacity.combined(with: .scale(scale: 0.98)))
                .zIndex(200)
            }
        }
        .onReceive(StoreDataService.shared.$approvalRecords) { threadsStore.syncApprovals($0) }
        .task {
            StoreDataService.shared.attach(state: state)
            while !Task.isCancelled {
                StoreDataService.shared.loadControlRecords()
                do { try await Task.sleep(for: .seconds(8)) }
                catch is CancellationError { return }
                catch { return }
            }
        }
        .background(WindowAccessor())
        .ignoresSafeArea()
        .animation(.easeInOut(duration: 0.2), value: panelsStore.showSettings)
        .animation(.easeInOut(duration: 0.2), value: panelsStore.isSidebarVisible)
        .animation(.easeInOut(duration: 0.2), value: panelsStore.isRightPanelVisible)
        .frame(minWidth: 1080, minHeight: 660)
        .onReceive(NotificationCenter.default.publisher(for: UIIntent.toggleSidebar)) { _ in
            withAnimation(.easeInOut(duration: 0.2)) {
                panelsStore.isSidebarVisible.toggle()
            }
        }
        .onReceive(
            NotificationCenter.default.publisher(for: UIIntent.toggleRightPanel)
        ) { _ in
            withAnimation(.easeInOut(duration: 0.2)) {
                panelsStore.isRightPanelVisible.toggle()
            }
        }
        .onReceive(
            NotificationCenter.default.publisher(for: UIIntent.openModelPalette)
        ) { _ in
            panelsStore.showModelPalette = true
        }
        .onReceive(
            NotificationCenter.default.publisher(for: UIIntent.openSettings)
        ) { _ in
            withAnimation(.easeInOut(duration: 0.18)) {
                panelsStore.showSettings.toggle()
            }
        }
        .onReceive(
            NotificationCenter.default.publisher(for: UIIntent.newTask)
        ) { _ in
            state.newTask()
        }
        .onReceive(
            NotificationCenter.default.publisher(for: UIIntent.emergencyStop)
        ) { _ in
            state.emergencyStop()
        }
        .onKeyPress(.escape) {
            if threadsStore.threadPendingDeletion != nil {
                withAnimation(.easeInOut(duration: 0.18)) {
                    threadsStore.threadPendingDeletion = nil
                }
                return .handled
            }
            if panelsStore.showSettings {
                withAnimation(.easeInOut(duration: 0.18)) {
                    panelsStore.showSettings = false
                }
                return .handled
            }
            if runStore.runState.isRunning {
                state.emergencyStop()
                return .handled
            }
            return .ignored
        }
        .onReceive(NotificationCenter.default.publisher(for: UIIntent.openCommandPalette)) { _ in
            panelsStore.showCommandPalette = true
        }
        .sheet(isPresented: $panelsStore.showCommandPalette) {
            CommandPaletteView(state: state)
        }
        .sheet(isPresented: $panelsStore.showStats) {
            StatsSheet(state: state)
        }
        .sheet(isPresented: $panelsStore.showModelPalette) {
            ModelPaletteSheet(state: state)
        }
    }
}


// MARK: - Delete Task Confirmation Modal (Blurred Background)

private struct DeleteConfirmationModal: View {
    let thread: AgentThread
    let onConfirm: () -> Void
    let onCancel: () -> Void

    @State private var isDeleteHovered = false
    @State private var isCancelHovered = false

    var body: some View {
        ZStack {
            // Blurred backdrop
            Rectangle()
                .fill(.ultraThinMaterial)
                .overlay(Color.black.opacity(0.55))
                .ignoresSafeArea()
                .onTapGesture {
                    onCancel()
                }

            // Dialog Card
            VStack(spacing: 16) {
                HStack(spacing: 12) {
                    Image(systemName: "trash.fill")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Color(hex: 0xFF6B6B))
                        .frame(width: 36, height: 36)
                        .background(Color(hex: 0xFF6B6B).opacity(0.12))
                        .clipShape(Circle())

                    VStack(alignment: .leading, spacing: 3) {
                        Text("Delete Task?")
                            .font(.system(size: 14.5, weight: .semibold))
                            .foregroundStyle(Theme.textPrimary)

                        Text("This task and all its step traces will be permanently deleted.")
                            .font(.system(size: 11.5))
                            .foregroundStyle(Theme.textMuted)
                    }
                    Spacer()
                }

                // Task Preview
                HStack {
                    Image(systemName: "bubble.left.and.bubble.right")
                        .font(.system(size: 11))
                        .foregroundStyle(Theme.textMuted)
                    Text(thread.title.isEmpty ? "New task" : thread.title)
                        .font(.system(size: 11.5, design: .monospaced))
                        .foregroundStyle(Theme.textSecondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer()
                    if thread.entries.count > 0 {
                        Text("\(thread.entries.count) steps")
                            .font(.system(size: 10.5))
                            .foregroundStyle(Theme.textMuted.opacity(0.65))
                    }
                }
                .padding(.horizontal, 11)
                .padding(.vertical, 7)
                .background(Theme.canvas)
                .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 7, style: .continuous)
                        .stroke(Theme.borderOverlay, lineWidth: 1)
                )

                // Buttons
                HStack(spacing: 10) {
                    Button(action: onCancel) {
                        Text("Cancel")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(isCancelHovered ? Theme.textPrimary : Theme.textSecondary)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 7)
                            .background(isCancelHovered ? Color.white.opacity(0.1) : Color.white.opacity(0.05))
                            .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
                    }
                    .buttonStyle(.plain)
                    .pointingHand()
                    .onHover { isCancelHovered = $0 }

                    Button(action: onConfirm) {
                        Text("Delete")
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(Color.white)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 7)
                            .background(isDeleteHovered ? Color(hex: 0xEB4D4D) : Color(hex: 0xD63031))
                            .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
                    }
                    .buttonStyle(.plain)
                    .pointingHand()
                    .onHover { isDeleteHovered = $0 }
                }
            }
            .padding(18)
            .frame(width: 380)
            .background(Theme.card)
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(Theme.borderOverlay, lineWidth: 1)
            )
            .shadow(color: Color.black.opacity(0.45), radius: 24, y: 8)
        }
    }
}
