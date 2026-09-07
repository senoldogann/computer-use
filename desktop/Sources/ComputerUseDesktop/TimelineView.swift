import SwiftUI

/// Timeline:
/// 1. User message bubble on the right + copy icon (text matches token usage color).
/// 2. Clean step rows with icon + action/thought label (no line dashes).
/// 3. Clear SF icons + badge (Desktop/Tool/Skill).
/// 4. Soft shimmer animation on active live step.
/// 5. Task footer: date, copy button, step count, and elapsed time.
/// 6. Multi-task divider between subsequent prompts in the same session.
struct TimelineView: View {
    @ObservedObject var state: AppState
    @ObservedObject var threadsStore: ThreadsStore
    @ObservedObject var runStore: RunStore
    let entries: [TimelineEntry]
    let status: ThreadStatus

    init(state: AppState, entries: [TimelineEntry], status: ThreadStatus) {
        self.state = state
        self.threadsStore = state.threadsStore
        self.runStore = state.runStore
        self.entries = entries
        self.status = status
    }

    private var visibleEntries: [TimelineEntry] {
        entries.filter { entry in
            entry.kind != .console || (!threadsStore.hideConsole && (threadsStore.consoleFilter.isEmpty || entry.detail.localizedCaseInsensitiveContains(threadsStore.consoleFilter)))
        }
    }

    private var actionCount: Int {
        entries.filter { $0.kind == .action }.count
    }

    private var commandCount: Int {
        entries.filter { $0.kind == .action || $0.kind == .console }.count
    }

    private var errorCount: Int {
        entries.filter { $0.isFailure }.count
    }

    private var isRunning: Bool {
        status == .working && runStore.runState.isRunning
    }

    private var lastAgentEntryID: UUID? {
        entries.last(where: { $0.kind != .userMessage })?.id
    }

    @State private var isOutputCopied: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(visibleEntries.enumerated()), id: \.element.id) { index, entry in
                let isSubsequent = entry.kind == .userMessage && index > 0
                TimelineRow(
                    state: state,
                    threadsStore: threadsStore,
                    entry: entry,
                    isLiveLast: isRunning && entry.id == lastAgentEntryID,
                    isSubsequentTask: isSubsequent
                )
                .id("entry-\(entry.id)")
            }

            if isRunning {
                progressLine
            } else if !entries.isEmpty && entries.contains(where: { $0.kind != .userMessage }) {
                runSummaryCard
            }
        }
    }

    // MARK: - Live Progress Line

    private var progressLine: some View {
        HStack(spacing: 8) {
            ProgressView()
                .controlSize(.small)
                .tint(Theme.textMuted)
            if let started = runStore.runStartedAt {
                Text("Running ")
                    + Text(started, style: .timer)
                    + Text(" · \(commandCount) steps")
            } else {
                Text("Running · \(commandCount) steps")
            }
            Spacer(minLength: 0)
        }
        .font(.system(size: 11.5))
        .foregroundStyle(Theme.textMuted.opacity(0.85))
        .padding(.vertical, 4)
        .padding(.leading, 6)
    }

    // MARK: - Run Summary Card

    /// One structured block shown when the run finishes: what it did (tools /
    /// steps / duration / errors), what it cost (tokens · calls · cost) and
    /// when it finished — with the single copy action.
    private var runSummaryCard: some View {
        HStack(alignment: .center, spacing: 12) {
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 15))
                .foregroundStyle(Theme.success)

            VStack(alignment: .leading, spacing: 3) {
                Text(summaryText)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(Theme.textPrimary)

                if let telemetry = threadsStore.activeThread?.telemetry {
                    Text(telemetry.label)
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(Theme.textSecondary)
                }
            }

            Spacer(minLength: 8)

            Text(finishTimeString)
                .font(.system(size: 10.5, design: .monospaced))
                .foregroundStyle(Theme.textMuted.opacity(0.85))

            Button {
                copyAllTranscript()
            } label: {
                Image(systemName: isOutputCopied ? "checkmark" : "doc.on.doc")
                    .font(.system(size: 11))
                    .foregroundStyle(isOutputCopied ? Theme.success : Theme.textMuted.opacity(0.85))
                    .frame(width: 22, height: 22)
            }
            .buttonStyle(.plain)
            .hoverPointer(radius: 4)
            .help("Copy entire response to clipboard")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(Theme.card)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(Theme.borderOverlay, lineWidth: 1)
        )
    }

    private var summaryText: String {
        var text =
            "\(max(actionCount, 1)) tools, \(max(commandCount, 1)) steps completed"
        if errorCount > 0 {
            text += ", \(errorCount) errors"
        }
        if !elapsedString.isEmpty {
            text += " · \(elapsedString)"
        }
        return text
    }

    private var finishTimeString: String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        if let thread = threadsStore.activeThread, let finished = thread.finishedAt {
            return formatter.string(from: finished)
        }
        return formatter.string(from: Date())
    }

    private var elapsedString: String {
        guard let thread = threadsStore.activeThread else { return "" }
        return thread.elapsedLabel(at: Date())
    }

    private func copyAllTranscript() {
        let allText = entries.map { entry in
            if entry.kind == .userMessage {
                return "User: \(entry.title)"
            } else {
                return "[\(entry.title)] \(entry.detail)"
            }
        }.joined(separator: "\n")

        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(allText, forType: .string)

        isOutputCopied = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
            isOutputCopied = false
        }
    }
}

// MARK: - Console Menu (top-right of the window, outside the chat)

/// Top-bar "⋯" menu for console management: hide/show, filter field, copy log
/// and jump-to-first-error. Rendered only when the active thread has console
/// output (see CenterStageView.topBar).
struct ConsoleMenuButton: View {
    @ObservedObject var threadsStore: ThreadsStore
    let entries: [TimelineEntry]
    @State private var showFilter: Bool = false

    var body: some View {
        HStack(spacing: 8) {
            if showFilter {
                TextField("Filter console", text: $threadsStore.consoleFilter)
                    .textFieldStyle(.plain)
                    .font(.system(size: 10.5))
                    .foregroundStyle(Theme.textSecondary)
                    .frame(width: 130)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 3)
                    .background(Theme.card)
                    .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: 5, style: .continuous)
                            .stroke(Theme.borderOverlay, lineWidth: 1)
                    )
            }

            Menu {
                Button(threadsStore.hideConsole ? "Show Console" : "Hide Console") {
                    threadsStore.hideConsole.toggle()
                }
                Button(showFilter ? "Hide Filter" : "Filter Console…") {
                    showFilter.toggle()
                }
                Button("Copy Log") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(
                        entries.filter { $0.kind == .console }.map(\.detail).joined(separator: "\n"),
                        forType: .string
                    )
                }
                Button("First Error") {
                    threadsStore.hideConsole = false
                    threadsStore.consoleFilter = ""
                    threadsStore.scrollTargetID = entries.first(where: \.isFailure)?.id
                }
                .disabled(!entries.contains(where: \.isFailure))
            } label: {
                Image(systemName: "ellipsis")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Theme.textSecondary)
                    .frame(width: 26, height: 24)
                    .contentShape(Rectangle())
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .help("Console options")
        }
    }
}

// MARK: - Row Selector

private struct TimelineRow: View {
    @ObservedObject var state: AppState
    @ObservedObject var threadsStore: ThreadsStore
    let entry: TimelineEntry
    let isLiveLast: Bool
    let isSubsequentTask: Bool

    init(state: AppState, threadsStore: ThreadsStore, entry: TimelineEntry, isLiveLast: Bool, isSubsequentTask: Bool) {
        self.state = state
        self.threadsStore = threadsStore
        self.entry = entry
        self.isLiveLast = isLiveLast
        self.isSubsequentTask = isSubsequentTask
    }

    var body: some View {
        VStack(spacing: 0) {
            if isSubsequentTask {
                HStack(spacing: 12) {
                    Rectangle()
                        .fill(Theme.borderOverlay)
                        .frame(height: 1)
                    Text("New Task")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(Theme.textMuted.opacity(0.65))
                    Rectangle()
                        .fill(Theme.borderOverlay)
                        .frame(height: 1)
                }
                .padding(.vertical, 16)
            }

            switch entry.kind {
            case .userMessage:
                UserMessageBubble(text: entry.title, createdAt: entry.createdAt)
                    .padding(.bottom, 6)

            case .approval:
                if let id = entry.approvalID { ApprovalCard(requestID: id) }
            case .plan:
                PlanChecklistView(state: state, entry: entry)
            case .console:
                StepLineView(threadsStore: threadsStore, entry: entry, isLiveLast: isLiveLast)
            case .thinking, .action:
                StepLineView(
                    threadsStore: threadsStore,
                    entry: entry,
                    isLiveLast: isLiveLast
                )
            }
        }
    }
}

// MARK: - User Message Bubble

private struct UserMessageBubble: View {
    let text: String
    let createdAt: Date

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            HStack(spacing: 0) {
                Spacer(minLength: 40)

                Text(text)
                    .font(.system(size: 13.5, weight: .regular))
                    .foregroundStyle(Theme.textPrimary.opacity(0.95))
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(Theme.card)
                    .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: 14, style: .continuous)
                            .stroke(Theme.borderOverlay, lineWidth: 1)
                    )
            }

            // One copy action lives in the finished footer; per-message copy
            // buttons only add noise.
            Text(formatTime(createdAt))
                .font(.system(size: 10.5))
                .foregroundStyle(Theme.textMuted.opacity(0.55))
                .padding(.trailing, 4)
        }
    }

    private func formatTime(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm"
        return formatter.string(from: date)
    }
}

// MARK: - Step Line View (Refined Icon + Preview Text + Clean Borderless Hover)

private struct StepLineView: View {
    @ObservedObject var threadsStore: ThreadsStore
    let entry: TimelineEntry
    let isLiveLast: Bool

    init(threadsStore: ThreadsStore, entry: TimelineEntry, isLiveLast: Bool) {
        self.threadsStore = threadsStore
        self.entry = entry
        self.isLiveLast = isLiveLast
    }

    @State private var isHovered = false

    private var iconInfo: (name: String, color: Color) {
        if entry.isFailure {
            return ("xmark.circle.fill", Theme.danger)
        }
        switch entry.kind {
        case .thinking:
            return ("brain", Theme.textMuted.opacity(0.7))
        case .approval:
            return ("hand.raised", Theme.accent)
        case .plan:
            return ("list.bullet.clipboard", Theme.accent)
        case .console:
            return ("terminal", Theme.textMuted.opacity(0.7))
        case .userMessage:
            return ("person.fill", Theme.accent)
        case .action:
            let action = entry.title.lowercased()
            let detail = entry.detail.lowercased()
            let combined = action + " " + detail

            if combined.contains("click") || combined.contains("mouse_down") {
                return ("cursorarrow.rays", Theme.accent)
            }
            if combined.contains("move") || combined.contains("mouse_move") {
                return ("cursorarrow", Theme.accent)
            }
            if combined.contains("type") || combined.contains("key") {
                return ("keyboard", Theme.accent)
            }
            if combined.contains("scroll") {
                return ("arrow.up.and.down", Theme.accent)
            }
            if combined.contains("bash") || combined.contains("terminal") || combined.contains("shell") {
                return ("terminal", Theme.textPrimary)
            }
            if combined.contains("web") || combined.contains("search") || combined.contains("http") {
                return ("globe", Theme.accent)
            }
            if combined.contains("verify") || combined.contains("check") {
                return ("checkmark.circle", Theme.success)
            }
            return ("bolt.fill", Theme.textPrimary)
        }
    }

    private var previewText: String {
        switch entry.kind {
        case .thinking:
            return entry.detail.isEmpty ? "Thinking..." : entry.detail
        case .approval:
            return entry.title
        case .plan:
            return entry.title
        case .console:
            return entry.detail.trimmingCharacters(in: .whitespacesAndNewlines)
        case .action, .userMessage:
            if !entry.detail.isEmpty {
                let trimmed = entry.detail.trimmingCharacters(in: .whitespacesAndNewlines)
                if !trimmed.isEmpty && trimmed.count < 120 {
                    return trimmed
                }
            }
            return entry.title
        }
    }

    private var routeBadge: (text: String, color: Color, bg: Color)? {
        guard entry.kind == .action, !entry.isFailure else { return nil }
        let low = entry.detail.lowercased()
        if low.contains("physical") || low.contains("desktop") || low.contains("gui") {
            return ("Desktop", Theme.textSecondary, Theme.cardElevated)
        }
        if low.contains("internal_skill") || low.contains("skill") {
            return ("Skill", Theme.textMuted, Theme.cardElevated)
        }
        if low.contains("internal_tool") || low.contains("tool") {
            return ("Tool", Theme.textMuted, Theme.cardElevated)
        }
        if low.contains("internal_wait") || low.contains("wait") {
            return ("Wait", Theme.textMuted, Theme.cardElevated)
        }
        return nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                guard let threadID = threadsStore.selectedThreadID else { return }
                threadsStore.setEntryExpanded(
                    threadID: threadID,
                    entryID: entry.id,
                    expanded: !entry.isExpanded
                )
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: iconInfo.name)
                        .font(.system(size: 11.5, weight: .regular))
                        .foregroundStyle(iconInfo.color)
                        .frame(width: 14)

                    Text(previewText)
                        .font(.system(size: 12, design: .monospaced))
                        .foregroundStyle(entry.isFailure ? Theme.danger : Theme.textMuted.opacity(0.85))
                        .lineLimit(1)
                        .truncationMode(.middle)

                    if let badge = routeBadge {
                        Text(badge.text)
                            .font(.system(size: 9.5, weight: .medium))
                            .foregroundStyle(badge.color)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1.5)
                            .background(badge.bg)
                            .clipShape(RoundedRectangle(cornerRadius: 3.5, style: .continuous))
                    }

                    Spacer(minLength: 0)

                    if !entry.detail.isEmpty && entry.detail != previewText {
                        Image(systemName: "chevron.right")
                            .font(.system(size: 9, weight: .semibold))
                            .foregroundStyle(Theme.textMuted.opacity(0.4))
                            .rotationEffect(.degrees(entry.isExpanded ? 90 : 0))
                    }
                }
                .padding(.vertical, 4.5)
                .padding(.horizontal, 6)
                .background(
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .fill(isHovered ? Theme.cardElevated : Color.clear)
                )
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .onHover { isHovered = $0 }
            .shimmerEffect(isActive: isLiveLast)

            if entry.isExpanded && !entry.detail.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ScrollView(.horizontal, showsIndicators: false) {
                        Text(entry.detail)
                            .font(.system(size: 11.5, design: .monospaced))
                            .foregroundStyle(entry.isFailure ? Theme.danger.opacity(0.9) : Theme.textMuted)
                            .textSelection(.enabled)
                    }
                }
                .padding(.leading, 28)
                .padding(.trailing, 8)
                .padding(.bottom, 6)
            }
        }
    }
}

// MARK: - Shimmer Glow

private struct ShimmerModifier: ViewModifier {
    let isActive: Bool
    @State private var phase: CGFloat = 0

    func body(content: Content) -> some View {
        if isActive {
            content
                .overlay(
                    GeometryReader { geo in
                        LinearGradient(
                            colors: [
                                Color.clear,
                                Color.white.opacity(0.85),
                                Color.clear,
                            ],
                            startPoint: .leading,
                            endPoint: .trailing
                        )
                        .frame(width: geo.size.width * 0.6)
                        .offset(x: phase * geo.size.width * 1.5 - geo.size.width * 0.3)
                    }
                    .allowsHitTesting(false)
                )
                .mask(content)
                .onAppear {
                    withAnimation(
                        .linear(duration: 1.8)
                        .repeatForever(autoreverses: false)
                    ) {
                        phase = 1.0
                    }
                }
        } else {
            content
        }
    }
}

private extension View {
    func shimmerEffect(isActive: Bool) -> some View {
        modifier(ShimmerModifier(isActive: isActive))
    }
}
