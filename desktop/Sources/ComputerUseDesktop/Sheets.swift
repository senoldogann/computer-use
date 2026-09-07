import AppKit
import SwiftUI

// MARK: - Statistics Sheet

/// Statistics sheet: session counters, average latency, live FPS, and
/// hardware summary. Counters persist in `AppState`.
struct StatsSheet: View {
    @ObservedObject var state: AppState
    @ObservedObject var capture = CaptureService.shared
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            SheetHeader(title: "Statistics", dismiss: dismiss)

            LazyVGrid(
                columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10
            ) {
                StatTile(label: "Total runs", value: "\(state.runStore.runCount)")
                StatTile(
                    label: "Success / Failed",
                    value: "\(state.runStore.succeededCount) / \(state.runStore.failedCount)")
                StatTile(
                    label: "Average latency",
                    value: String(format: "%.1f s", state.runStore.averageLatency))
                StatTile(
                    label: "Viewport FPS",
                    value: capture.isStreaming ? "\(capture.fps)" : "—")
            }

            Divider().background(Theme.borderOverlay)

            VStack(alignment: .leading, spacing: 6) {
                Label("Hardware", systemImage: "cpu.fill")
                    .font(.system(size: 12.5, weight: .semibold))
                    .foregroundStyle(Theme.textPrimary)
                Text(SystemStatus.hardwareLine())
                    .font(.system(size: 12, design: .monospaced))
                    .foregroundStyle(Theme.textMuted)
                Text("sensor 30 Hz · tick \(state.bridge.sensorTicks)")
                    .font(.system(size: 12, design: .monospaced))
                    .foregroundStyle(Theme.textMuted)
            }

            Spacer(minLength: 0)
        }
        .padding(20)
        .frame(width: 440, height: 280)
        .background(Theme.card)
    }
}

// MARK: - Quick Model Palette (Cmd+K)

/// Quick model switcher palette: opened via `Cmd+K`, closed with `Escape`.
struct ModelPaletteSheet: View {
    @ObservedObject var state: AppState
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Switch model")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Theme.textMuted)
                .padding(.horizontal, 4)
            ForEach(AgentModel.all, id: \.self) { model in
                Button {
                    state.setSelectedModel(model)
                    dismiss()
                } label: {
                    HStack {
                        Image(systemName: "cpu")
                            .font(.system(size: 12))
                            .foregroundStyle(Theme.textMuted)
                        Text(model)
                            .font(.system(size: 13, design: .monospaced))
                            .foregroundStyle(Theme.textPrimary)
                        Spacer()
                        if state.selectedModel == model {
                            Image(systemName: "checkmark")
                                .foregroundStyle(Theme.success)
                                .font(.system(size: 12, weight: .semibold))
                        }
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 9)
                    .background(
                        RoundedRectangle(cornerRadius: 6, style: .continuous)
                            .fill(
                                state.selectedModel == model
                                    ? Theme.accent.opacity(0.15) : Theme.cardElevated)
                    )
                }
                .buttonStyle(.plain)
                .hoverPointer(radius: 6)
            }
        }
        .padding(16)
        .frame(width: 320)
        .background(Theme.card)
    }
}

// MARK: - Shared sheet components

private struct SheetHeader: View {
    let title: String
    let dismiss: DismissAction

    var body: some View {
        HStack {
            Text(title)
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(Theme.textPrimary)
            Spacer()
            Button {
                dismiss()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Theme.textMuted)
                    .frame(width: 22, height: 22)
            }
            .buttonStyle(.plain)
            .hoverPointer(radius: 4)
        }
    }
}

private struct StatTile: View {
    let label: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label)
                .font(.system(size: 11))
                .foregroundStyle(Theme.textMuted)
            Text(value)
                .font(.system(size: 16, weight: .semibold, design: .monospaced))
                .foregroundStyle(Theme.textPrimary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Theme.cardElevated)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(Theme.borderOverlay, lineWidth: 1)
        )
    }
}

// MARK: - Real Machine Status Probe

enum SystemStatus {
    static func openAIKeyStatus() -> (text: String, ok: Bool) {
        if let key = ProcessInfo.processInfo.environment["OPENAI_API_KEY"],
            !key.trimmingCharacters(in: .whitespaces).isEmpty
        {
            return ("Active · configured", true)
        }
        let envFile = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".computeruse/env")
        guard let content = try? String(contentsOf: envFile, encoding: .utf8) else {
            return ("Not configured", false)
        }
        for rawLine in content.split(separator: "\n") {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            guard !line.hasPrefix("#"), line.hasPrefix("OPENAI_API_KEY=") else { continue }
            let value = line.dropFirst("OPENAI_API_KEY=".count)
                .trimmingCharacters(in: .whitespaces)
                .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
            if !value.isEmpty {
                return ("Active · configured", true)
            }
        }
        return ("Not configured", false)
    }

    static func accessibilityStatus() -> (text: String, ok: Bool) {
        let trusted = AXIsProcessTrusted()
        return (trusted ? "Granted" : "Pending authorization", trusted)
    }

    static func screenRecordingStatus() -> (text: String, ok: Bool) {
        let granted = CGPreflightScreenCaptureAccess()
        return (granted ? "Granted" : "Pending authorization", granted)
    }

    static func hardwareLine() -> String {
        let cpu = ProcessInfo.processInfo.processorCount
        let memGB = Double(ProcessInfo.processInfo.physicalMemory) / 1_073_741_824.0
        return "\(cpu) cores · \(String(format: "%.0f", memGB)) GB memory"
    }
}
