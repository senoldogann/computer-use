import SwiftUI

/// Left sidebar:
/// - Traffic-light safe header with workspace branding and collapse button
/// - Hero "+ New Task" button (Cmd+N) & subtle search field with focus border
/// - Mini Agent Status Strip (Ready/Running indicator, model pill, Trust toggle, Pending Approval alert)
/// - Filterable tasks list with status icons, elapsed time, step count, active accent pill
/// - Polished footer with Settings (Cmd+,), Quick Model Switcher (Cmd+K), and version
struct SidebarView: View {
    @ObservedObject var state: AppState
    @ObservedObject var threadsStore: ThreadsStore
    @ObservedObject var runStore: RunStore
    @ObservedObject var panelsStore: PanelsStore
    @ObservedObject var store = StoreDataService.shared

    init(state: AppState) {
        self.state = state
        self.threadsStore = state.threadsStore
        self.runStore = state.runStore
        self.panelsStore = state.panelsStore
    }

    @FocusState private var isSearchFocused: Bool
    @State private var isSearchHovered = false
    @State private var isNewTaskHovered = false
    @State private var isSettingsHovered = false
    @State private var isModelHovered = false

    var body: some View {
        VStack(spacing: 0) {
            CosmicSidebarHeader(panelsStore: panelsStore)

            VStack(spacing: 7) {
                // Primary "+ New Task" action
                newTaskButton

                // Search field
                searchBar
            }
            .padding(.horizontal, 11)
            .padding(.top, 4)
            .padding(.bottom, 9)

            // Tasks Section Header
            tasksHeader
                .padding(.horizontal, 14)
                .padding(.bottom, 6)

            // Thread List / Empty State
            ScrollView {
                LazyVStack(spacing: 2) {
                    if threadsStore.filteredThreads.isEmpty {
                        emptyThreadsView
                    } else {
                        ForEach(threadsStore.filteredThreads) { thread in
                            threadRow(for: thread)
                        }
                    }
                }
                .padding(.horizontal, 7)
            }

            Spacer(minLength: 0)

            // Footer
            sidebarFooter
        }
        .frame(width: 245)
        .background(GlassPanel(material: .sidebar, wash: Theme.sidebarWash))
        .overlay(alignment: .trailing) {
            Theme.borderOverlay.frame(width: 1)
        }
    }

    // MARK: - New Task Button

    private var newTaskButton: some View {
        Button {
            state.newTask()
        } label: {
            HStack(spacing: 6) {
                Image(systemName: "plus")
                    .font(.system(size: 12.5, weight: .semibold))
                    .foregroundStyle(Theme.textPrimary)

                Text("New Task")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(Theme.textPrimary)

                Spacer()

                Text("⌘N")
                    .font(.system(size: 11, weight: .medium, design: .monospaced))
                    .foregroundStyle(Theme.textMuted.opacity(0.8))
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(
                isNewTaskHovered
                    ? Color.white.opacity(0.08)
                    : Color.clear
            )
            .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .stroke(isNewTaskHovered ? Theme.borderOverlay : Color.clear, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .pointingHand()
        .onHover { isNewTaskHovered = $0 }
        .help("Start new task (Cmd+N)")
    }

    // MARK: - Search Bar

    private var searchBar: some View {
        HStack(spacing: 6) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 12))
                .foregroundStyle(Theme.textMuted)

            TextField("Search tasks...", text: $threadsStore.searchText)
                .textFieldStyle(.plain)
                .font(.system(size: 12.5))
                .foregroundStyle(Theme.textPrimary)
                .focused($isSearchFocused)

            if !threadsStore.searchText.isEmpty {
                Button {
                    threadsStore.searchText = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 11))
                        .foregroundStyle(Theme.textMuted)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .background(
            isSearchFocused
                ? Color.white.opacity(0.08)
                : (isSearchHovered ? Color.white.opacity(0.04) : Color.white.opacity(0.025))
        )
        .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .stroke(
                    (isSearchFocused || !state.searchText.isEmpty)
                        ? Theme.accent.opacity(0.6)
                        : (isSearchHovered ? Theme.borderOverlay : Color.clear),
                    lineWidth: 1
                )
        )
        .onHover { isSearchHovered = $0 }
    }

    // MARK: - Tasks Header

    private var tasksHeader: some View {
        HStack {
            Text("RECENT TASKS")
                .font(.system(size: 10.5, weight: .bold))
                .foregroundStyle(Theme.textMuted.opacity(0.65))
                .tracking(0.6)

            Spacer()

            if !threadsStore.threads.isEmpty {
                Text("\(threadsStore.filteredThreads.count)")
                    .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                    .foregroundStyle(Theme.textMuted)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1)
                    .background(Color.white.opacity(0.04))
                    .clipShape(Capsule())
            }
        }
    }

    // MARK: - Empty Threads View

    private var emptyThreadsView: some View {
        VStack(spacing: 6) {
            Image(systemName: threadsStore.searchText.isEmpty ? "sparkles" : "magnifyingglass")
                .font(.system(size: 16))
                .foregroundStyle(Theme.textMuted.opacity(0.35))
                .padding(.top, 24)

            Text(threadsStore.searchText.isEmpty ? "No recent tasks" : "No matches found")
                .font(.system(size: 12.5, weight: .medium))
                .foregroundStyle(Theme.textMuted)

            if !threadsStore.searchText.isEmpty {
                Button("Clear search") {
                    threadsStore.searchText = ""
                }
                .buttonStyle(.plain)
                .font(.system(size: 11.5))
                .foregroundStyle(Theme.accent)
                .padding(.top, 2)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 16)
    }

    // MARK: - Thread Row Builder

    @ViewBuilder
    private func threadRow(for thread: AgentThread) -> some View {
        let isSelected = (thread.id == threadsStore.selectedThreadID)
        let isRunning = (thread.id == runStore.runningThreadID)
        ThreadRow(
            thread: thread,
            isSelected: isSelected,
            isRunning: isRunning,
            onSelect: {
                state.selectThread(thread.id)
            },
            onDelete: {
                withAnimation(.easeInOut(duration: 0.18)) {
                    threadsStore.threadPendingDeletion = thread
                }
            }
        )
    }

    // MARK: - Footer

    private var sidebarFooter: some View {
        HStack(spacing: 6) {
            // Settings button
            Button {
                panelsStore.showSettings = true
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "gearshape")
                        .font(.system(size: 12.5))
                    Text("Settings")
                        .font(.system(size: 12.5, weight: .medium))
                }
                .foregroundStyle(isSettingsHovered ? Theme.textPrimary : Theme.textMuted)
                .padding(.horizontal, 7)
                .padding(.vertical, 5)
                .background(isSettingsHovered ? Color.white.opacity(0.06) : Color.clear)
                .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            }
            .buttonStyle(.plain)
            .pointingHand()
            .help("Settings (Cmd+,)")
            .onHover { isSettingsHovered = $0 }

            Spacer()

            // App version
            Text("v\(Theme.appVersion)")
                .font(.system(size: 10.5, design: .monospaced))
                .foregroundStyle(Theme.textMuted.opacity(0.45))
                .padding(.trailing, 4)
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 8)
    }
}

// MARK: - Thread Row

private struct ThreadRow: View {
    let thread: AgentThread
    let isSelected: Bool
    let isRunning: Bool
    let onSelect: () -> Void
    let onDelete: () -> Void

    @State private var isHovered = false

    var body: some View {
        Button(action: onSelect) {
            HStack(spacing: 7) {
                // Active Left Pill Indicator (Cursor / Apple style)
                RoundedRectangle(cornerRadius: 1.5, style: .continuous)
                    .fill(isSelected ? Theme.accent : Color.clear)
                    .frame(width: 2.5, height: 22)

                // Status icon
                statusIcon
                    .font(.system(size: 11.5))
                    .foregroundStyle(statusColor)
                    .frame(width: 15)

                VStack(alignment: .leading, spacing: 2.5) {
                    Text(thread.title.isEmpty ? "New task" : thread.title)
                        .font(.system(size: 13, weight: isSelected ? .medium : .regular))
                        .foregroundStyle(isSelected ? Theme.textPrimary : (isHovered ? Theme.textPrimary : Theme.textSecondary))
                        .lineLimit(1)
                        .truncationMode(.tail)

                    HStack(spacing: 4) {
                        Text(thread.elapsedLabel(at: Date()))
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(Theme.textMuted.opacity(0.65))

                        if thread.entries.count > 0 {
                            Text("·")
                                .font(.system(size: 9.5))
                                .foregroundStyle(Theme.textMuted.opacity(0.4))

                            Text("\(thread.entries.count) steps")
                                .font(.system(size: 10.5))
                                .foregroundStyle(Theme.textMuted.opacity(0.65))
                        }
                    }
                }

                Spacer(minLength: 4)

                // Delete button on hover
                if isHovered {
                    Button(action: onDelete) {
                        Image(systemName: "trash")
                            .font(.system(size: 9.5))
                            .foregroundStyle(Theme.textMuted)
                            .padding(3.5)
                            .background(Color.white.opacity(0.08))
                            .clipShape(RoundedRectangle(cornerRadius: 4, style: .continuous))
                    }
                    .buttonStyle(.plain)
                    .help("Delete task")
                }
            }
            .padding(.trailing, 7)
            .padding(.vertical, 5)
            .background(
                isSelected
                    ? Color.white.opacity(0.065)
                    : (isHovered ? Color.white.opacity(0.035) : Color.clear)
            )
            .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .stroke(isSelected ? Theme.borderOverlay : Color.clear, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .pointingHand()
        .onHover { isHovered = $0 }
    }

    private var statusIcon: some View {
        Group {
            switch thread.status {
            case .working:
                Image(systemName: "bolt.fill")
            case .done:
                Image(systemName: "checkmark.circle.fill")
            case .failed:
                Image(systemName: "exclamationmark.circle.fill")
            case .idle, .draft, .parked:
                Image(systemName: "circle")
            }
        }
    }

    private var statusColor: Color {
        switch thread.status {
        case .working: return Theme.accent
        case .done: return Theme.success
        case .failed: return Theme.danger
        case .idle, .draft, .parked: return Theme.textMuted.opacity(0.45)
        }
    }
}
