import SwiftUI

/// Center stage:
/// - Top bar: workspace breadcrumb · task title · status · right-panel toggle (no bottom line)
/// - Empty: vertically centered hero + composer + chips
/// - Active: timeline with a glass composer docked at the bottom and task rail (-)
struct CenterStageView: View {
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

    private var truncatedTaskTitle: String {
        guard let title = threadsStore.activeThread?.title, !title.isEmpty else {
            return "New task"
        }
        if title.count > 20 {
            return String(title.prefix(20)) + ".."
        }
        return title
    }

    var body: some View {
        VStack(spacing: 0) {
            topBar
            ResumeFirstCard(state: state)

            if threadsStore.isEmptyState {
                emptyState
            } else {
                taskThreadView
            }
        }
        .background(Color.clear)
    }

    /// Single top row shared by the whole window chrome:
    /// traffic-light clearance & left-sidebar toggle on the left, breadcrumbs in the middle,
    /// and right-panel toggle on the far right.
    private var topBar: some View {
        HStack(spacing: 8) {
            if !panelsStore.isSidebarVisible {
                // Traffic-light clearance when sidebar is collapsed
                Spacer().frame(width: Theme.trafficLightClearance)

                // Left sidebar re-open button (stays on the LEFT where the sidebar lives)
                NativeTitleBarButton(
                    iconName: "sidebar.left",
                    isActive: false,
                    tooltip: "Show sidebar (Cmd+B)"
                ) {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        panelsStore.isSidebarVisible = true
                    }
                }
                .frame(width: 28, height: 28)
            }

            Text(Theme.workspaceName)
                .font(.system(size: 12.5, weight: .semibold))
                .foregroundStyle(Theme.textMuted)

            Image(systemName: "chevron.right")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(Theme.textMuted.opacity(0.6))

            Text(truncatedTaskTitle)
                .font(.system(size: 12.5))
                .foregroundStyle(Theme.textPrimary)
                .fontWeight(.medium)
                .lineLimit(1)

            Spacer()

            if runStore.isRunning && runStore.runningThreadID == threadsStore.selectedThreadID {
                Text("Level \(state.autonomyLevel.cliLevel) · Trust \(state.trustMode ? "ON" : "OFF") · Verify \(state.verifyEnabled ? "ON" : "OFF")")
                    .font(.system(size: 9, design: .monospaced)).foregroundStyle(Theme.accent)
                    .padding(.horizontal, 7).padding(.vertical, 3).background(Theme.cardElevated).clipShape(Capsule())
            }

            // Right panel toggle (live viewport)
            NativeTitleBarButton(
                iconName: "sidebar.right",
                isActive: panelsStore.isRightPanelVisible,
                tooltip: "Toggle live viewport (Cmd+])"
            ) {
                withAnimation(.easeInOut(duration: 0.2)) {
                    panelsStore.isRightPanelVisible.toggle()
                }
            }
            .frame(width: 28, height: 28)
        }
        .font(.system(size: 12.5))
        .padding(.horizontal, 12)
        .frame(height: Theme.titleRowHeight)
        .padding(.top, Theme.titleRowTopPadding)
        .background(Color.clear)
    }

    private var emptyState: some View {
        GeometryReader { geo in
            ScrollView {
                VStack(spacing: 0) {
                    Spacer(minLength: 24)

                    VStack(spacing: 24) {
                        VStack(spacing: 12) {
                            Text("What should we build in computeruse?")
                                .font(.system(size: 30, weight: .semibold, design: .rounded))
                                .foregroundStyle(Theme.textPrimary)
                            Text("Agentic macOS Actuation IDE — direct command, step-by-step execution.")
                                .font(.system(size: 14))
                                .foregroundStyle(Theme.textMuted)
                        }
                        .multilineTextAlignment(.center)

                        ComposerView(state: state, compact: false)

                        HStack(spacing: 8) {
                            ForEach(state.exampleChips, id: \.self) { chip in
                                ExampleChipButton(chip: chip) {
                                    state.applyChip(chip)
                                }
                            }
                        }
                    }
                    .frame(maxWidth: 680)

                    Spacer(minLength: 48)
                }
                .frame(minWidth: geo.size.width, minHeight: geo.size.height)
            }
        }
    }

    private var taskThreadView: some View {
        ZStack(alignment: .bottom) {
            ScrollViewReader { proxy in
                ZStack(alignment: .leading) {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 0) {
                            if let thread = threadsStore.activeThread {
                                TimelineView(
                                    state: state,
                                    entries: thread.entries,
                                    status: thread.status
                                )
                            }
                            Color.clear
                                .frame(height: 140)
                                .id("timeline-bottom-spacer")
                        }
                        // Centered reading column: 780pt max width with
                        // side margins, like a chat surface — not full-bleed.
                        .frame(maxWidth: 780)
                        .frame(maxWidth: .infinity, alignment: .center)
                        .padding(.horizontal, 28)
                        .padding(.top, 16)
                    }
                    .onChange(of: threadsStore.scrollTargetID) { _, targetID in
                        guard let targetID else { return }
                        withAnimation(.easeInOut(duration: 0.25)) {
                            proxy.scrollTo("entry-\(targetID)", anchor: .center)
                        }
                        threadsStore.scrollTargetID = nil
                    }
                    .onChange(of: threadsStore.activeThread?.entries.count) { _, count in
                        guard let count, count > 0 else { return }
                        withAnimation(.easeInOut(duration: 0.2)) {
                            proxy.scrollTo("timeline-bottom-spacer", anchor: .bottom)
                        }
                    }

                    // Left Task Rail: hover-reveal horizontal dashes for every user goal
                    if let thread = threadsStore.activeThread, thread.entries.contains(where: { $0.kind == .userMessage }) {
                        TaskRailView(entries: thread.entries, proxy: proxy)
                            .padding(.leading, 8)
                    }
                }
            }

            // Glass composer docked at the bottom of the active task
            ComposerView(state: state, compact: true)
                .padding(.horizontal, 24)
                .padding(.bottom, 16)
        }
    }
}

// MARK: - Task Rail View (Subtle Hover Dash Strip)

private struct TaskRailView: View {
    let entries: [TimelineEntry]
    let proxy: ScrollViewProxy

    @State private var isStripHovered = false

    private var userEntries: [(index: Int, entry: TimelineEntry)] {
        Array(entries.enumerated())
            .filter { $0.element.kind == .userMessage }
            .enumerated()
            .map { ($0.offset + 1, $0.element.element) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(userEntries, id: \.entry.id) { item in
                TaskRailDash(
                    index: item.index,
                    entry: item.entry,
                    isActive: item.entry.id == entries.last(where: { $0.kind == .userMessage })?.id
                ) {
                    withAnimation(.easeInOut(duration: 0.25)) {
                        proxy.scrollTo("entry-\(item.entry.id)", anchor: .top)
                    }
                }
            }
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 4)
        .background(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(isStripHovered ? Theme.cardElevated.opacity(0.85) : Color.clear)
        )
        .onHover { isStripHovered = $0 }
        .animation(.easeInOut(duration: 0.15), value: isStripHovered)
    }
}

private struct TaskRailDash: View {
    let index: Int
    let entry: TimelineEntry
    let isActive: Bool
    let action: () -> Void

    @State private var isHovered = false

    var body: some View {
        Button(action: action) {
            RoundedRectangle(cornerRadius: 1, style: .continuous)
                .fill(fillColor)
                .frame(width: isHovered ? 24 : (isActive ? 18 : 8), height: 2)
                .padding(.vertical, 2.5)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .pointingHand()
        .onHover { isHovered = $0 }
        .help("Task \(index): \(String(entry.title.prefix(60)))")
        .animation(.easeInOut(duration: 0.15), value: isHovered)
        .animation(.easeInOut(duration: 0.15), value: isActive)
    }

    private var fillColor: Color {
        if isHovered {
            return Theme.textPrimary
        }
        if isActive {
            return Theme.accent
        }
        return Theme.textMuted.opacity(0.35)
    }
}

private struct ExampleChipButton: View {
    let chip: String
    let action: () -> Void

    @State private var isHovered = false

    var body: some View {
        Button(action: action) {
            Text(chip)
                .font(.system(size: 12))
                .foregroundStyle(isHovered ? Theme.textPrimary : Theme.textMuted)
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .background(isHovered ? Theme.cardElevated : Theme.card)
                .clipShape(Capsule())
                .overlay(
                    Capsule()
                        .stroke(isHovered ? Theme.borderActive : Theme.borderOverlay, lineWidth: 1)
                )
        }
        .buttonStyle(.plain)
        .pointingHand(radius: 12)
        .onHover { isHovered = $0 }
    }
}
