import SwiftUI

/// Right panel: live ScreenCaptureKit viewport + telemetry HUD.
/// Collapsible via Cmd+J or toolbar button.
struct RightPanelView: View {
    @ObservedObject var state: AppState
    @ObservedObject var capture = CaptureService.shared

    var body: some View {
        VStack(spacing: 12) {
            topBar
            streamCard
            hudCard
            Spacer(minLength: 0)
        }
        .padding(12)
        .frame(minWidth: 280, idealWidth: 320, maxWidth: 400)
        .background(Theme.canvas)
        .overlay(alignment: .leading) {
            Theme.borderOverlay.frame(width: 1)
        }
        .onAppear {
            capture.targetFps = state.targetFps
            capture.start()
        }
        .onDisappear { capture.stop() }
        .onChange(of: state.targetFps) { _, newFps in
            capture.targetFps = newFps
        }
    }

    private var topBar: some View {
        HStack(spacing: 8) {
            LiveBadge(capture: capture)
            Spacer()
            Text(state.bridge.driverBackend.isEmpty ? "Driver" : state.bridge.driverBackend)
                .font(.system(size: 11, weight: .medium, design: .monospaced))
                .foregroundStyle(Theme.textMuted)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(Theme.card)
                .clipShape(Capsule())
                .overlay(Capsule().stroke(Theme.borderOverlay, lineWidth: 1))

            NativeTitleBarButton(
                iconName: "sidebar.right",
                isActive: true,
                tooltip: "Close live viewport (Cmd+])"
            ) {
                withAnimation(.easeInOut(duration: 0.2)) {
                    state.panelsStore.isRightPanelVisible = false
                }
            }
            .frame(width: 28, height: 28)
        }
        .frame(height: 40)
    }

    // MARK: - Live stream card

    private var streamCard: some View {
        VStack(spacing: 6) {
            ZStack {
                Theme.canvas

                if capture.isStreaming {
                    StreamFrameView(store: capture.frames)
                } else if !capture.hasPermission {
                    PermissionPrompt(capture: capture)
                } else {
                    FallbackSurface(status: capture.statusLine)
                }

                CursorOverlay(
                    xNorm: state.cursorFX,
                    yNorm: state.cursorFY,
                    active: state.runStore.isRunning
                )
            }
            .aspectRatio(16 / 10, contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))

            HStack {
                Text(cornerLine)
                    .font(.system(size: 10.5, design: .monospaced))
                    .foregroundStyle(Theme.textMuted)
                Spacer()
                Text(state.bridge.bridgeLine)
                    .font(.system(size: 10.5))
                    .foregroundStyle(Theme.textMuted)
            }
            .padding(.horizontal, 4)
        }
        .padding(8)
        .cardStyle(cornerRadius: 12)
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .stroke(Theme.borderOverlay, lineWidth: 1)
        )
    }

    private var cornerLine: String {
        if capture.isStreaming {
            let size = capture.frames.pixelSize
            return "\(capture.fps) fps · \(Int(size.width))×\(Int(size.height))"
        }
        return "sensor 30 Hz · tick \(state.bridge.sensorTicks)"
    }

    // MARK: - HUD

    private var hudCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            HudRow(icon: "cursorarrow", title: "Cursor", value: state.cursorPoint, accent: false)
            Divider().background(Theme.borderOverlay)
            HudRow(icon: "scope", title: "Active target", value: state.activeTarget, accent: true)
            Divider().background(Theme.borderOverlay)
            HudRow(icon: "bolt", title: "Last action", value: state.lastAction, accent: false)
        }
        .padding(12)
        .cardStyle(cornerRadius: 12)
    }
}

private struct LiveBadge: View {
    @ObservedObject var capture: CaptureService

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(dot)
                .frame(width: 7, height: 7)
            Text(label)
                .font(.system(size: 11, weight: .bold, design: .rounded))
                .foregroundStyle(Theme.textPrimary)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 3.5)
        .background(Theme.card)
        .clipShape(Capsule())
        .overlay(Capsule().stroke(Theme.borderOverlay, lineWidth: 1))
    }

    private var dot: Color {
        if capture.isStreaming { return Theme.accent }
        if !capture.hasPermission { return Theme.textSecondary }
        return Theme.textMuted
    }

    private var label: String {
        if capture.isStreaming { return "LIVE" }
        if !capture.hasPermission { return "PERMISSION" }
        return "STUB"
    }
}

private struct PermissionPrompt: View {
    @ObservedObject var capture: CaptureService

    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: "video.slash")
                .font(.system(size: 22))
                .foregroundStyle(Theme.textMuted)
            Text("Screen recording permission required")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Theme.textPrimary)
            Text("Grant permission in System Settings to enable live viewport, then restart the app.")
                .font(.system(size: 10.5))
                .foregroundStyle(Theme.textMuted)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 20)
            HStack(spacing: 8) {
                Button("Request Permission") { capture.requestPermission() }
                    .buttonStyle(.plain)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Theme.canvas)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .background(Theme.accent)
                    .clipShape(Capsule())
                    .overlay(Capsule().stroke(Theme.accent.opacity(0.4), lineWidth: 1))
                    .hoverPointer(radius: 12)
                Button("Open Settings") { capture.openScreenRecordingSettings() }
                    .buttonStyle(.plain)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Theme.textPrimary)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .background(Theme.cardElevated)
                    .clipShape(Capsule())
                    .overlay(Capsule().stroke(Theme.borderOverlay, lineWidth: 1))
                    .hoverPointer(radius: 12)
            }
        }
        .padding(12)
    }
}

private struct FallbackSurface: View {
    let status: String

    var body: some View {
        VStack(spacing: 8) {
            ProgressView()
                .controlSize(.small)
            Text(status)
                .font(.system(size: 11))
                .foregroundStyle(Theme.textMuted)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 16)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct CursorOverlay: View {
    let xNorm: Double
    let yNorm: Double
    let active: Bool

    var body: some View {
        GeometryReader { geo in
            let px = geo.size.width * CGFloat(xNorm)
            let py = geo.size.height * CGFloat(yNorm)
            ZStack {
                Circle()
                    .stroke(Theme.accent, lineWidth: 1.5)
                    .frame(width: 16, height: 16)
                Circle()
                    .fill(Theme.accent)
                    .frame(width: 5, height: 5)
            }
            .position(x: px, y: py)
            .opacity(active ? 1.0 : 0.0)
            .animation(.interactiveSpring(response: 0.15, dampingFraction: 0.8), value: px)
            .animation(.interactiveSpring(response: 0.15, dampingFraction: 0.8), value: py)
            .animation(.easeInOut(duration: 0.2), value: active)
        }
        .allowsHitTesting(false)
    }
}

private struct HudRow: View {
    let icon: String
    let title: String
    let value: String
    let accent: Bool

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: icon)
                .font(.system(size: 11))
                .foregroundStyle(accent ? Theme.accent : Theme.textMuted)
                .frame(width: 14)
            Text(title)
                .font(.system(size: 11.5))
                .foregroundStyle(Theme.textMuted)
            Spacer()
            Text(value)
                .font(.system(size: 11.5, weight: .medium, design: .monospaced))
                .foregroundStyle(accent ? Theme.accent : Theme.textPrimary)
                .lineLimit(1)
                .truncationMode(.tail)
        }
    }
}
