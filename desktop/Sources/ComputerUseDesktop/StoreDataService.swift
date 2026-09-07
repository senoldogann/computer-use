import AppKit
import Combine
import Foundation

// MARK: - Models

/// An episode record loaded from ~/.computeruse/episodes/*.json
struct StoreEpisodeItem: Identifiable, Sendable {
    var id: String { episodeId }
    let episodeId: String
    let app: String
    let goal: String
    let outcome: EpisodeOutcome
    let stepsCount: Int
    let recordedAt: Date
    let retrospective: String?
    let actions: PhysicalActionsCount
    let stepTypes: [String]

    var formattedDate: String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US")
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        return formatter.string(from: recordedAt)
    }

    var relativeTime: String {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: recordedAt, relativeTo: Date())
    }
}

enum EpisodeOutcome: String, Sendable {
    case success
    case failure
    case stopped

    var label: String {
        switch self {
        case .success: return "Done"
        case .failure: return "Error"
        case .stopped: return "Stopped"
        }
    }
}

struct PhysicalActionsCount: Sendable {
    var clicks: Int = 0
    var types: Int = 0
    var pastes: Int = 0
    var hotkeys: Int = 0
    var apps: Int = 0

    var total: Int {
        clicks + types + pastes + hotkeys + apps
    }
}

/// A distilled skill loaded from ~/.computeruse/skills/*.json
struct StoreSkillItem: Identifiable, Sendable {
    var id: String { skillId }
    let skillId: String
    let name: String
    let app: String
    let description: String
    let uses: Int
    let wins: Int
    let stepsCount: Int
    let steps: [String]
    let phase: String

    var winRate: String {
        guard uses > 0 else { return "—" }
        let pct = Int((Double(wins) / Double(uses)) * 100)
        return "\(pct)%"
    }
}

/// A pending approval from ~/.computeruse/approvals/*.json
struct PendingApprovalItem: Identifiable, Sendable {
    var id: String { requestId }
    var goal: String = ""
    var missionId: String? = nil
    let requestId: String
    let subGoal: String
    let actionType: String
    let risk: String
    let targetLabel: String
    let decision: String
    let createdAt: String
}

/// A paused mission from ~/.computeruse/missions/*.json
struct BlockedMissionItem: Identifiable, Sendable {
    var id: String { missionId }
    var planID: String? = nil
    var approvalID: String? = nil
    let missionId: String
    let goal: String
    let app: String
    let attempts: Int
    let blockedReason: String
    let status: String
}

/// An active standing grant from ~/.computeruse/grants/*.json
struct StandingGrantItem: Identifiable, Sendable {
    var id: String { grantId }
    let grantId: String
    let verb: String
    let app: String
    let targetPattern: String
    let used: Int
    let maxInvocations: Int
    let expiresAt: String
    let note: String

    var remainingUses: Int {
        max(0, maxInvocations - used)
    }

    var isLive: Bool {
        if remainingUses <= 0 { return false }
        if expiresAt.isEmpty { return true }
        guard let expDate = ISO8601DateFormatter().date(from: expiresAt) else { return true }
        return expDate > Date()
    }
}

/// Curated MCP Catalog item
struct McpCatalogItem: Identifiable, Sendable {
    let id: String
    let name: String
    let category: String
    let description: String
    let iconSystemName: String
    let command: String
    let args: [String]
    let requiredEnvKeys: [String]
    let envKeyPlaceholder: String?
}

// MARK: - Store Data Service

@MainActor
final class StoreDataService: ObservableObject {
    static let shared = StoreDataService()
    private weak var appState: AppState?
    func attach(state: AppState) { appState = state }

    // MARK: - Published State

    @Published private(set) var episodes: [StoreEpisodeItem] = []
    @Published private(set) var disabledSkillIDs: Set<String> = Set(UserDefaults.standard.stringArray(forKey: "cu.disabledSkills") ?? [])
    func toggleSkill(id: String) {
        if disabledSkillIDs.contains(id) { disabledSkillIDs.remove(id) }
        else { disabledSkillIDs.insert(id) }
        UserDefaults.standard.set(disabledSkillIDs.sorted(), forKey: "cu.disabledSkills")
    }
    @Published private(set) var skills: [StoreSkillItem] = []
    @Published private(set) var installedMcp: [String: [String: Any]] = [:]
    @Published private(set) var approvalRecords: [PendingApprovalItem] = []
    @Published private(set) var decidingApprovalIDs: Set<String> = []
    @Published private(set) var pendingApprovals: [PendingApprovalItem] = []
    @Published private(set) var blockedMissions: [BlockedMissionItem] = []
    @Published private(set) var activeGrants: [StandingGrantItem] = []
    @Published private(set) var auditReportText: String = ""
    @Published var isLoadingReport: Bool = false
    @Published var statusToast: String?

    // Aggregates
    @Published private(set) var totalTasks: Int = 0
    @Published private(set) var successRate: Int = 100
    @Published private(set) var totalActions: Int = 0
    @Published private(set) var distilledSkillsCount: Int = 0
    @Published private(set) var actionMetrics = PhysicalActionsCount()

    // Directories
    private let homeDir = FileManager.default.homeDirectoryForCurrentUser
    private var computerUseDir: URL { homeDir.appendingPathComponent(".computeruse") }
    private var episodesDir: URL { computerUseDir.appendingPathComponent("episodes") }
    private var skillsDir: URL { computerUseDir.appendingPathComponent("skills") }
    private var mcpConfigURL: URL { computerUseDir.appendingPathComponent("mcp.json") }
    private var approvalsDir: URL { computerUseDir.appendingPathComponent("approvals") }
    private var missionsDir: URL { computerUseDir.appendingPathComponent("missions") }
    private var grantsDir: URL { computerUseDir.appendingPathComponent("grants") }

    // MARK: - MCP Catalog

    let mcpCatalog: [McpCatalogItem] = [
        McpCatalogItem(
            id: "filesystem",
            name: "Local Filesystem",
            category: "Featured",
            description: "Safely read, write, and search files and directories on your Mac.",
            iconSystemName: "folder.fill",
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-filesystem", "/Users"],
            requiredEnvKeys: [],
            envKeyPlaceholder: nil
        ),
        McpCatalogItem(
            id: "memory",
            name: "Knowledge Graph Memory",
            category: "Featured",
            description: "Persistent knowledge graph memory across agent sessions.",
            iconSystemName: "brain.head.profile",
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-memory"],
            requiredEnvKeys: [],
            envKeyPlaceholder: nil
        ),
        McpCatalogItem(
            id: "tavily",
            name: "Tavily AI Search",
            category: "Featured",
            description: "Real-time web search optimized for AI models and autonomous agents.",
            iconSystemName: "globe",
            command: "npx",
            args: ["-y", "tavily-mcp@latest"],
            requiredEnvKeys: ["TAVILY_API_KEY"],
            envKeyPlaceholder: "tvly-..."
        ),
        McpCatalogItem(
            id: "exa",
            name: "Exa Neural Search",
            category: "Featured",
            description: "Semantic neural web search designed for autonomous LLM agents.",
            iconSystemName: "sparkle.magnifyingglass",
            command: "npx",
            args: ["-y", "exa-mcp-server"],
            requiredEnvKeys: ["EXA_API_KEY"],
            envKeyPlaceholder: "exa_key_..."
        ),
        McpCatalogItem(
            id: "github",
            name: "GitHub Developer",
            category: "Developer",
            description: "Inspect repositories, open issues, and search code across GitHub.",
            iconSystemName: "chevron.left.forwardslash.chevron.right",
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-github"],
            requiredEnvKeys: ["GITHUB_PERSONAL_ACCESS_TOKEN"],
            envKeyPlaceholder: "ghp_..."
        ),
        McpCatalogItem(
            id: "playwright",
            name: "Playwright Browser",
            category: "Developer",
            description: "Web automation and testing powered by modern headless Chromium.",
            iconSystemName: "network",
            command: "npx",
            args: ["-y", "@playwright/mcp@latest"],
            requiredEnvKeys: [],
            envKeyPlaceholder: nil
        ),
        McpCatalogItem(
            id: "puppeteer",
            name: "Puppeteer Web Driver",
            category: "Developer",
            description: "Headless Chrome page inspection, rendering, and DOM management.",
            iconSystemName: "macwindow",
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-puppeteer"],
            requiredEnvKeys: [],
            envKeyPlaceholder: nil
        ),
        McpCatalogItem(
            id: "sqlite",
            name: "SQLite Database",
            category: "Developer",
            description: "Query and inspect schemas of local SQLite databases.",
            iconSystemName: "cylinder.split.1x2",
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-sqlite", "/Users"],
            requiredEnvKeys: [],
            envKeyPlaceholder: nil
        ),
        McpCatalogItem(
            id: "postgres",
            name: "PostgreSQL",
            category: "Developer",
            description: "Execute SQL queries and inspect data on PostgreSQL tables.",
            iconSystemName: "cylinder",
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-postgres"],
            requiredEnvKeys: ["POSTGRES_URL"],
            envKeyPlaceholder: "postgresql://user:pass@host:5432/db"
        ),
        McpCatalogItem(
            id: "git",
            name: "Git Tools",
            category: "Developer",
            description: "Inspect Git branches, commit history, diffs, and staging status.",
            iconSystemName: "arrow.triangle.branch",
            command: "npx",
            args: ["-y", "mcp-server-git"],
            requiredEnvKeys: [],
            envKeyPlaceholder: nil
        ),
        McpCatalogItem(
            id: "brave-search",
            name: "Brave Search API",
            category: "Featured",
            description: "Privacy-preserving independent web search API.",
            iconSystemName: "magnifyingglass",
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-brave-search"],
            requiredEnvKeys: ["BRAVE_API_KEY"],
            envKeyPlaceholder: "BSA..."
        ),
        McpCatalogItem(
            id: "notion",
            name: "Notion",
            category: "Productivity",
            description: "Search, read, and edit Notion pages and databases.",
            iconSystemName: "note.text",
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-notion"],
            requiredEnvKeys: ["NOTION_API_TOKEN"],
            envKeyPlaceholder: "ntn_..."
        ),
        McpCatalogItem(
            id: "slack",
            name: "Slack",
            category: "Productivity",
            description: "Read Slack channels, send messages, and summarize discussions.",
            iconSystemName: "bubble.left.and.bubble.right",
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-slack"],
            requiredEnvKeys: ["SLACK_BOT_TOKEN"],
            envKeyPlaceholder: "xoxb-..."
        ),
        McpCatalogItem(
            id: "linear",
            name: "Linear",
            category: "Productivity",
            description: "Track Linear projects, issues, cycles, and team roadmaps.",
            iconSystemName: "circle.grid.cross",
            command: "npx",
            args: ["-y", "linear-mcp-server"],
            requiredEnvKeys: ["LINEAR_API_KEY"],
            envKeyPlaceholder: "lin_api_..."
        ),
        McpCatalogItem(
            id: "sequential-thinking",
            name: "Sequential Thinking",
            category: "Reasoning",
            description: "Deep step-by-step reasoning for complex multi-stage tasks.",
            iconSystemName: "arrow.triangle.pull",
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-sequential-thinking"],
            requiredEnvKeys: [],
            envKeyPlaceholder: nil
        ),
        McpCatalogItem(
            id: "time",
            name: "Time & Timezone",
            category: "Productivity",
            description: "Current time, timezone conversions, and scheduling utilities.",
            iconSystemName: "clock",
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-time"],
            requiredEnvKeys: [],
            envKeyPlaceholder: nil
        ),
        McpCatalogItem(
            id: "google-maps",
            name: "Google Maps",
            category: "Productivity",
            description: "Address resolution, place lookups, and navigation directions.",
            iconSystemName: "map",
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-google-maps"],
            requiredEnvKeys: ["GOOGLE_MAPS_API_KEY"],
            envKeyPlaceholder: "AIza..."
        ),
        McpCatalogItem(
            id: "google-drive",
            name: "Google Drive",
            category: "Productivity",
            description: "List, search, read, and manage Google Drive files.",
            iconSystemName: "externaldrive",
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-gdrive"],
            requiredEnvKeys: [],
            envKeyPlaceholder: nil
        ),
        McpCatalogItem(
            id: "gmail",
            name: "Gmail",
            category: "Productivity",
            description: "Search, read, draft, and send emails via Gmail.",
            iconSystemName: "envelope.fill",
            command: "npx",
            args: ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
            requiredEnvKeys: [],
            envKeyPlaceholder: nil
        ),
        McpCatalogItem(
            id: "youtube-transcript",
            name: "YouTube Transcript",
            category: "Media",
            description: "Extract and summarize transcripts from YouTube videos.",
            iconSystemName: "play.rectangle.fill",
            command: "npx",
            args: ["-y", "@kimtaeyoon83/mcp-server-youtube-transcript"],
            requiredEnvKeys: [],
            envKeyPlaceholder: nil
        )
    ]

    // MARK: - Init & Load

    init() {
        reloadAll()
    }

    func reloadAll() {
        loadEpisodes()
        loadSkills()
        loadMcpServers()
        loadControlRecords()
    }

    // MARK: - Episodes & Analytics

    func loadEpisodes() {
        guard FileManager.default.fileExists(atPath: episodesDir.path) else {
            episodes = []
            recalculateAggregates()
            return
        }

        let dir = self.episodesDir
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            guard let files = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil) else {
                DispatchQueue.main.async {
                    self.episodes = []
                    self.recalculateAggregates()
                }
                return
            }

            let isoFractional: ISO8601DateFormatter = {
                let f = ISO8601DateFormatter()
                f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
                return f
            }()
            let isoStandard = ISO8601DateFormatter()

            var items: [StoreEpisodeItem] = []

            for file in files where file.pathExtension == "json" {
                guard let data = try? Data(contentsOf: file),
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    continue
                }

                let epId = (obj["episode_id"] as? String) ?? file.deletingPathExtension().lastPathComponent
                let app = (obj["app"] as? String) ?? "macOS"
                let rawGoal = (obj["description"] as? String) ?? ""

                // Extract concise clean goal without massive stdout/stderr log blocks
                var goal = rawGoal
                if goal.hasPrefix("User: ") {
                    let afterPrefix = String(goal.dropFirst(6))
                    if let firstLineEnd = afterPrefix.firstIndex(of: "\n") {
                        goal = String(afterPrefix[..<firstLineEnd]).trimmingCharacters(in: .whitespaces)
                    } else {
                        goal = afterPrefix.trimmingCharacters(in: .whitespaces)
                    }
                } else if let firstLineEnd = goal.firstIndex(of: "\n") {
                    let firstLine = String(goal[..<firstLineEnd]).trimmingCharacters(in: .whitespaces)
                    if !firstLine.isEmpty && firstLine.count < 300 {
                        goal = firstLine
                    }
                }
                if goal.count > 300 {
                    goal = String(goal.prefix(300)) + "..."
                }

                let rawOutcome = (obj["outcome"] as? String) ?? "failure"
                let outcome: EpisodeOutcome = (rawOutcome == "success") ? .success : ((rawOutcome == "stopped") ? .stopped : .failure)

                // Clamp retrospective length to prevent CoreText layout stalls
                var retro = obj["retrospective"] as? String
                if let r = retro, r.count > 1200 {
                    retro = String(r.prefix(1200)) + "\n... (truncated for performance)"
                }

                var date = Date()
                if let dateStr = obj["recorded_at"] as? String {
                    date = isoFractional.date(from: dateStr) ?? isoStandard.date(from: dateStr) ?? Date()
                }

                var counts = PhysicalActionsCount()
                var stepTypes: [String] = []

                if let steps = obj["steps"] as? [[String: Any]] {
                    for st in steps {
                        guard let type = st["type"] as? String else { continue }
                        stepTypes.append(type)
                        switch type {
                        case "mouse_click", "click_mark", "mouse_drag":
                            counts.clicks += 1
                        case "type_text":
                            counts.types += 1
                        case "clipboard_paste":
                            counts.pastes += 1
                        case "press_hotkey":
                            counts.hotkeys += 1
                        case "activate_app":
                            counts.apps += 1
                        default:
                            break
                        }
                    }
                }

                items.append(StoreEpisodeItem(
                    episodeId: epId,
                    app: app,
                    goal: goal,
                    outcome: outcome,
                    stepsCount: stepTypes.count,
                    recordedAt: date,
                    retrospective: retro,
                    actions: counts,
                    stepTypes: stepTypes
                ))
            }

            // Newest first
            items.sort(by: { $0.recordedAt > $1.recordedAt })

            DispatchQueue.main.async {
                self.episodes = items
                self.recalculateAggregates()
            }
        }
    }

    private func recalculateAggregates() {
        totalTasks = episodes.count
        let successes = episodes.filter { $0.outcome == .success }.count
        successRate = totalTasks > 0 ? Int(round((Double(successes) / Double(totalTasks)) * 100.0)) : 100

        var metrics = PhysicalActionsCount()
        for ep in episodes {
            metrics.clicks += ep.actions.clicks
            metrics.types += ep.actions.types
            metrics.pastes += ep.actions.pastes
            metrics.hotkeys += ep.actions.hotkeys
            metrics.apps += ep.actions.apps
        }
        actionMetrics = metrics
        totalActions = metrics.total
        distilledSkillsCount = skills.count
    }

    func clearHistory() {
        if let files = try? FileManager.default.contentsOfDirectory(at: episodesDir, includingPropertiesForKeys: nil) {
            for file in files where file.pathExtension == "json" {
                try? FileManager.default.removeItem(at: file)
            }
        }
        loadEpisodes()
        showToast("All task history cleared.")
    }

    func deleteEpisode(id: String) {
        let dir = self.episodesDir
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            guard FileManager.default.fileExists(atPath: dir.path),
                  let files = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil) else {
                return
            }

            var deleted = false
            for file in files where file.pathExtension == "json" {
                let baseName = file.deletingPathExtension().lastPathComponent
                var matches = (baseName == id)
                if !matches, let data = try? Data(contentsOf: file),
                   let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    matches = ((obj["episode_id"] as? String) == id)
                }

                if matches {
                    try? FileManager.default.removeItem(at: file)
                    deleted = true
                    break
                }
            }

            DispatchQueue.main.async {
                self.loadEpisodes()
                if deleted {
                    self.showToast("Task history entry deleted.")
                }
            }
        }
    }

    // MARK: - Skills

    func loadSkills() {
        guard FileManager.default.fileExists(atPath: skillsDir.path),
              let files = try? FileManager.default.contentsOfDirectory(at: skillsDir, includingPropertiesForKeys: nil) else {
            skills = []
            distilledSkillsCount = 0
            return
        }

        var list: [StoreSkillItem] = []
        for file in files where file.pathExtension == "json" {
            guard let data = try? Data(contentsOf: file),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                continue
            }

            let id = (obj["skill_id"] as? String) ?? file.deletingPathExtension().lastPathComponent
            let desc = (obj["description"] as? String) ?? id
            let app = (obj["app"] as? String) ?? "macOS"
            let uses = (obj["uses"] as? Int) ?? 0
            let wins = (obj["wins"] as? Int) ?? 0
            let steps = (obj["steps"] as? [String]) ?? []
            let phase = (obj["phase"] as? String) ?? "draft"

            list.append(StoreSkillItem(
                skillId: id,
                name: desc,
                app: app,
                description: desc,
                uses: uses,
                wins: wins,
                stepsCount: steps.count,
                steps: steps,
                phase: phase
            ))
        }

        list.sort(by: { $0.uses > $1.uses })
        skills = list
        distilledSkillsCount = list.count
    }

    func deleteSkill(id: String) {
        if disabledSkillIDs.contains(id) {
            disabledSkillIDs.remove(id)
            UserDefaults.standard.set(disabledSkillIDs.sorted(), forKey: "cu.disabledSkills")
        }

        let dir = self.skillsDir
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            guard FileManager.default.fileExists(atPath: dir.path),
                  let files = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil) else {
                return
            }

            var deleted = false
            for file in files where file.pathExtension == "json" {
                let baseName = file.deletingPathExtension().lastPathComponent
                var matches = (baseName == id)
                if !matches, let data = try? Data(contentsOf: file),
                   let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    matches = ((obj["skill_id"] as? String) == id)
                }

                if matches {
                    try? FileManager.default.removeItem(at: file)
                    deleted = true
                    break
                }
            }

            DispatchQueue.main.async {
                self.loadSkills()
                ExtensionsService.shared.loadLearnedSkills()
                if deleted {
                    self.showToast("Skill deleted successfully.")
                }
            }
        }
    }

    // MARK: - MCP Servers

    func loadMcpServers() {
        let extensions = ExtensionsService.shared
        extensions.loadMcpServers()
        installedMcp = Dictionary(uniqueKeysWithValues: extensions.mcpServers.map {
            ($0.name, ["command": $0.command, "args": $0.args, "env": $0.env])
        })
        if let warning = extensions.statusMessage { showToast(warning) }
    }

    func isMcpInstalled(_ id: String) -> Bool {
        installedMcp.keys.contains(where: { $0.lowercased() == id.lowercased() })
    }

    func installMcp(id: String, command: String, args: [String], env: [String: String]) {
        var root: [String: Any] = ["mcpServers": [String: Any]()]
        if FileManager.default.fileExists(atPath: mcpConfigURL.path),
           let data = try? Data(contentsOf: mcpConfigURL),
           let existing = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            root = existing
        }

        var servers = (root["mcpServers"] as? [String: Any]) ?? [:]
        var entry: [String: Any] = [
            "command": command.isEmpty ? "npx" : command,
            "args": args
        ]
        if !env.isEmpty {
            entry["env"] = env
        }
        servers[id.lowercased()] = entry
        root["mcpServers"] = servers

        do {
            let parent = mcpConfigURL.deletingLastPathComponent()
            try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true)
            let data = try JSONSerialization.data(withJSONObject: root, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: mcpConfigURL, options: .atomic)
            loadMcpServers()
            showToast("\(id) extension installed.")
        } catch {
            showToast("Installation error: \(error.localizedDescription)")
        }
    }

    func uninstallMcp(id: String) {
        guard FileManager.default.fileExists(atPath: mcpConfigURL.path),
              let data = try? Data(contentsOf: mcpConfigURL),
              var root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              var servers = root["mcpServers"] as? [String: Any] else { return }

        servers.removeValue(forKey: id.lowercased())
        root["mcpServers"] = servers

        if let updatedData = try? JSONSerialization.data(withJSONObject: root, options: [.prettyPrinted, .sortedKeys]) {
            try? updatedData.write(to: mcpConfigURL, options: .atomic)
            loadMcpServers()
            showToast("\(id) extension removed.")
        }
    }

    // MARK: - Kontrol (Approvals, Missions, Grants, Reports)

    func loadControlRecords() {
        loadApprovals()
        loadMissions()
        loadGrants()
    }

    private func loadApprovals() {
        guard FileManager.default.fileExists(atPath: approvalsDir.path),
              let files = try? FileManager.default.contentsOfDirectory(at: approvalsDir, includingPropertiesForKeys: nil) else {
            approvalRecords = []
            pendingApprovals = []
            return
        }

        var items: [PendingApprovalItem] = []
        for file in files where file.pathExtension == "json" {
            guard let data = try? Data(contentsOf: file),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
            let id = (obj["request_id"] as? String) ?? file.deletingPathExtension().lastPathComponent
            let subGoal = (obj["sub_goal"] as? String) ?? ""
            let actionType = (obj["action_type"] as? String) ?? "unknown"
            let risk = (obj["risk"] as? String) ?? "normal"
            let target = (obj["target_label"] as? String) ?? ""
            let decision = (obj["decision"] as? String) ?? "pending"
            let created = (obj["created_at"] as? String) ?? ""

            do {
                items.append(PendingApprovalItem(
                    goal: (obj["goal"] as? String) ?? "",
                    missionId: obj["mission_id"] as? String,
                    requestId: id,
                    subGoal: subGoal,
                    actionType: actionType,
                    risk: risk,
                    targetLabel: target,
                    decision: decision,
                    createdAt: created
                ))
            }
        }
        approvalRecords = items.sorted { $0.createdAt < $1.createdAt }
        pendingApprovals = approvalRecords.filter { $0.decision == "pending" }
    }

    private func loadMissions() {
        guard FileManager.default.fileExists(atPath: missionsDir.path),
              let files = try? FileManager.default.contentsOfDirectory(at: missionsDir, includingPropertiesForKeys: nil) else {
            blockedMissions = []
            return
        }

        var items: [BlockedMissionItem] = []
        for file in files where file.pathExtension == "json" {
            guard let data = try? Data(contentsOf: file),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
            let id = (obj["mission_id"] as? String) ?? file.deletingPathExtension().lastPathComponent
            let goal = (obj["goal"] as? String) ?? ""
            let app = (obj["app"] as? String) ?? ""
            let attempts = (obj["attempts"] as? Int) ?? 0
            let reason = (obj["blocked_reason"] as? String) ?? ""
            let status = (obj["status"] as? String) ?? ""

            if status == "blocked" || status == "pending" {
                items.append(BlockedMissionItem(
                    planID: (obj["plan"] as? [String: Any])?["plan_id"] as? String,
                    approvalID: obj["approval_id"] as? String,
                    missionId: id,
                    goal: goal,
                    app: app,
                    attempts: attempts,
                    blockedReason: reason,
                    status: status
                ))
            }
        }
        blockedMissions = items
    }

    private func loadGrants() {
        guard FileManager.default.fileExists(atPath: grantsDir.path),
              let files = try? FileManager.default.contentsOfDirectory(at: grantsDir, includingPropertiesForKeys: nil) else {
            activeGrants = []
            return
        }

        var items: [StandingGrantItem] = []
        for file in files where file.pathExtension == "json" {
            guard let data = try? Data(contentsOf: file),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
            let id = (obj["grant_id"] as? String) ?? file.deletingPathExtension().lastPathComponent
            let verb = (obj["verb"] as? String) ?? ""
            let app = (obj["app"] as? String) ?? ""
            let targetPattern = (obj["target_pattern"] as? String) ?? "*"
            let used = (obj["used"] as? Int) ?? 0
            let maxInvocations = (obj["max_invocations"] as? Int) ?? 0
            let exp = (obj["expires_at"] as? String) ?? ""
            let note = (obj["note"] as? String) ?? ""

            let grant = StandingGrantItem(
                grantId: id,
                verb: verb,
                app: app,
                targetPattern: targetPattern,
                used: used,
                maxInvocations: maxInvocations,
                expiresAt: exp,
                note: note
            )
            if grant.isLive {
                items.append(grant)
            }
        }
        activeGrants = items
    }

    func decideApproval(id: String, approve: Bool, always: Bool) {
        guard validateRecordID(id) else { return }
        guard !decidingApprovalIDs.contains(id) else { return }
        decidingApprovalIDs.insert(id)
        var args = [approve ? "--approve" : "--deny", id]
        if approve && always {
            args.append("--always")
        }
        Task { [weak self] in
            defer { self?.decidingApprovalIDs.remove(id) }
            do {
            let _ = try await self?.runCliCommand(args: args)
            self?.showToast(approve ? "Approval recorded." : "Request rejected.")
            self?.loadControlRecords()
            } catch { self?.showToast(error.localizedDescription) }
        }
    }

    func revokeGrant(id: String) {
        guard validateRecordID(id) else { return }
        let args = ["--revoke", id]
        Task { [weak self] in
            do {
            let _ = try await self?.runCliCommand(args: args)
            self?.showToast("Grant revoked.")
            self?.loadControlRecords()
            } catch { self?.showToast(error.localizedDescription) }
        }
    }

    func resumeMission(id: String) {
        guard validateRecordID(id) else { return }
        loadControlRecords()
        guard let mission = blockedMissions.first(where: { $0.id == id }) else {
            showToast("Mission is no longer waiting."); return
        }
        guard !pendingApprovals.contains(where: { $0.missionId == id }) else {
            showToast("Resolve this mission's pending approval first."); return
        }
        guard let state = appState, !state.runStore.isRunning else {
            showToast("Stop the active run before resuming a mission."); return
        }
        guard let planID = mission.planID, validateRecordID(planID),
            FileManager.default.fileExists(atPath: computerUseDir.appendingPathComponent("checkpoints/" + planID + ".json").path) else {
            showToast("No resumable checkpoint exists for this mission; completed steps will not be replayed."); return
        }
        state.resumeCheckpoint(mission: mission, planID: planID)
    }

    func cancelMission(id: String) {
        guard validateRecordID(id) else { return }
        showToast("Mission cancellation is not supported by the CLI.")
    }

    func generateReport(hours: Double) {
        isLoadingReport = true
        auditReportText = "Generating report (\(Int(hours))h)..."

        Task { [weak self] in
            do {
            let output = try await self?.runCliCommand(args: ["--report", "--since-hours", "\(hours)"]) ?? "Failed to retrieve report."
            self?.auditReportText = output
            } catch { self?.auditReportText = error.localizedDescription }
            self?.isLoadingReport = false
        }
    }

    private func validateRecordID(_ id: String) -> Bool {
        guard id.range(of: "^[a-z0-9][a-z0-9._-]*$", options: .regularExpression) != nil else {
            showToast("Invalid record ID: \(id)")
            return false
        }
        return true
    }

    // MARK: - CLI Runner Helper

    func runCliCommand(args: [String]) async throws -> String {
        let command = AgentRunner.resolveCommand(cliArgs: args)
        // Keep arguments separate; never route through /bin/sh.
        let proc = Process()
        proc.executableURL = command.executable
        proc.arguments = command.args
        proc.currentDirectoryURL = AgentRunner.repoRoot()

        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
        proc.environment = env

        let outPipe = Pipe()
        let errPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = errPipe

        do {
            try proc.run()
            async let outData = Task.detached { outPipe.fileHandleForReading.readDataToEndOfFile() }.value
            async let errData = Task.detached { errPipe.fileHandleForReading.readDataToEndOfFile() }.value
            await Task.detached { proc.waitUntilExit() }.value
            let stdout = String(data: await outData, encoding: .utf8) ?? ""
            let stderr = String(data: await errData, encoding: .utf8) ?? ""

            guard proc.terminationStatus == 0 else {
                throw NSError(domain: "ComputerUseCLI", code: Int(proc.terminationStatus),
                    userInfo: [NSLocalizedDescriptionKey: "CLI failed (\(proc.terminationStatus)): \(stderr)\(stdout)"])
            }
            if !stdout.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                return stdout
            }
            return stderr.isEmpty ? "Command completed." : stderr
        } catch {
            throw error
        }
    }

    func showToast(_ message: String) {
        statusToast = message
        DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) { [weak self] in
            if self?.statusToast == message {
                self?.statusToast = nil
            }
        }
    }
}
