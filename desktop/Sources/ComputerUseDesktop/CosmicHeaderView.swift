import AppKit
import SwiftUI

/// Title-bar toggle button as a pure SwiftUI Button.
///
/// A representable NSButton desyncs from SwiftUI's layout after an animated
/// panel toggle: its model frame stays at the pre-toggle position while the
/// icon animates away, so clicks land next to the icon ("works once, then
/// dead"). A SwiftUI Button hit-tests exactly where it draws, always.
struct NativeTitleBarButton: View {
    let iconName: String
    let isActive: Bool
    let tooltip: String
    let action: () -> Void

    @State private var isHovered = false

    var body: some View {
        Button(action: action) {
            Image(systemName: iconName)
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(
                    isActive || isHovered ? Theme.textPrimary : Theme.textSecondary
                )
                .frame(width: 28, height: 28)
                .background(
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .fill(
                            isHovered
                                ? Theme.cardElevated
                                : (isActive ? Color.white.opacity(0.08) : Color.clear)
                        )
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .stroke(
                            isHovered || isActive ? Theme.borderOverlay : Color.clear,
                            lineWidth: 1
                        )
                )
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { isHovered = $0 }
        .help(tooltip)
    }
}

/// Full-bar drag surface: clicking the empty top-bar area drags the window;
/// the toggle buttons sit on top of it and opt out via
/// `mouseDownCanMoveWindow = false`, so they receive clicks normally.
struct TitleBarDragSurface: NSViewRepresentable {
    func makeNSView(context: Context) -> DragSurfaceView {
        DragSurfaceView(frame: .zero)
    }

    func updateNSView(_ nsView: DragSurfaceView, context: Context) {}
}

final class DragSurfaceView: NSView {
    override var mouseDownCanMoveWindow: Bool { true }
}

/// Cosmic sidebar header: aligns with the window traffic-lights row.
/// Contains the traffic light clearance on the left and the sidebar toggle button on the right.
struct CosmicSidebarHeader: View {
    @ObservedObject var panelsStore: PanelsStore

    var body: some View {
        HStack(spacing: 0) {
            Spacer()
                .frame(width: Theme.trafficLightClearance)

            Spacer()

            NativeTitleBarButton(
                iconName: "sidebar.left",
                isActive: panelsStore.isSidebarVisible,
                tooltip: "Hide sidebar (Cmd+B)"
            ) {
                withAnimation(.easeInOut(duration: 0.2)) {
                    panelsStore.isSidebarVisible.toggle()
                }
            }
            .frame(width: 28, height: 28)
            .padding(.trailing, 8)
        }
        .frame(height: Theme.titleRowHeight)
        .padding(.top, Theme.titleRowTopPadding)
        .background(TitleBarDragSurface())
    }
}
