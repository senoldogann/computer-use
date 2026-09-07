import AppKit
import SwiftUI

/// Composer: hero or compact docked.
/// - Big multiline TextField
/// - Model picker with official OpenAI SVG icon + Autonomy level picker (custom popover) + Stop / Send action
/// - Hover effects with clear background color transition on interactive pickers
struct ComposerView: View {
    @ObservedObject var state: AppState
    let compact: Bool
    @State private var isManuallyExpanded: Bool = false

    private var shouldBeExpanded: Bool {
        !compact || isManuallyExpanded || !state.composerText.isEmpty
    }

    var body: some View {
        if shouldBeExpanded {
            FullComposer(
                state: state,
                canCollapse: compact,
                onCollapse: {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        isManuallyExpanded = false
                    }
                }
            )
            .transition(.opacity.combined(with: .scale(scale: 0.99)))
        } else {
            CompactComposer(
                state: state,
                onActivate: {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        isManuallyExpanded = true
                    }
                }
            )
            .transition(.opacity.combined(with: .scale(scale: 0.99)))
        }
    }
}

// MARK: - Unified Action Button (Send / Stop)

struct ComposerActionButton: View {
    @ObservedObject var state: AppState
    let canSend: Bool
    var size: CGFloat = 26

    var body: some View {
        Button {
            if state.runStore.isRunning {
                state.emergencyStop()
            } else if canSend {
                state.sendComposer()
            }
        } label: {
            Group {
                if state.runStore.isRunning {
                    Image(systemName: "stop.fill")
                        .font(.system(size: size * 0.38, weight: .bold))
                        .foregroundStyle(Theme.canvas)
                        .frame(width: size, height: size)
                        .background(Theme.accent)
                        .clipShape(Circle())
                } else {
                    Image(systemName: "arrow.up")
                        .font(.system(size: size * 0.42, weight: .bold))
                        .foregroundStyle(canSend ? Theme.canvas : Theme.textMuted.opacity(0.5))
                        .frame(width: size, height: size)
                        .background(canSend ? Theme.accent : Theme.card)
                        .clipShape(Circle())
                }
            }
        }
        .buttonStyle(.plain)
        .pointingHand()
        .disabled(!state.runStore.isRunning && !canSend)
        .help(state.runStore.isRunning ? "Stop task (Esc / Cmd+.)" : "Run (Cmd+Enter)")
    }
}

// MARK: - Official OpenAI / ChatGPT SVG Logo

struct OpenAILogoView: View {
    var size: CGFloat = 11

    private static let image: NSImage? = {
        let svg = """
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="black">
          <path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z"/>
        </svg>
        """
        guard let data = svg.data(using: .utf8), let img = NSImage(data: data) else { return nil }
        img.isTemplate = true
        return img
    }()

    var body: some View {
        if let image = Self.image {
            Image(nsImage: image)
                .resizable()
                .renderingMode(.template)
                .aspectRatio(contentMode: .fit)
                .frame(width: size, height: size)
        }
    }
}

// MARK: - Hero (empty state)

private struct FullComposer: View {
    @ObservedObject var state: AppState
    var canCollapse: Bool = false
    var onCollapse: (() -> Void)? = nil
    @FocusState private var isFocused: Bool

    private var canSend: Bool {
        !state.composerText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        VStack(spacing: 0) {
            TextEditor(text: $state.composerText)
                .font(.system(size: 14))
                .scrollContentBackground(.hidden)
                .background(Color.clear)
                .focused($isFocused)
                .frame(minHeight: 74, maxHeight: 170)
                .padding(.horizontal, 14)
                .padding(.top, 12)
                .padding(.bottom, 6)
                .overlay(alignment: .topLeading) {
                    if state.composerText.isEmpty {
                        Text(state.runStore.isRunning ? "Type next instruction while agent runs..." : "What would you like to build? (e.g. Open Chrome, fill out a form...)")
                            .font(.system(size: 14))
                            .foregroundStyle(Theme.textMuted.opacity(0.7))
                            .padding(.horizontal, 18)
                            .padding(.top, 12)
                            .allowsHitTesting(false)
                    }
                }
                .overlay(alignment: .topTrailing) {
                    if canCollapse {
                        Button {
                            onCollapse?()
                        } label: {
                            Image(systemName: "chevron.down")
                                .font(.system(size: 10, weight: .semibold))
                                .foregroundStyle(Theme.textMuted)
                                .padding(6)
                                .background(Color.white.opacity(0.04))
                                .clipShape(Circle())
                        }
                        .buttonStyle(.plain)
                        .pointingHand()
                        .padding(.top, 8)
                        .padding(.trailing, 10)
                        .help("Collapse")
                    }
                }
                .onKeyPress(keys: [.return]) { press in
                    guard press.modifiers.contains(.command) else { return .ignored }
                    if canSend {
                        state.sendComposer()
                        onCollapse?()
                    }
                    return .handled
                }

            HStack(spacing: 8) {
                ModelPickerButton(state: state, compact: false)

                AutonomyPickerButton(state: state, compact: false)

                TrustToggleButton(state: state, compact: false)

                Spacer()

                PreviewPlanButton(state: state)

                ComposerActionButton(state: state, canSend: canSend, size: 26)
            }
            .padding(.horizontal, 12)
            .padding(.bottom, 10)
        }
        .cardStyle(cornerRadius: 14)
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(
                    isFocused ? Theme.borderActive : Theme.borderOverlay,
                    lineWidth: 1
                )
        )
        .shadow(color: Theme.canvas.opacity(0.6), radius: 12, y: 4)
        .onAppear { isFocused = true }
        .onChange(of: state.focusComposerCounter) { isFocused = true }
    }
}

// MARK: - Compact (docked at bottom)

private struct CompactComposer: View {
    @ObservedObject var state: AppState
    var onActivate: (() -> Void)? = nil
    @FocusState private var isFocused: Bool

    private var canSend: Bool {
        !state.composerText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        VStack(spacing: 6) {
            HStack(spacing: 8) {
                TextField(
                    state.runStore.isRunning
                        ? "Type next instruction while agent runs... (Enter to send)"
                        : "Type next step... (Enter to send)",
                    text: $state.composerText
                )
                    .textFieldStyle(.plain)
                    .font(.system(size: 13))
                    .foregroundStyle(Theme.textPrimary)
                    .focused($isFocused)
                    .onChange(of: state.composerText) {
                        if !state.composerText.isEmpty {
                            onActivate?()
                        }
                    }
                    .onSubmit {
                        if canSend { state.sendComposer() }
                    }

                PreviewPlanButton(state: state)

                ComposerActionButton(state: state, canSend: canSend, size: 24)
            }
            .padding(.horizontal, 14)
            .frame(height: 38)
            .background(
                Capsule().fill(Theme.card)
            )
            .clipShape(Capsule())
            .overlay(
                Capsule().stroke(
                    isFocused ? Theme.borderActive : Theme.borderOverlay,
                    lineWidth: 1
                )
            )
            .shadow(color: Theme.canvas.opacity(0.6), radius: 8, y: 3)
            .contentShape(Rectangle())
            .onTapGesture {
                onActivate?()
            }

            HStack(spacing: 8) {
                ModelPickerButton(state: state, compact: true)

                Text("·")
                    .foregroundStyle(Theme.textMuted.opacity(0.5))

                AutonomyPickerButton(state: state, compact: true)

                Text("·")
                    .foregroundStyle(Theme.textMuted.opacity(0.5))

                TrustToggleButton(state: state, compact: true)
            }
            .padding(.top, 2)
        }
    }
}

// MARK: - Model Picker Button (same Button + popover pattern as Autonomy)

struct ModelPickerButton: View {
    @ObservedObject var state: AppState
    let compact: Bool
    @State private var showPopover = false
    @State private var isHovered = false

    var body: some View {
        Button {
            showPopover.toggle()
        } label: {
            HStack(spacing: 6) {
                OpenAILogoView(size: compact ? 10 : 11)
                    .foregroundStyle(compact ? (isHovered ? Theme.textPrimary : Theme.textMuted) : Theme.textPrimary)

                Text(state.selectedModel)
                    .font(.system(size: compact ? 11 : 11.5, weight: .medium))
                    .foregroundStyle(compact ? (isHovered ? Theme.textPrimary : Theme.textMuted) : Theme.textPrimary)

                Image(systemName: "chevron.down")
                    .font(.system(size: compact ? 7 : 8, weight: .bold))
                    .foregroundStyle(isHovered ? Theme.textPrimary : Theme.textMuted)
            }
            .padding(.horizontal, compact ? 7 : 9)
            .padding(.vertical, compact ? 3.5 : 5)
            .background(isHovered ? Theme.cardElevated : Theme.card)
            .clipShape(Capsule())
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .fixedSize()
        .onHover { hovering in
            withAnimation(.easeInOut(duration: 0.15)) {
                isHovered = hovering
            }
            if hovering {
                NSCursor.pointingHand.push()
            } else {
                NSCursor.pop()
            }
        }
        .onDisappear {
            if isHovered {
                NSCursor.pop()
            }
        }
        .popover(isPresented: $showPopover) {
            ModelMenuPopover(state: state, isPresented: $showPopover)
        }
    }
}

private struct ModelMenuPopover: View {
    @ObservedObject var state: AppState
    @Binding var isPresented: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(AgentModel.all, id: \.self) { model in
                ModelRowButton(
                    model: model,
                    isSelected: state.selectedModel == model
                ) {
                    state.setSelectedModel(model)
                    isPresented = false
                }
            }
        }
        .padding(8)
        .frame(width: 240)
        .background(Theme.card)
    }
}

private struct ModelRowButton: View {
    let model: String
    let isSelected: Bool
    let action: () -> Void

    @State private var isHovered = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                OpenAILogoView(size: 12)
                    .foregroundStyle(isSelected ? Theme.textPrimary : Theme.textMuted)
                    .frame(width: 18, height: 18)

                Text(model)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(Theme.textPrimary)

                Spacer(minLength: 0)

                if isSelected {
                    Image(systemName: "checkmark")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Theme.success)
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            .background(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(isSelected ? Theme.cardElevated : (isHovered ? Theme.cardElevated.opacity(0.6) : Color.clear))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(isSelected ? Theme.borderOverlay : Color.clear, lineWidth: 1)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .hoverPointer(bg: nil, radius: 8)
        .onHover { isHovered = $0 }
        .animation(.easeInOut(duration: 0.15), value: isHovered)
    }
}

// MARK: - Autonomy Picker Button (Custom SVG/SF Icons, Reliable Click & Hover)

struct AutonomyPickerButton: View {
    @ObservedObject var state: AppState
    let compact: Bool
    @State private var showPopover = false
    @State private var isHovered = false

    var body: some View {
        Button {
            showPopover.toggle()
        } label: {
            HStack(spacing: 5) {
                Image(systemName: state.autonomyLevel.iconName)
                    .font(.system(size: compact ? 10 : 11.5, weight: .medium))
                    .foregroundStyle(compact ? (isHovered ? Theme.textPrimary : Theme.textMuted) : Theme.textPrimary)

                Text(state.autonomyLevel.rawValue)
                    .font(.system(size: compact ? 11 : 11.5, weight: .medium))
                    .foregroundStyle(compact ? (isHovered ? Theme.textPrimary : Theme.textMuted) : Theme.textPrimary)

                Image(systemName: "chevron.down")
                    .font(.system(size: compact ? 7 : 8, weight: .bold))
                    .foregroundStyle(isHovered ? Theme.textPrimary : Theme.textMuted)
            }
            .padding(.horizontal, compact ? 7 : 9)
            .padding(.vertical, compact ? 3.5 : 5)
            .background(isHovered ? Theme.cardElevated : Theme.card)
            .clipShape(Capsule())
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .fixedSize()
        .onHover { hovering in
            withAnimation(.easeInOut(duration: 0.15)) {
                isHovered = hovering
            }
            if hovering {
                NSCursor.pointingHand.push()
            } else {
                NSCursor.pop()
            }
        }
        .onDisappear {
            if isHovered {
                NSCursor.pop()
            }
        }
        .popover(isPresented: $showPopover) {
            AutonomyMenuPopover(state: state, isPresented: $showPopover)
        }
    }
}

private struct AutonomyMenuPopover: View {
    @ObservedObject var state: AppState
    @Binding var isPresented: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(AutonomyLevel.allCases) { level in
                AutonomyRowButton(
                    level: level,
                    isSelected: state.autonomyLevel == level
                ) {
                    state.autonomyLevel = level
                    isPresented = false
                }
            }
        }
        .padding(8)
        .frame(width: 330)
        .background(Theme.card)
    }
}

private struct AutonomyRowButton: View {
    let level: AutonomyLevel
    let isSelected: Bool
    let action: () -> Void

    @State private var isHovered = false

    var body: some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: level.iconName)
                    .font(.system(size: 14, weight: .regular))
                    .foregroundStyle(isSelected ? Theme.textPrimary : Theme.textMuted)
                    .frame(width: 18, height: 18)
                    .padding(.top, 2)

                VStack(alignment: .leading, spacing: 2) {
                    Text(level.rawValue)
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(Theme.textPrimary)

                    Text(level.subtitle)
                        .font(.system(size: 11))
                        .foregroundStyle(Theme.textMuted.opacity(0.85))
                        .fixedSize(horizontal: false, vertical: true)
                }

                Spacer(minLength: 0)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            .background(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(isSelected ? Theme.cardElevated : (isHovered ? Theme.cardElevated.opacity(0.6) : Color.clear))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(isSelected ? Theme.borderOverlay : Color.clear, lineWidth: 1)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .hoverPointer(bg: nil, radius: 8)
        .onHover { isHovered = $0 }
        .animation(.easeInOut(duration: 0.15), value: isHovered)
    }
}


// MARK: - Trust Mode Toggle Button (from menu.html)

struct TrustToggleButton: View {
    @ObservedObject var state: AppState
    let compact: Bool
    @State private var isHovered = false

    var body: some View {
        Button {
            state.toggleTrustMode()
        } label: {
            HStack(spacing: compact ? 4 : 5) {
                Image(systemName: state.trustMode ? "checkmark.shield.fill" : "shield")
                    .font(.system(size: compact ? 10.5 : 11.5))
                    .foregroundStyle(state.trustMode ? Theme.success : Theme.textMuted)

                Text(state.trustMode ? "Trust: ON" : "Trust: OFF")
                    .font(.system(size: compact ? 11 : 12, weight: state.trustMode ? .semibold : .medium))
                    .foregroundStyle(state.trustMode ? Theme.success : Theme.textMuted)
            }
            .padding(.horizontal, compact ? 7 : 9)
            .padding(.vertical, compact ? 3.5 : 5)
            .background(
                state.trustMode
                    ? Theme.success.opacity(isHovered ? 0.22 : 0.14)
                    : (isHovered ? Theme.cardElevated : Theme.card)
            )
            .clipShape(Capsule())
            .overlay(
                Capsule()
                    .stroke(
                        state.trustMode
                            ? Theme.success.opacity(0.40)
                            : (isHovered ? Theme.borderOverlay : Color.clear),
                        lineWidth: 1
                    )
            )
        }
        .buttonStyle(.plain)
        .pointingHand()
        .onHover { isHovered = $0 }
        .animation(.easeInOut(duration: 0.15), value: isHovered)
        .help(
            state.trustMode
                ? "Trust Mode ON (--yes): Steps requiring confirmation are auto-approved. Click to disable."
                : "Trust Mode OFF: Sensitive steps require confirmation. Click to enable uninterrupted autonomy."
        )
    }
}

private struct PreviewPlanButton: View {
    @ObservedObject var state: AppState
    var body: some View {
        Button("Preview Plan", systemImage: "checklist") { state.previewComposer() }
            .font(.system(size: 10)).foregroundStyle(Theme.textSecondary)
            .buttonStyle(.plain).hoverPointer(radius: 5)
            .disabled(state.runStore.isRunning || state.composerText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
    }
}
