import SwiftUI

/// Analytics & Distilled Skills view:
/// - Top 4 KPI cards: Total Tasks, Success Rate, Total Actions, Distilled Skills
/// - Physical Actuations breakdown: Clicks, Typings, Pastes, Hotkeys, App Activations
/// - Learned Skills Registry: Distilled autonomous reusable workflows with 1-click Run button
struct AnalyticsView: View {
    @ObservedObject var state: AppState
    @ObservedObject var store = StoreDataService.shared
    @State private var hoveredSkillID: String?
    @State private var skillPendingDeletion: StoreSkillItem? = nil

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                headerBar

                Text("IDE session · " + state.sessionTelemetry.label)
                    .font(.system(size: 11, design: .monospaced)).foregroundStyle(Theme.textSecondary)
                    .padding(12).frame(maxWidth: .infinity, alignment: .leading).cardStyle(cornerRadius: 10)

                kpiCardsGrid

                physicalActuationsSection

                learnedSkillsSection

                Spacer(minLength: 40)
            }
            .padding(.horizontal, 32)
            .padding(.top, 20)
            .padding(.bottom, 40)
            .frame(maxWidth: 860, alignment: .leading)
        }
        .background(Color.clear)
        .onAppear {
            store.loadEpisodes()
            store.loadSkills()
        }
        .alert("Delete Distilled Skill", isPresented: Binding(
            get: { skillPendingDeletion != nil },
            set: { if !$0 { skillPendingDeletion = nil } }
        )) {
            Button("Cancel", role: .cancel) {
                skillPendingDeletion = nil
            }
            Button("Delete", role: .destructive) {
                if let skill = skillPendingDeletion {
                    store.deleteSkill(id: skill.id)
                    skillPendingDeletion = nil
                }
            }
        } message: {
            if let skill = skillPendingDeletion {
                Text("Are you sure you want to delete skill \"\(skill.name)\"?\n\nThis permanently deletes the workflow file from ~/.computeruse/skills.")
            }
        }
    }

    // MARK: - Header Bar

    private var headerBar: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Image(systemName: "chart.bar.xaxis")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Theme.accent)
                    Text("Analytics & Skills")
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(Theme.textPrimary)
                }
                Text("Real system execution metrics, action telemetry, and distilled autonomous skills")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textMuted)
            }

            Spacer()

            Button {
                store.loadEpisodes()
                store.loadSkills()
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 11, weight: .medium))
                    Text("Refresh")
                        .font(.system(size: 11.5, weight: .medium))
                }
                .foregroundStyle(Theme.textMuted)
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .background(Theme.card)
                .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .stroke(Theme.borderOverlay, lineWidth: 1)
                )
            }
            .buttonStyle(.plain)
            .hoverPointer(bg: Theme.cardElevated, radius: 6)
            .help("Rescan metrics and skills")
        }
        .padding(.bottom, 4)
    }

    // MARK: - 4 KPI Cards Grid (Matches Screenshot)

    private var kpiCardsGrid: some View {
        LazyVGrid(columns: [GridItem(.flexible(), spacing: 14), GridItem(.flexible(), spacing: 14)], spacing: 14) {
            kpiCard(
                icon: "circle.circle",
                title: "TOTAL TASKS",
                value: "\(store.totalTasks)",
                accentColor: Theme.accent
            )

            kpiCard(
                icon: "waveform.path.ecg",
                title: "SUCCESS RATE",
                value: "\(store.successRate)%",
                accentColor: Theme.textMuted
            )

            kpiCard(
                icon: "cursorarrow.rays",
                title: "TOTAL ACTIONS",
                value: "\(store.totalActions)",
                accentColor: Theme.accent
            )

            kpiCard(
                icon: "star.fill",
                title: "DISTILLED SKILLS",
                value: "\(store.distilledSkillsCount)",
                accentColor: Theme.textMuted
            )
        }
    }

    private func kpiCard(icon: String, title: String, value: String, accentColor: Color) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 7) {
                Image(systemName: icon)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(accentColor)
                Text(title)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Theme.textMuted)
                    .tracking(0.6)
            }

            Text(value)
                .font(.system(size: 28, weight: .bold, design: .rounded))
                .foregroundStyle(Theme.textPrimary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .background(Theme.card)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(Theme.borderOverlay, lineWidth: 1)
        )
    }

    // MARK: - Physical Actuations (Matches Screenshot)

    private var physicalActuationsSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 7) {
                Image(systemName: "display")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Theme.accent)
                Text("PHYSICAL ACTUATIONS")
                    .font(.system(size: 11.5, weight: .bold))
                    .foregroundStyle(Theme.textMuted)
                    .tracking(0.8)
            }

            VStack(spacing: 0) {
                actuationRow(
                    icon: "cursorarrow.click",
                    label: "Mouse Clicks",
                    value: store.actionMetrics.clicks
                )
                divider

                actuationRow(
                    icon: "keyboard",
                    label: "Text Typings",
                    value: store.actionMetrics.types
                )
                divider

                actuationRow(
                    icon: "doc.on.clipboard",
                    label: "Clipboard Pastes",
                    value: store.actionMetrics.pastes
                )
                divider

                actuationRow(
                    icon: "command",
                    label: "Keyboard Hotkeys",
                    value: store.actionMetrics.hotkeys
                )
                divider

                actuationRow(
                    icon: "macwindow",
                    label: "App Activations",
                    value: store.actionMetrics.apps
                )
            }
            .background(Theme.card)
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .stroke(Theme.borderOverlay, lineWidth: 1)
            )
        }
    }

    private func actuationRow(icon: String, label: String, value: Int) -> some View {
        HStack {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .font(.system(size: 12.5))
                    .foregroundStyle(Theme.textMuted)
                    .frame(width: 16)
                Text(label)
                    .font(.system(size: 13, weight: .regular))
                    .foregroundStyle(Theme.textPrimary)
            }

            Spacer()

            Text("\(value)")
                .font(.system(size: 13, weight: .semibold, design: .monospaced))
                .foregroundStyle(Theme.textPrimary)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10.5)
    }

    private var divider: some View {
        Divider()
            .background(Theme.borderOverlay.opacity(0.6))
            .padding(.horizontal, 14)
    }

    // MARK: - Learned Skills Registry (Matches Screenshot)

    private var learnedSkillsSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 7) {
                Image(systemName: "star.fill")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Theme.accent)
                Text("LEARNED SKILLS REGISTRY")
                    .font(.system(size: 11.5, weight: .bold))
                    .foregroundStyle(Theme.textMuted)
                    .tracking(0.8)

                Spacer()

                Text("\(store.skills.count) skills")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(Theme.textMuted)
            }

            if store.skills.isEmpty {
                HStack {
                    Spacer()
                    VStack(spacing: 8) {
                        Image(systemName: "brain.head.profile")
                            .font(.system(size: 24))
                            .foregroundStyle(Theme.textMuted.opacity(0.5))
                        Text("No synthesized skills found yet.")
                            .font(.system(size: 12))
                            .foregroundStyle(Theme.textMuted)
                        Text("As multi-step tasks complete, the agent automatically distills new reusable skills.")
                            .font(.system(size: 11))
                            .foregroundStyle(Theme.textMuted.opacity(0.7))
                    }
                    .padding(.vertical, 24)
                    Spacer()
                }
                .background(Theme.card)
                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .stroke(Theme.borderOverlay, lineWidth: 1)
                )
            } else {
                VStack(spacing: 8) {
                    ForEach(store.skills) { skill in
                        skillCard(skill)
                    }
                }
            }
        }
    }

    private func skillCard(_ skill: StoreSkillItem) -> some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Text(skill.name)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(Theme.textPrimary)
                        .lineLimit(1)

                    if skill.app != "macOS" && !skill.app.isEmpty {
                        Text(skill.app)
                            .font(.system(size: 10.5, weight: .medium))
                            .foregroundStyle(Theme.textSecondary)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(Theme.cardElevated)
                            .clipShape(Capsule())
                            .overlay(Capsule().stroke(Theme.borderOverlay, lineWidth: 1))
                    }
                }

                HStack(spacing: 8) {
                    Text("Autonomous Reusable Workflow")
                        .font(.system(size: 11))
                        .foregroundStyle(Theme.textMuted)

                    if skill.stepsCount > 0 {
                        Text("·")
                            .foregroundStyle(Theme.textMuted.opacity(0.5))
                        Text("\(skill.stepsCount) steps")
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(Theme.textMuted.opacity(0.8))
                    }

                    if skill.uses > 0 {
                        Text("·")
                            .foregroundStyle(Theme.textMuted.opacity(0.5))
                        Text("\(skill.uses) uses (\(skill.winRate))")
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(Theme.textMuted.opacity(0.8))
                    }
                }
            }

            Spacer()

            HStack(spacing: 6) {
                // [ Run ] button (matches screenshot)
                Button {
                    runSkill(skill)
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: "bolt.fill")
                            .font(.system(size: 10.5, weight: .bold))
                        Text("Run")
                            .font(.system(size: 12, weight: .semibold))
                    }
                    .foregroundStyle(Theme.canvas)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 6)
                    .background(Theme.accent)
                    .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                }
                .buttonStyle(.plain)
                .hoverPointer(bg: Theme.accent.opacity(0.85), scale: 1.02, radius: 6)
                .help("Execute this skill on Live Agent immediately")
                .disabled(store.disabledSkillIDs.contains(skill.id))

                // Toggle Enable / Disable
                Button(store.disabledSkillIDs.contains(skill.id) ? "Enable" : "Disable") {
                    store.toggleSkill(id: skill.id)
                }
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(Theme.textSecondary)
                .padding(.horizontal, 8)
                .padding(.vertical, 5.5)
                .background(Theme.cardElevated)
                .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .stroke(Theme.borderOverlay, lineWidth: 1)
                )
                .buttonStyle(.plain)
                .hoverPointer(radius: 6)
                .help("Controls IDE launch only; automatic agent retrieval is unchanged")

                // Delete Skill Button
                Button {
                    skillPendingDeletion = skill
                } label: {
                    Image(systemName: "trash")
                        .font(.system(size: 11))
                        .foregroundStyle(Theme.textMuted)
                        .frame(width: 26, height: 26)
                        .background(Theme.cardElevated)
                        .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 6, style: .continuous)
                                .stroke(Theme.borderOverlay, lineWidth: 1)
                        )
                }
                .buttonStyle(.plain)
                .hoverPointer(bg: Theme.danger.opacity(0.18), radius: 6)
                .help("Delete this distilled skill")
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .background(Theme.card)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(Theme.borderOverlay, lineWidth: 1)
        )
    }

    private func runSkill(_ skill: StoreSkillItem) {
        guard !store.disabledSkillIDs.contains(skill.id) else { return }
        state.panelsStore.showSettings = false
        state.activeTab = .live
        state.composerText = "Execute distilled skill: \(skill.name)"
        state.focusComposerCounter += 1
        // Auto-run if idle
        if !state.runStore.isRunning {
            state.sendComposer()
        }
    }
}
