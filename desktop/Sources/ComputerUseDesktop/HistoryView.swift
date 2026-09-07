import SwiftUI

/// Task History view:
/// - Real episode history loaded from ~/.computeruse/episodes/*.json
/// - Search filter & status filter (All, Success, Failed)
/// - Detailed execution cards with status, target app, action breakdown, steps, timestamps
/// - Expandable retrospective / failure diagnosis
/// - 1-click [ Re-run ] button to re-execute in Live Agent
struct HistoryView: View {
    @ObservedObject var state: AppState
    @ObservedObject var store = StoreDataService.shared

    @State private var searchText = ""
    @State private var filterOutcome: EpisodeOutcome? = nil
    @State private var expandedEpisodeIDs: Set<String> = []
    @State private var showClearConfirm = false
    @State private var episodePendingDeletion: StoreEpisodeItem? = nil

    private var filteredEpisodes: [StoreEpisodeItem] {
        let q = searchText.trimmingCharacters(in: .whitespaces)
        return store.episodes.filter { ep in
            let matchesOutcome = (filterOutcome == nil) || (ep.outcome == filterOutcome)
            if !matchesOutcome { return false }
            if q.isEmpty { return true }
            return ep.goal.localizedCaseInsensitiveContains(q)
                || ep.app.localizedCaseInsensitiveContains(q)
        }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                headerBar

                searchAndFilterBar

                if store.episodes.isEmpty {
                    emptyHistoryState
                } else {
                    episodesList
                }

                Spacer(minLength: 40)
            }
            .padding(.horizontal, 32)
            .padding(.top, 20)
            .padding(.bottom, 40)
            .frame(maxWidth: 880, alignment: .leading)
        }
        .background(Color.clear)
        .onAppear {
            store.loadEpisodes()
        }
        .alert("Clear Task History", isPresented: $showClearConfirm) {
            Button("Cancel", role: .cancel) {}
            Button("Clear", role: .destructive) {
                store.clearHistory()
            }
        } message: {
            Text("All saved task execution history will be cleared. This action cannot be undone.")
        }
        .alert("Delete Task Entry", isPresented: Binding(
            get: { episodePendingDeletion != nil },
            set: { if !$0 { episodePendingDeletion = nil } }
        )) {
            Button("Cancel", role: .cancel) {
                episodePendingDeletion = nil
            }
            Button("Delete", role: .destructive) {
                if let ep = episodePendingDeletion {
                    store.deleteEpisode(id: ep.id)
                    episodePendingDeletion = nil
                }
            }
        } message: {
            if let ep = episodePendingDeletion {
                Text("Are you sure you want to delete this task from history?\n\n\"\(ep.goal)\"")
            }
        }
    }

    // MARK: - Header Bar

    private var headerBar: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Image(systemName: "clock.arrow.circlepath")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Theme.accent)
                    Text("Task History")
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(Theme.textPrimary)

                    Text("\(store.totalTasks) Tasks")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Theme.textMuted)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Theme.card)
                        .clipShape(Capsule())
                }

                Text("Agent task history, execution steps, and physical action traces")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textMuted)
            }

            Spacer()

            HStack(spacing: 8) {
                if !store.episodes.isEmpty {
                    Button {
                        showClearConfirm = true
                    } label: {
                        Text("Clear All")
                            .font(.system(size: 11.5, weight: .medium))
                            .foregroundStyle(Theme.danger.opacity(0.85))
                            .padding(.horizontal, 10)
                            .padding(.vertical, 6)
                            .background(Theme.danger.opacity(0.10))
                            .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                    }
                    .buttonStyle(.plain)
                    .hoverPointer(bg: Theme.danger.opacity(0.18), radius: 6)
                }

                Button {
                    store.loadEpisodes()
                } label: {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(Theme.textMuted)
                        .frame(width: 28, height: 28)
                        .background(Theme.card)
                        .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 6, style: .continuous)
                                .stroke(Theme.borderOverlay, lineWidth: 1)
                        )
                }
                .buttonStyle(.plain)
                .hoverPointer(bg: Theme.cardElevated, radius: 6)
                .help("Reload history")
            }
        }
    }

    // MARK: - Search & Filters

    private var searchAndFilterBar: some View {
        HStack(spacing: 12) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textMuted)

                TextField("Search task history...", text: $searchText)
                    .textFieldStyle(.plain)
                    .font(.system(size: 12.5))
                    .foregroundStyle(Theme.textPrimary)

                if !searchText.isEmpty {
                    Button {
                        searchText = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 12))
                            .foregroundStyle(Theme.textMuted)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
            .background(Theme.card)
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(Theme.borderOverlay, lineWidth: 1)
            )

            // Status Filter buttons
            HStack(spacing: 6) {
                filterButton(title: "All", target: nil)
                filterButton(title: "Success", target: .success)
                filterButton(title: "Failed", target: .failure)
            }
        }
    }

    private func filterButton(title: String, target: EpisodeOutcome?) -> some View {
        let isSelected = filterOutcome == target
        return Button {
            filterOutcome = target
        } label: {
            Text(title)
                .font(.system(size: 11.5, weight: isSelected ? .semibold : .regular))
                .foregroundStyle(isSelected ? Theme.textPrimary : Theme.textMuted)
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .background(isSelected ? Theme.cardElevated : Theme.card)
                .clipShape(Capsule())
                .overlay(
                    Capsule()
                        .stroke(isSelected ? Theme.borderActive : Color.clear, lineWidth: 1)
                )
        }
        .buttonStyle(.plain)
        .hoverPointer(radius: 12)
    }

    // MARK: - Episodes List

    private var episodesList: some View {
        LazyVStack(spacing: 10) {
            ForEach(filteredEpisodes) { episode in
                episodeCard(episode)
            }
        }
    }

    private func episodeCard(_ episode: StoreEpisodeItem) -> some View {
        let isExpanded = expandedEpisodeIDs.contains(episode.id)

        return VStack(alignment: .leading, spacing: 10) {
            // Header: Goal + Re-run button
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(episode.goal)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(Theme.textPrimary)
                        .lineLimit(isExpanded ? nil : 2)
                        .fixedSize(horizontal: false, vertical: isExpanded)

                    // Meta row
                    HStack(spacing: 8) {
                        statusBadge(episode.outcome)

                        if episode.app != "macOS" && !episode.app.isEmpty {
                            Text(episode.app)
                                .font(.system(size: 10.5, weight: .medium))
                                .foregroundStyle(Theme.textSecondary)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(Theme.cardElevated)
                                .clipShape(Capsule())
                                .overlay(Capsule().stroke(Theme.borderOverlay, lineWidth: 1))
                        }

                        Text("\(episode.stepsCount) steps")
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(Theme.textMuted)

                        Text("·")
                            .foregroundStyle(Theme.textMuted.opacity(0.4))

                        Text(episode.relativeTime)
                            .font(.system(size: 11))
                            .foregroundStyle(Theme.textMuted.opacity(0.8))
                    }
                }

                Spacer()

                HStack(spacing: 6) {
                    // Re-run Button
                    Button {
                        rerunEpisode(episode)
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: "arrow.clockwise")
                                .font(.system(size: 10.5, weight: .semibold))
                            Text("Re-run")
                                .font(.system(size: 11.5, weight: .semibold))
                        }
                        .foregroundStyle(Theme.textPrimary)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(Theme.card)
                        .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 6, style: .continuous)
                                .stroke(Theme.borderOverlay, lineWidth: 1)
                        )
                    }
                    .buttonStyle(.plain)
                    .hoverPointer(bg: Theme.cardElevated, radius: 6)
                    .help("Re-run this task on Live Agent")

                    // Delete Single Episode Button
                    Button {
                        episodePendingDeletion = episode
                    } label: {
                        Image(systemName: "trash")
                            .font(.system(size: 11))
                            .foregroundStyle(Theme.textMuted)
                            .frame(width: 26, height: 26)
                            .background(Theme.card)
                            .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                            .overlay(
                                RoundedRectangle(cornerRadius: 6, style: .continuous)
                                    .stroke(Theme.borderOverlay, lineWidth: 1)
                            )
                    }
                    .buttonStyle(.plain)
                    .hoverPointer(bg: Theme.danger.opacity(0.18), radius: 6)
                    .help("Delete this task from history")
                }
            }

            // Physical Actions breakdown pill row
            HStack(spacing: 6) {
                if episode.actions.clicks > 0 {
                    actionPill(icon: "cursorarrow.click", label: "\(episode.actions.clicks) clicks")
                }
                if episode.actions.types > 0 {
                    actionPill(icon: "keyboard", label: "\(episode.actions.types) types")
                }
                if episode.actions.pastes > 0 {
                    actionPill(icon: "doc.on.clipboard", label: "\(episode.actions.pastes) pastes")
                }
                if episode.actions.hotkeys > 0 {
                    actionPill(icon: "command", label: "\(episode.actions.hotkeys) hotkeys")
                }
                if episode.actions.apps > 0 {
                    actionPill(icon: "macwindow", label: "\(episode.actions.apps) apps")
                }

                Spacer()

                if episode.retrospective != nil || !episode.stepTypes.isEmpty {
                    Button {
                        toggleExpanded(episode.id)
                    } label: {
                        HStack(spacing: 3) {
                            Text(isExpanded ? "Hide Details" : "Details")
                                .font(.system(size: 10.5, weight: .medium))
                            Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                                .font(.system(size: 9))
                        }
                        .foregroundStyle(Theme.textMuted)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 3)
                    }
                    .buttonStyle(.plain)
                    .hoverPointer(radius: 4)
                }
            }

            // Expanded failure diagnosis / step details
            if isExpanded {
                VStack(alignment: .leading, spacing: 8) {
                    if let retro = episode.retrospective, !retro.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Failure / Diagnostic Summary:")
                                .font(.system(size: 10.5, weight: .semibold))
                                .foregroundStyle(Theme.textPrimary)
                            Text(retro)
                                .font(.system(size: 11, design: .monospaced))
                                .foregroundStyle(Theme.textSecondary)
                                .lineLimit(6)
                        }
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Theme.cardElevated)
                        .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 6, style: .continuous)
                                .stroke(Theme.borderOverlay, lineWidth: 1)
                        )
                    }

                    if !episode.stepTypes.isEmpty {
                        Text("Step Sequence: \(episode.stepTypes.prefix(10).joined(separator: " → "))\(episode.stepTypes.count > 10 ? "..." : "")")
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(Theme.textMuted.opacity(0.8))
                    }
                }
                .padding(.top, 4)
            }
        }
        .padding(14)
        .background(Theme.card)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(Theme.borderOverlay, lineWidth: 1)
        )
    }

    private func statusBadge(_ outcome: EpisodeOutcome) -> some View {
        HStack(spacing: 4) {
            switch outcome {
            case .success:
                Image(systemName: "checkmark")
                    .font(.system(size: 9, weight: .bold))
                Text("Done")
                    .font(.system(size: 10.5, weight: .semibold))
            case .failure:
                Image(systemName: "xmark")
                    .font(.system(size: 9, weight: .bold))
                Text("Error")
                    .font(.system(size: 10.5, weight: .semibold))
            case .stopped:
                Image(systemName: "stop.fill")
                    .font(.system(size: 8))
                Text("Stopped")
                    .font(.system(size: 10.5, weight: .semibold))
            }
        }
        .foregroundStyle(Theme.textSecondary)
        .padding(.horizontal, 7)
        .padding(.vertical, 2.5)
        .background(Theme.cardElevated)
        .clipShape(Capsule())
        .overlay(Capsule().stroke(Theme.borderOverlay, lineWidth: 1))
    }

    private func actionPill(icon: String, label: String) -> some View {
        HStack(spacing: 4) {
            Image(systemName: icon)
                .font(.system(size: 9))
            Text(label)
                .font(.system(size: 10, design: .monospaced))
        }
        .foregroundStyle(Theme.textSecondary)
        .padding(.horizontal, 6)
        .padding(.vertical, 2.5)
        .background(Theme.cardElevated)
        .clipShape(RoundedRectangle(cornerRadius: 4, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 4, style: .continuous)
                .stroke(Theme.borderOverlay, lineWidth: 1)
        )
    }

    private func toggleExpanded(_ id: String) {
        if expandedEpisodeIDs.contains(id) {
            expandedEpisodeIDs.remove(id)
        } else {
            expandedEpisodeIDs.insert(id)
        }
    }

    private func rerunEpisode(_ episode: StoreEpisodeItem) {
        state.panelsStore.showSettings = false
        state.activeTab = .live
        state.composerText = episode.goal
        state.focusComposerCounter += 1
        if !state.runStore.isRunning {
            state.sendComposer()
        }
    }

    private var emptyHistoryState: some View {
        HStack {
            Spacer()
            VStack(spacing: 8) {
                Image(systemName: "clock")
                    .font(.system(size: 26))
                    .foregroundStyle(Theme.textMuted.opacity(0.5))
                Text("No task history recorded yet.")
                    .font(.system(size: 12.5))
                    .foregroundStyle(Theme.textMuted)
                Text("Detailed traces and step logs will appear here when tasks run.")
                    .font(.system(size: 11))
                    .foregroundStyle(Theme.textMuted.opacity(0.7))
            }
            .padding(.vertical, 40)
            Spacer()
        }
        .background(Theme.card)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(Theme.borderOverlay, lineWidth: 1)
        )
    }
}
