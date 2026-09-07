import Foundation
import SwiftUI

/// Data models for Model Context Protocol (MCP) servers configured on the user's system.
struct InstalledMcpServer: Identifiable, Sendable {
    var id: String { name }
    let name: String
    let command: String
    let args: [String]
    let env: [String: String]
}

/// Catalog item for 1-click curated MCP installation.
struct McpCatalogPreset: Identifiable, Sendable {
    let id: String
    let name: String
    let description: String
    let icon: String
    let command: String
    let args: [String]
    let defaultEnv: [String: String]
    let envKeyPlaceholder: String?
}

/// Model for distilled parametric skills learned by the agent.
struct LearnedSkillItem: Identifiable, Sendable {
    var id: String { skillId }
    let skillId: String
    let app: String
    let description: String
    let uses: Int
    let wins: Int
    let steps: [String]
    let phase: String

    var winRate: String {
        guard uses > 0 else { return "—" }
        let pct = Int((Double(wins) / Double(uses)) * 100)
        return "\(pct)%"
    }
}

/// Manages MCP servers from ~/.computeruse/mcp.json and learned skills from ~/.computeruse/skills/.
@MainActor
final class ExtensionsService: ObservableObject {
    static let shared = ExtensionsService()

    @Published private(set) var mcpServers: [InstalledMcpServer] = []
    @Published private(set) var learnedSkills: [LearnedSkillItem] = []
    @Published var statusMessage: String?

    private let mcpConfigURL: URL = {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".computeruse")
            .appendingPathComponent("mcp.json")
    }()

    private let skillsDirURL: URL = {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".computeruse")
            .appendingPathComponent("skills")
    }()

    // Curated 1-click catalog matching computeruse/mcp/catalog.py
    let catalogPresets: [McpCatalogPreset] = [
        McpCatalogPreset(
            id: "filesystem",
            name: "Local Filesystem",
            description: "Read, write and manage files and directories securely across macOS.",
            icon: "folder.fill",
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-filesystem", "/Users"],
            defaultEnv: [:],
            envKeyPlaceholder: nil
        ),
        McpCatalogPreset(
            id: "memory",
            name: "Knowledge Graph Memory",
            description: "Persistent long-term knowledge graph memory across agent runs.",
            icon: "brain.head.profile",
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-memory"],
            defaultEnv: [:],
            envKeyPlaceholder: nil
        ),
        McpCatalogPreset(
            id: "tavily",
            name: "Tavily AI Search",
            description: "Real-time AI-optimized web search engine for high quality content.",
            icon: "globe",
            command: "npx",
            args: ["-y", "tavily-mcp@latest"],
            defaultEnv: ["TAVILY_API_KEY": ""],
            envKeyPlaceholder: "TAVILY_API_KEY"
        ),
        McpCatalogPreset(
            id: "exa",
            name: "Exa Neural Search",
            description: "Neural web search API designed specifically for autonomous LLM agents.",
            icon: "sparkle.magnifyingglass",
            command: "npx",
            args: ["-y", "exa-mcp-server"],
            defaultEnv: ["EXA_API_KEY": ""],
            envKeyPlaceholder: "EXA_API_KEY"
        ),
        McpCatalogPreset(
            id: "github",
            name: "GitHub Developer",
            description: "Inspect repositories, file issues, and search code on GitHub.",
            icon: "chevron.left.forwardslash.chevron.right",
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-github"],
            defaultEnv: ["GITHUB_PERSONAL_ACCESS_TOKEN": ""],
            envKeyPlaceholder: "GITHUB_PERSONAL_ACCESS_TOKEN"
        ),
        McpCatalogPreset(
            id: "playwright",
            name: "Playwright Headless",
            description: "Browser automation and web scraping through headless Chromium.",
            icon: "network",
            command: "npx",
            args: ["-y", "@playwright/mcp@latest"],
            defaultEnv: [:],
            envKeyPlaceholder: nil
        ),
        McpCatalogPreset(
            id: "puppeteer",
            name: "Puppeteer Web Driver",
            description: "Web page inspection, rendering, and DOM manipulation via Puppeteer.",
            icon: "macwindow",
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-puppeteer"],
            defaultEnv: [:],
            envKeyPlaceholder: nil
        ),
        McpCatalogPreset(
            id: "brave-search",
            name: "Brave Search API",
            description: "Privacy-focused web search API with ranked search results.",
            icon: "magnifyingglass",
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-brave-search"],
            defaultEnv: ["BRAVE_API_KEY": ""],
            envKeyPlaceholder: "BRAVE_API_KEY"
        )
    ]

    init() {
        reloadAll()
    }

    func reloadAll() {
        loadMcpServers()
        loadLearnedSkills()
    }

    // MARK: - MCP Management

    func loadMcpServers() {
        guard FileManager.default.fileExists(atPath: mcpConfigURL.path) else {
            mcpServers = []
            return
        }

        do {
            let data = try Data(contentsOf: mcpConfigURL)
            guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let serversDict = root["mcpServers"] as? [String: [String: Any]] else {
                mcpServers = []
                statusMessage = "Invalid mcp.json: expected mcpServers object"
                return
            }

            var list: [InstalledMcpServer] = []
            var invalid: [String] = []
            for (key, dict) in serversDict {
                guard let cmd = dict["command"] as? String,
                    !cmd.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                    dict["args"] == nil || dict["args"] is [String],
                    dict["env"] == nil || dict["env"] is [String: String] else {
                    invalid.append(key)
                    continue
                }
                let args = dict["args"] as? [String] ?? []
                let env = dict["env"] as? [String: String] ?? [:]
                list.append(InstalledMcpServer(name: key, command: cmd, args: args, env: env))
            }
            statusMessage = invalid.isEmpty ? nil : "Invalid MCP entries: " + invalid.sorted().joined(separator: ", ")
            mcpServers = list.sorted(by: { $0.name.lowercased() < $1.name.lowercased() })
        } catch {
            mcpServers = []
            statusMessage = "Failed to parse mcp.json: \(error.localizedDescription)"
        }
    }

    func installMcpServer(name: String, command: String, args: [String], env: [String: String]) -> Bool {
        let trimmedName = name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !trimmedName.isEmpty else { return false }

        var rootDict: [String: Any] = ["mcpServers": [String: Any]()]
        if FileManager.default.fileExists(atPath: mcpConfigURL.path),
           let data = try? Data(contentsOf: mcpConfigURL),
           let existing = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            rootDict = existing
        }

        var servers = (rootDict["mcpServers"] as? [String: Any]) ?? [:]
        var serverEntry: [String: Any] = [
            "command": command.isEmpty ? "npx" : command,
            "args": args
        ]
        if !env.isEmpty {
            serverEntry["env"] = env
        }
        servers[trimmedName] = serverEntry
        rootDict["mcpServers"] = servers

        do {
            let parent = mcpConfigURL.deletingLastPathComponent()
            try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true)
            let data = try JSONSerialization.data(withJSONObject: rootDict, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: mcpConfigURL, options: .atomic)
            loadMcpServers()
            return true
        } catch {
            statusMessage = "Failed to save MCP server: \(error.localizedDescription)"
            return false
        }
    }

    func removeMcpServer(name: String) {
        guard FileManager.default.fileExists(atPath: mcpConfigURL.path),
              let data = try? Data(contentsOf: mcpConfigURL),
              var root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              var servers = root["mcpServers"] as? [String: Any] else { return }

        servers.removeValue(forKey: name)
        root["mcpServers"] = servers

        if let updatedData = try? JSONSerialization.data(withJSONObject: root, options: [.prettyPrinted, .sortedKeys]) {
            try? updatedData.write(to: mcpConfigURL, options: .atomic)
            loadMcpServers()
        }
    }

    func isPresetInstalled(_ preset: McpCatalogPreset) -> Bool {
        mcpServers.contains(where: { $0.name.lowercased() == preset.id.lowercased() })
    }

    // MARK: - Learned Skills Management

    func loadLearnedSkills() {
        guard FileManager.default.fileExists(atPath: skillsDirURL.path) else {
            learnedSkills = []
            return
        }

        guard let files = try? FileManager.default.contentsOfDirectory(at: skillsDirURL, includingPropertiesForKeys: nil) else {
            return
        }

        var items: [LearnedSkillItem] = []
        for file in files where file.pathExtension == "json" {
            guard let data = try? Data(contentsOf: file),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }

            let skillId = (obj["skill_id"] as? String) ?? file.deletingPathExtension().lastPathComponent
            let app = (obj["app"] as? String) ?? "macOS"
            let desc = (obj["description"] as? String) ?? "Learned workflow"
            let uses = (obj["uses"] as? Int) ?? 0
            let wins = (obj["wins"] as? Int) ?? 0
            let steps = (obj["steps"] as? [String]) ?? []
            let phase = (obj["phase"] as? String) ?? "draft"

            items.append(LearnedSkillItem(
                skillId: skillId,
                app: app,
                description: desc,
                uses: uses,
                wins: wins,
                steps: steps,
                phase: phase
            ))
        }

        learnedSkills = items.sorted(by: { $0.uses > $1.uses })
    }

    func deleteLearnedSkill(id: String) {
        do {
            let files = try FileManager.default.contentsOfDirectory(at: skillsDirURL, includingPropertiesForKeys: nil)
            guard let file = files.first(where: { $0.pathExtension == "json" && $0.deletingPathExtension().lastPathComponent == id }) else {
                throw CocoaError(.fileNoSuchFile)
            }
            try FileManager.default.removeItem(at: file)
            loadLearnedSkills()
        } catch { statusMessage = error.localizedDescription }
    }
}
