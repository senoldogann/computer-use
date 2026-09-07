import SwiftUI

/// Extensions / MCP Store view:
/// - Header with active count badge
/// - Search bar & category filters
/// - Installed tools quick strip ("Installed Tools & Servers")
/// - Rich catalog grid with 1-click install, configuration modal for API keys, uninstall
/// - Custom MCP server creator
struct ExtensionsView: View {
    @ObservedObject var state: AppState
    @ObservedObject var store = StoreDataService.shared

    @State private var searchText = ""
    @State private var selectedCategory = "All"
    @State private var configuringPreset: McpCatalogItem?
    @State private var showCustomMcpSheet = false

    private let categories = ["All", "Featured", "Developer", "Productivity", "Reasoning", "Media"]

    private var filteredCatalog: [McpCatalogItem] {
        store.mcpCatalog.filter { item in
            let matchesCat = (selectedCategory == "All") || (item.category == selectedCategory)
            let matchesSearch = searchText.trimmingCharacters(in: .whitespaces).isEmpty
                || item.name.localizedCaseInsensitiveContains(searchText)
                || item.description.localizedCaseInsensitiveContains(searchText)
                || item.id.localizedCaseInsensitiveContains(searchText)
            return matchesCat && matchesSearch
        }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                headerBar

                searchAndFilterSection

                installedStripSection

                catalogGridSection

                Spacer(minLength: 40)
            }
            .padding(.horizontal, 32)
            .padding(.top, 20)
            .padding(.bottom, 40)
            .frame(maxWidth: 880, alignment: .leading)
        }
        .background(Color.clear)
        .onAppear {
            store.loadMcpServers()
        }
        .sheet(item: $configuringPreset) { preset in
            McpConfigModal(preset: preset, store: store)
        }
        .sheet(isPresented: $showCustomMcpSheet) {
            CustomMcpModal(store: store)
        }
    }

    // MARK: - Header Bar

    private var headerBar: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Image(systemName: "puzzlepiece.extension")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Theme.accent)
                    Text("Extensions")
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(Theme.textPrimary)

                    Text("\(store.installedMcp.count) Active")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Theme.textSecondary)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Theme.cardElevated)
                        .clipShape(Capsule())
                        .overlay(Capsule().stroke(Theme.borderOverlay, lineWidth: 1))
                }

                Text("Connect external tools and services via Model Context Protocol (MCP)")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textMuted)
            }

            Spacer()

            HStack(spacing: 8) {
                Button {
                    showCustomMcpSheet = true
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: "plus")
                            .font(.system(size: 11, weight: .bold))
                        Text("Add Custom MCP")
                            .font(.system(size: 11.5, weight: .medium))
                    }
                    .foregroundStyle(Theme.textPrimary)
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

                Button {
                    store.loadMcpServers()
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
                .help("Reload MCP configuration")
            }
        }
    }

    // MARK: - Search & Categories

    private var searchAndFilterSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textMuted)

                TextField("Search extensions or tools...", text: $searchText)
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
            .padding(.vertical, 8)
            .background(Theme.card)
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(Theme.borderOverlay, lineWidth: 1)
            )

            // Category pills
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(categories, id: \.self) { cat in
                        Button {
                            selectedCategory = cat
                        } label: {
                            Text(cat)
                                .font(.system(size: 11.5, weight: selectedCategory == cat ? .semibold : .regular))
                                .foregroundStyle(selectedCategory == cat ? Theme.textPrimary : Theme.textMuted)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 5)
                                .background(selectedCategory == cat ? Theme.cardElevated : Theme.card)
                                .clipShape(Capsule())
                                .overlay(
                                    Capsule()
                                        .stroke(selectedCategory == cat ? Theme.textSecondary.opacity(0.4) : Color.clear, lineWidth: 1)
                                )
                        }
                        .buttonStyle(.plain)
                        .hoverPointer(radius: 12)
                    }
                }
            }
        }
    }

    // MARK: - Installed Strip ("Installed Tools & Servers")

    private var installedStripSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Installed Tools & Servers")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Theme.textPrimary)

                Spacer()

                Text("\(store.installedMcp.count) Installed")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Theme.accent)
            }

            if store.installedMcp.isEmpty {
                Text("No MCP extensions currently active. Install from the catalog below or add a custom server.")
                    .font(.system(size: 11.5))
                    .foregroundStyle(Theme.textMuted)
                    .padding(.vertical, 8)
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(Array(store.installedMcp.keys.sorted()), id: \.self) { serverName in
                            HStack(spacing: 6) {
                                Circle()
                                    .fill(Theme.success)
                                    .frame(width: 6, height: 6)

                                Text(serverName)
                                    .font(.system(size: 12, weight: .medium, design: .monospaced))
                                    .foregroundStyle(Theme.textPrimary)

                                Button {
                                    store.uninstallMcp(id: serverName)
                                } label: {
                                    Image(systemName: "xmark")
                                        .font(.system(size: 9, weight: .bold))
                                        .foregroundStyle(Theme.textMuted)
                                        .frame(width: 14, height: 14)
                                }
                                .buttonStyle(.plain)
                                .hoverPointer(radius: 3)
                                .help("Uninstall \(serverName)")
                            }
                            .padding(.horizontal, 10)
                            .padding(.vertical, 6)
                            .background(Theme.cardElevated)
                            .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                            .overlay(
                                RoundedRectangle(cornerRadius: 6, style: .continuous)
                                    .stroke(Theme.borderOverlay, lineWidth: 1)
                            )
                        }
                    }
                }
            }
        }
        .padding(14)
        .background(Theme.card)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(Theme.borderOverlay, lineWidth: 1)
        )
    }

    // MARK: - Catalog Grid

    private var catalogGridSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("MCP Catalog")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Theme.textMuted)
                Spacer()
                Text("\(filteredCatalog.count) available")
                    .font(.system(size: 11))
                    .foregroundStyle(Theme.textMuted)
            }

            LazyVGrid(columns: [GridItem(.flexible(), spacing: 14), GridItem(.flexible(), spacing: 14)], spacing: 14) {
                ForEach(filteredCatalog) { item in
                    catalogCard(item)
                }
            }
        }
    }

    private func catalogCard(_ item: McpCatalogItem) -> some View {
        let isInstalled = store.isMcpInstalled(item.id)

        return VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 10) {
                // Icon
                ZStack {
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(Theme.cardElevated)
                        .frame(width: 34, height: 34)
                    Image(systemName: item.iconSystemName)
                        .font(.system(size: 15))
                        .foregroundStyle(Theme.textSecondary)
                }

                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(item.name)
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(Theme.textPrimary)
                            .lineLimit(1)

                        if isInstalled {
                            Image(systemName: "checkmark.circle.fill")
                                .font(.system(size: 11))
                                .foregroundStyle(Theme.success)
                        }
                    }

                    Text(item.category)
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(Theme.textMuted)
                }

                Spacer()
            }

            Text(item.description)
                .font(.system(size: 11.5))
                .foregroundStyle(Theme.textMuted)
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)

            HStack {
                if !item.requiredEnvKeys.isEmpty {
                    HStack(spacing: 4) {
                        Image(systemName: "key.fill")
                            .font(.system(size: 9))
                        Text(item.requiredEnvKeys.joined(separator: ", "))
                            .font(.system(size: 9.5, design: .monospaced))
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

                Spacer()

                if isInstalled {
                    HStack(spacing: 6) {
                        if !item.requiredEnvKeys.isEmpty {
                            Button {
                                configuringPreset = item
                            } label: {
                                Image(systemName: "gearshape.fill")
                                    .font(.system(size: 11))
                                    .foregroundStyle(Theme.textMuted)
                                    .frame(width: 26, height: 26)
                                    .background(Theme.cardElevated)
                                    .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
                                    .overlay(
                                        RoundedRectangle(cornerRadius: 5, style: .continuous)
                                            .stroke(Theme.borderOverlay, lineWidth: 1)
                                    )
                            }
                            .buttonStyle(.plain)
                            .hoverPointer(radius: 5)
                            .help("Configure environment variables")
                        }

                        Button {
                            store.uninstallMcp(id: item.id)
                        } label: {
                            Text("Uninstall")
                                .font(.system(size: 11, weight: .medium))
                                .foregroundStyle(Theme.textSecondary)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 4.5)
                                .background(Theme.cardElevated)
                                .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
                                .overlay(
                                    RoundedRectangle(cornerRadius: 5, style: .continuous)
                                        .stroke(Theme.borderOverlay, lineWidth: 1)
                                )
                        }
                        .buttonStyle(.plain)
                        .hoverPointer(radius: 5)
                    }
                } else {
                    Button {
                        if !item.requiredEnvKeys.isEmpty {
                            configuringPreset = item
                        } else {
                            store.installMcp(id: item.id, command: item.command, args: item.args, env: [:])
                        }
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: "arrow.down.circle.fill")
                                .font(.system(size: 10))
                            Text("Install")
                                .font(.system(size: 11, weight: .semibold))
                        }
                        .foregroundStyle(Theme.canvas)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 4.5)
                        .background(Theme.accent)
                        .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
                    }
                    .buttonStyle(.plain)
                    .hoverPointer(bg: Theme.accent.opacity(0.85), radius: 5)
                }
            }
        }
        .padding(14)
        .background(Theme.card)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(isInstalled ? Theme.borderOverlay : Theme.borderOverlay.opacity(0.5), lineWidth: 1)
        )
    }
}

// MARK: - MCP Configuration Modal (API Key inputs)

private struct McpConfigModal: View {
    let preset: McpCatalogItem
    let store: StoreDataService
    @Environment(\.dismiss) private var dismiss

    @State private var envValues: [String: String] = [:]

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                HStack(spacing: 8) {
                    Image(systemName: preset.iconSystemName)
                        .font(.system(size: 14))
                        .foregroundStyle(Theme.accent)
                    Text("Configure \(preset.name)")
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(Theme.textPrimary)
                }
                Spacer()
                Button {
                    dismiss()
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Theme.textMuted)
                }
                .buttonStyle(.plain)
            }

            Text("This extension requires API credentials or configuration keys to operate:")
                .font(.system(size: 11.5))
                .foregroundStyle(Theme.textMuted)

            VStack(spacing: 10) {
                ForEach(preset.requiredEnvKeys, id: \.self) { key in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(key)
                            .font(.system(size: 11, weight: .medium, design: .monospaced))
                            .foregroundStyle(Theme.textMuted)

                        SecureField("Enter \(key)...", text: Binding(
                            get: { envValues[key] ?? "" },
                            set: { envValues[key] = $0 }
                        ))
                        .textFieldStyle(.plain)
                        .font(.system(size: 12, design: .monospaced))
                        .padding(.horizontal, 10)
                        .padding(.vertical, 7)
                        .background(Theme.cardElevated)
                        .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 6, style: .continuous)
                                .stroke(Theme.borderOverlay, lineWidth: 1)
                        )
                    }
                }
            }

            HStack {
                Button("Cancel") {
                    dismiss()
                }
                .buttonStyle(.plain)
                .font(.system(size: 12))
                .foregroundStyle(Theme.textMuted)

                Spacer()

                Button("Save & Install") {
                    store.installMcp(
                        id: preset.id,
                        command: preset.command,
                        args: preset.args,
                        env: envValues
                    )
                    dismiss()
                }
                .buttonStyle(.plain)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Theme.canvas)
                .padding(.horizontal, 14)
                .padding(.vertical, 6)
                .background(Theme.accent)
                .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                .hoverPointer(bg: Theme.accent.opacity(0.85), radius: 6)
            }
        }
        .padding(20)
        .frame(width: 440)
        .background(Theme.card)
        .onAppear {
            if let existing = store.installedMcp[preset.id.lowercased()],
               let env = existing["env"] as? [String: String] {
                envValues = env
            }
        }
    }
}

// MARK: - Add Custom MCP Modal

private struct CustomMcpModal: View {
    let store: StoreDataService
    @Environment(\.dismiss) private var dismiss

    @State private var serverId = ""
    @State private var command = ""
    @State private var argsString = ""
    @State private var envString = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("Add Custom MCP Server")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(Theme.textPrimary)
                Spacer()
                Button {
                    dismiss()
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Theme.textMuted)
                }
                .buttonStyle(.plain)
            }

            VStack(spacing: 12) {
                fieldRow(label: "Server Name / ID", placeholder: "e.g. filesystem", text: $serverId)
                fieldRow(label: "Command / Executable", placeholder: "e.g. npx or node", text: $command)
                fieldRow(label: "Arguments (space separated)", placeholder: "-y @modelcontextprotocol/server-filesystem ...", text: $argsString)
                fieldRow(label: "Environment (KEY=VAL, comma separated)", placeholder: "API_KEY=xxx, PORT=3000", text: $envString)
            }

            HStack {
                Button("Cancel") {
                    dismiss()
                }
                .buttonStyle(.plain)
                .font(.system(size: 12))
                .foregroundStyle(Theme.textMuted)

                Spacer()

                Button("Add Server") {
                    let args = argsString.split(separator: " ").map(String.init)
                    var env: [String: String] = [:]
                    for pair in envString.split(separator: ",") {
                        let parts = pair.split(separator: "=", maxSplits: 1).map(String.init)
                        if parts.count == 2 {
                            env[parts[0].trimmingCharacters(in: .whitespaces)] = parts[1].trimmingCharacters(in: .whitespaces)
                        }
                    }
                    store.installMcp(
                        id: serverId.trimmingCharacters(in: .whitespaces),
                        command: command.trimmingCharacters(in: .whitespaces),
                        args: args,
                        env: env
                    )
                    dismiss()
                }
                .buttonStyle(.plain)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Theme.canvas)
                .padding(.horizontal, 14)
                .padding(.vertical, 6)
                .background(Theme.accent)
                .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                .disabled(serverId.isEmpty || command.isEmpty)
                .hoverPointer(bg: Theme.accent.opacity(0.85), radius: 6)
            }
        }
        .padding(20)
        .frame(width: 440)
        .background(Theme.card)
    }

    private func fieldRow(label: String, placeholder: String, text: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(Theme.textMuted)
            TextField(placeholder, text: text)
                .textFieldStyle(.plain)
                .font(.system(size: 12, design: .monospaced))
                .padding(.horizontal, 10)
                .padding(.vertical, 7)
                .background(Theme.cardElevated)
                .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .stroke(Theme.borderOverlay, lineWidth: 1)
                )
        }
    }
}
