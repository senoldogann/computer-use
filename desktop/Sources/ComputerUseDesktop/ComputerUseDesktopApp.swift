import AppKit
import SwiftUI

@main
struct ComputerUseDesktopApp: App {
    @StateObject private var appState = AppState()

    init() {
        // Load OPENAI_API_KEY from ~/.computeruse/env into the process environment
        Self.loadEnvFile()

        // Enable click-through on macOS hiddenTitleBar chrome so custom buttons receive clicks
        TitlebarHitTestHelper.installTitlebarHitTestPassThrough()

        // Lock window appearance to dark
        NSApp?.appearance = NSAppearance(named: .darkAqua)
    }

    private static func loadEnvFile() {
        let envFile = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".computeruse/env")
        guard FileManager.default.fileExists(atPath: envFile.path) else { return }
        do {
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: envFile.path)
            let content = try String(contentsOf: envFile, encoding: .utf8)
            for rawLine in content.split(separator: "\n") {
                let line = rawLine.trimmingCharacters(in: .whitespaces)
                guard !line.hasPrefix("#"), line.hasPrefix("OPENAI_API_KEY=") else { continue }
                let value = line.dropFirst("OPENAI_API_KEY=".count)
                    .trimmingCharacters(in: .whitespaces)
                    .trimmingCharacters(in: CharacterSet(charactersIn: "\"\'"))
                if !value.isEmpty {
                    setenv("OPENAI_API_KEY", value, 1)
                }
            }
        } catch {
            let alert = NSAlert()
            alert.messageText = "Unable to load API key file securely"
            alert.informativeText = error.localizedDescription
            alert.runModal()
        }
    }

    var body: some Scene {
        WindowGroup {
            ContentView(state: appState)
                .preferredColorScheme(.dark)
                .ignoresSafeArea()
                .frame(minWidth: 1080, minHeight: 660)
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1360, height: 820)
        .commands {
            CommandGroup(replacing: .appSettings) {
                Button("Settings...") {
                    NotificationCenter.default.post(
                        name: UIIntent.openSettings, object: nil)
                }
                .keyboardShortcut(",", modifiers: .command)
            }

            CommandMenu("View") {
                Button("Toggle Left Sidebar") {
                    NotificationCenter.default.post(
                        name: UIIntent.toggleSidebar, object: nil)
                }
                .keyboardShortcut("b", modifiers: .command)
                Button("Toggle Right Live Viewport") {
                    NotificationCenter.default.post(
                        name: UIIntent.toggleRightPanel, object: nil)
                }
                .keyboardShortcut("]", modifiers: [.command, .option])
                Divider()
                Button("Quick Model Switcher") {
                    NotificationCenter.default.post(
                        name: UIIntent.openModelPalette, object: nil)
                }
                .keyboardShortcut("k", modifiers: .command)
            }

            CommandMenu("Task") {
                Button("Command Palette") {
                    NotificationCenter.default.post(name: UIIntent.openCommandPalette, object: nil)
                }.keyboardShortcut("p", modifiers: [.command, .shift])
                Button("New Task") {
                    NotificationCenter.default.post(
                        name: UIIntent.newTask, object: nil)
                }
                .keyboardShortcut("n", modifiers: .command)
                Button("Emergency Stop (SIGINT)") {
                    NotificationCenter.default.post(
                        name: UIIntent.emergencyStop, object: nil)
                }
                .keyboardShortcut(".", modifiers: .command)
            }
        }
    }
}
