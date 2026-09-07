import SwiftUI

/// Settings view:
/// - General (Model, Autonomy level, OpenAI API)
/// - Extensions (MCP Tools & Servers Catalog, Active Servers, Custom Server Add)
/// - Actuation & Safety (macOS control --real, 60fps viewport, Unix socket)
/// - Appearance (Sidebar and Live Viewport toggles)
/// - Keybindings (IDE keyboard shortcuts)
/// - About & Diagnostics (Version, runtime statistics, reset)
struct SettingsView: View {
    @ObservedObject var state: AppState
    @ObservedObject var panelsStore: PanelsStore
    @ObservedObject var store = StoreDataService.shared
    @State private var selectedCategory: SettingsCategory = .general
    @State private var searchText = ""

    init(state: AppState) {
        self.state = state
        self.panelsStore = state.panelsStore
    }

    enum SettingsCategory: String, CaseIterable, Identifiable {
        case general = "General"
        case extensions = "Extensions"
        case history = "History"
        case control = "Control & Safety"
        case analytics = "Analytics & Skills"
        case actuation = "Actuation & Real Driver"
        case appearance = "Appearance"
        case keybindings = "Keybindings"
        case about = "About & Diagnostics"

        var id: String { rawValue }

        var icon: String {
            switch self {
            case .general: return "slider.horizontal.3"
            case .extensions: return "puzzlepiece.extension"
            case .history: return "clock.arrow.circlepath"
            case .control: return "shield.checkered"
            case .analytics: return "chart.bar.xaxis"
            case .actuation: return "lock.shield"
            case .appearance: return "macwindow"
            case .keybindings: return "command"
            case .about: return "info.circle"
            }
        }
    }

    var filteredCategories: [SettingsCategory] {
        if searchText.trimmingCharacters(in: .whitespaces).isEmpty {
            return SettingsCategory.allCases
        }
        return SettingsCategory.allCases.filter {
            $0.rawValue.localizedCaseInsensitiveContains(searchText)
        }
    }

    var body: some View {
        HStack(spacing: 0) {
            settingsSidebar
                .frame(width: 240)
                .background(GlassPanel(material: .sidebar, wash: Theme.sidebarWash))
                .overlay(alignment: .trailing) {
                    Theme.borderOverlay.frame(width: 1)
                }

            settingsContent
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Theme.canvas)
        }
        .frame(minWidth: 880, minHeight: 580)
        .onKeyPress(keys: [.escape]) { _ in
            state.panelsStore.showSettings = false
            return .handled
        }
    }

    // MARK: - Left Category Menu (Aligned with traffic lights)

    private var settingsSidebar: some View {
        VStack(spacing: 0) {
            HStack {
                Spacer().frame(width: 76)
                Spacer()
            }
            .frame(height: Theme.titleZoneHeight)

            // Search
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 11.5))
                    .foregroundStyle(Theme.textMuted)
                TextField("Search settings", text: $searchText)
                    .textFieldStyle(.plain)
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textPrimary)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(Color.white.opacity(0.04))
            .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
            .padding(.horizontal, 14)
            .padding(.top, 4)
            .padding(.bottom, 12)

            // Categories
            ScrollView {
                VStack(spacing: 2) {
                    ForEach(filteredCategories) { cat in
                        Button {
                            selectedCategory = cat
                        } label: {
                            HStack(spacing: 9) {
                                Image(systemName: cat.icon)
                                    .font(.system(size: 12))
                                    .foregroundStyle(selectedCategory == cat ? Theme.textPrimary : Theme.textMuted)
                                    .frame(width: 16)
                                Text(cat.rawValue)
                                    .font(.system(size: 12.5, weight: selectedCategory == cat ? .medium : .regular))
                                    .foregroundStyle(selectedCategory == cat ? Theme.textPrimary : Theme.textMuted)
                                Spacer()

                                if cat == .control && store.pendingApprovals.count > 0 {
                                    Text("\(store.pendingApprovals.count)")
                                        .font(.system(size: 9.5, weight: .bold))
                                        .foregroundStyle(Theme.canvas)
                                        .padding(.horizontal, 5)
                                        .padding(.vertical, 1)
                                        .background(Theme.warning)
                                        .clipShape(Capsule())
                                }
                            }
                            .padding(.horizontal, 10)
                            .padding(.vertical, 7)
                            .background(selectedCategory == cat ? Theme.cardFillHover : Color.clear)
                            .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
                        }
                        .buttonStyle(.plain)
                        .hoverPointer(bg: selectedCategory == cat ? Theme.cardFillHover : Theme.card, radius: 7)
                    }
                }
                .padding(.horizontal, 10)
            }

            Spacer()

            // Back Button (← Back to IDE)
            Button {
                state.panelsStore.showSettings = false
            } label: {
                HStack(spacing: 7) {
                    Image(systemName: "arrow.left")
                        .font(.system(size: 12, weight: .medium))
                    Text("Back to IDE")
                        .font(.system(size: 12, weight: .medium))
                }
                .foregroundStyle(Theme.textMuted)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(Theme.card)
                .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 7, style: .continuous)
                        .stroke(Theme.borderOverlay, lineWidth: 1)
                )
            }
            .buttonStyle(.plain)
            .hoverPointer(bg: Theme.cardElevated, radius: 7)
            .padding(14)
        }
    }

    // MARK: - Main Content

    private var settingsContent: some View {
        VStack(spacing: 0) {
            contentTopBar

            switch selectedCategory {
            case .extensions:
                ExtensionsView(state: state)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .history:
                HistoryView(state: state)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .control:
                ControlView(state: state)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .analytics:
                AnalyticsView(state: state)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            default:
                ScrollView {
                    VStack(alignment: .leading, spacing: 28) {
                        switch selectedCategory {
                        case .general:
                            generalSection
                        case .actuation:
                            actuationSection
                        case .appearance:
                            appearanceSection
                        case .keybindings:
                            keybindingsSection
                        case .about:
                            aboutSection
                        default:
                            EmptyView()
                        }
                    }
                    .padding(.horizontal, 36)
                    .padding(.vertical, 28)
                    .frame(maxWidth: 820, alignment: .leading)
                }
            }
        }
    }

    // MARK: - Breadcrumb & Restore Defaults

    private var contentTopBar: some View {
        HStack {
            HStack(spacing: 6) {
                Text("Settings")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textMuted)
                Image(systemName: "chevron.right")
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(Theme.textMuted.opacity(0.5))
                Text(selectedCategory.rawValue)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(Theme.textPrimary)
            }

            Spacer()

            if selectedCategory != .extensions {
                Button("Restore Defaults") {
                    state.restoreDefaultSettings()
                }
                .buttonStyle(.plain)
                .font(.system(size: 11.5, weight: .medium))
                .foregroundStyle(Theme.textMuted)
                .padding(.horizontal, 9)
                .padding(.vertical, 4.5)
                .background(Color.white.opacity(0.04))
                .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                .hoverPointer(bg: Color.white.opacity(0.08), radius: 6)
            }

            Button {
                state.panelsStore.showSettings = false
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: "xmark")
                        .font(.system(size: 11, weight: .semibold))
                    Text("Esc")
                        .font(.system(size: 10, weight: .medium, design: .monospaced))
                        .foregroundStyle(Theme.textMuted)
                }
                .foregroundStyle(Theme.textSecondary)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(Color.white.opacity(0.06))
                .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            }
            .buttonStyle(.plain)
            .pointingHand()
            .help("Close Settings (Esc)")
        }
        .padding(.horizontal, 32)
        .frame(height: 44)
    }

    // MARK: - 1. General (Model, Autonomy, API)

    private var generalSection: some View {
        let keyStatus = SystemStatus.openAIKeyStatus()

        return VStack(alignment: .leading, spacing: 18) {
            sectionHeader(title: "Agent & Model Configuration", subtitle: "Configure the LLM model and approval autonomy level.")

            SettingsGroupCard {
                VStack(spacing: 0) {
                    menuRow(
                        title: "Default Model",
                        subtitle: "Primary model used by the composer and actuation engine.",
                        current: state.selectedModel,
                        options: AgentModel.all
                    ) { state.setSelectedModel($0) }

                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)

                    menuRow(
                        title: "Autonomy Level",
                        subtitle: "Permission level granted for system actuation (--level).",
                        current: state.autonomyLevel.rawValue,
                        options: AutonomyLevel.allCases.map(\.rawValue)
                    ) { val in
                        if let level = AutonomyLevel(rawValue: val) {
                            state.autonomyLevel = level
                        }
                    }

                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)

                    infoRow(
                        title: "OpenAI API Environment",
                        subtitle: "API key is automatically loaded from ~/.computeruse/env; value is never displayed.",
                        badge: keyStatus.text
                    )

                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)

                    toggleRow(
                        title: "Trust Mode (--yes)",
                        subtitle: "Uninterrupted autonomy. Automatically approves confirmation steps without pausing.",
                        isOn: Binding(
                            get: { state.trustMode },
                            set: { _ in state.toggleTrustMode() }
                        )
                    )
                }
            }
        }
    }

    // MARK: - 2. Actuation & Safety (macOS control, FPS, Unix Socket)

    private var actuationSection: some View {
        VStack(alignment: .leading, spacing: 18) {
            sectionHeader(title: "Actuation & Host Safety", subtitle: "macOS Accessibility and ScreenCaptureKit hardware acceleration parameters.")
            SafetySettingsView(state: state)

            SettingsGroupCard {
                VStack(spacing: 0) {
                    toggleRow(
                        title: "Physical macOS Control (--real)",
                        subtitle: "Sends native mouse and keyboard events when enabled; runs in simulated sandbox when disabled.",
                        isOn: Binding(
                            get: { state.useRealDriver },
                            set: { state.setUseRealDriver($0) }
                        )
                    )

                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)

                    menuRow(
                        title: "Target Capture Rate (Target FPS)",
                        subtitle: "Maximum refresh frequency for ScreenCaptureKit live stream.",
                        current: "\(state.targetFps) FPS",
                        options: ["30 FPS", "60 FPS"]
                    ) { val in
                        state.targetFps = val.contains("30") ? 30 : 60
                    }

                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)

                    infoRow(
                        title: "Rust Actuation Socket",
                        subtitle: "POSIX IPC stream over /tmp/actuation-driver.sock.",
                        badge: state.bridge.driverBackend.isEmpty ? "Local Sensors (30Hz)" : state.bridge.driverBackend
                    )

                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)

                    infoRow(
                        title: "Accessibility (AX)",
                        subtitle: "AX tree provides UI coordinates. Without it, agent operates blind.",
                        badge: SystemStatus.accessibilityStatus().text
                    )

                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)

                    infoRow(
                        title: "Screen Recording",
                        subtitle: "Required for live viewport preview.",
                        badge: SystemStatus.screenRecordingStatus().text
                    )
                }
            }
        }
    }

    // MARK: - 3. Appearance (Sidebar, Viewport Toggles)

    private var appearanceSection: some View {
        VStack(alignment: .leading, spacing: 18) {
            sectionHeader(title: "IDE Appearance & Panels", subtitle: "Manage workspace layout and viewport visibility.")

            SettingsGroupCard {
                VStack(spacing: 0) {
                    toggleRow(
                        title: "Left Sidebar",
                        subtitle: "Panel containing past sessions and search bar.",
                        isOn: $panelsStore.isSidebarVisible
                    )

                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)

                    toggleRow(
                        title: "Right Live Viewport",
                        subtitle: "ScreenCaptureKit 60 FPS live preview and telemetry panel.",
                        isOn: $panelsStore.isRightPanelVisible
                    )

                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)

                    infoRow(
                        title: "Visual Theme",
                        subtitle: "Obsidian dark workspace design.",
                        badge: "Obsidian (Active)"
                    )
                }
            }
        }
    }

    // MARK: - 4. Keybindings (Shortcuts)

    private var keybindingsSection: some View {
        VStack(alignment: .leading, spacing: 18) {
            sectionHeader(title: "Keyboard Shortcuts", subtitle: "Active keyboard shortcuts within ComputerUse Desktop IDE.")

            SettingsGroupCard {
                VStack(spacing: 0) {
                    shortcutRow(label: "Run / Send Task", keys: "⌘ + Return")
                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)
                    shortcutRow(label: "Open / Close Settings", keys: "⌘ + ,")
                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)
                    shortcutRow(label: "Toggle Left Sidebar", keys: "⌘ + B")
                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)
                    shortcutRow(label: "Toggle Right Live Viewport", keys: "⌘ + ⌥ + ]")
                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)
                    shortcutRow(label: "Quick Model Switcher", keys: "⌘ + K")
                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)
                    shortcutRow(label: "New Task", keys: "⌘ + N")
                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)
                    shortcutRow(label: "Emergency Stop (SIGINT)", keys: "Esc / ⌘.")
                }
            }
        }
    }

    // MARK: - 5. About & Diagnostics (Version & Stats)

    private var aboutSection: some View {
        VStack(alignment: .leading, spacing: 18) {
            sectionHeader(title: "About & Session Diagnostics", subtitle: "Runtime statistics and engine telemetry.")

            SettingsGroupCard {
                VStack(spacing: 0) {
                    infoRow(
                        title: "Version",
                        subtitle: "macOS Native Actuation IDE (SwiftUI + ScreenCaptureKit + Rust)",
                        badge: "v\(Theme.appVersion)"
                    )

                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)

                    infoRow(
                        title: "Total Executed Tasks",
                        subtitle: "Number of tasks triggered during the session.",
                        badge: "\(state.runStore.runCount) tasks"
                    )

                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)

                    infoRow(
                        title: "Success / Failed",
                        subtitle: "Distribution of completed vs parked (SIGINT) tasks.",
                        badge: "✓ \(state.runStore.succeededCount) · ✕ \(state.runStore.failedCount)"
                    )

                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)

                    infoRow(
                        title: "Average Response Time",
                        subtitle: "Average time to complete goal.",
                        badge: String(format: "%.2f s", state.runStore.averageLatency)
                    )

                    Divider().background(Theme.borderOverlay).padding(.horizontal, 14)

                    HStack {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("Reset Statistics")
                                .font(.system(size: 13, weight: .medium))
                                .foregroundStyle(Theme.textPrimary)
                            Text("Clears session counters and latency metrics.")
                                .font(.system(size: 11.5))
                                .foregroundStyle(Theme.textMuted)
                        }
                        Spacer()
                        Button("Reset") {
                            state.runStore.resetStats()
                        }
                        .buttonStyle(.plain)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Theme.textSecondary)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 5)
                        .background(Theme.cardElevated)
                        .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 6, style: .continuous)
                                .stroke(Theme.borderOverlay, lineWidth: 1)
                        )
                        .hoverPointer(radius: 6)
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 12)
                }
            }
        }
    }

    // MARK: - Common components

    private func sectionHeader(title: String, subtitle: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.system(size: 17, weight: .semibold, design: .rounded))
                .foregroundStyle(Theme.textPrimary)
            Text(subtitle)
                .font(.system(size: 12))
                .foregroundStyle(Theme.textMuted)
        }
        .padding(.bottom, 4)
    }

    private func toggleRow(title: String, subtitle: String, isOn: Binding<Bool>) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(Theme.textPrimary)
                Text(subtitle)
                    .font(.system(size: 11.5))
                    .foregroundStyle(Theme.textMuted)
            }
            Spacer()
            Toggle("", isOn: isOn)
                .toggleStyle(.switch)
                .controlSize(.small)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }

    private func menuRow(title: String, subtitle: String, current: String, options: [String], onSelect: @escaping (String) -> Void) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(Theme.textPrimary)
                Text(subtitle)
                    .font(.system(size: 11.5))
                    .foregroundStyle(Theme.textMuted)
            }
            Spacer()
            Menu {
                ForEach(options, id: \.self) { opt in
                    Button(opt) { onSelect(opt) }
                }
            } label: {
                HStack(spacing: 5) {
                    Text(current)
                        .font(.system(size: 12, design: .monospaced))
                    Image(systemName: "chevron.up.chevron.down")
                        .font(.system(size: 10))
                }
                .foregroundStyle(Theme.textPrimary)
                .padding(.horizontal, 10)
                .padding(.vertical, 5)
                .background(Theme.cardElevated)
                .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }

    private func infoRow(title: String, subtitle: String, badge: String) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(Theme.textPrimary)
                Text(subtitle)
                    .font(.system(size: 11.5))
                    .foregroundStyle(Theme.textMuted)
            }
            Spacer()
            Text(badge)
                .font(.system(size: 11, weight: .medium, design: .monospaced))
                .foregroundStyle(Theme.textSecondary)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(Theme.cardElevated)
                .clipShape(Capsule())
                .overlay(
                    Capsule()
                        .stroke(Theme.borderOverlay, lineWidth: 1)
                )
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }

    private func shortcutRow(label: String, keys: String) -> some View {
        HStack {
            Text(label)
                .font(.system(size: 13, weight: .regular))
                .foregroundStyle(Theme.textPrimary)
            Spacer()
            Text(keys)
                .font(.system(size: 11.5, weight: .medium, design: .monospaced))
                .foregroundStyle(Theme.textMuted)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(Theme.cardElevated)
                .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 11)
    }
}

// MARK: - Reusable Settings Group Card

private struct SettingsGroupCard<Content: View>: View {
    @ViewBuilder let content: Content

    var body: some View {
        VStack(spacing: 0) {
            content
        }
        .background(Theme.card)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(Theme.borderOverlay, lineWidth: 1)
        )
    }
}
