import AppKit
import ObjectiveC.runtime
import SwiftUI

/// Ensures custom titlebar clicks reach SwiftUI views beneath NSTitlebarContainerView.
/// By default in macOS, NSTitlebarContainerView swallows hit-tests across the top 28-30pt
/// to handle system window dragging, which prevents clicks from reaching SwiftUI buttons.
enum TitlebarHitTestHelper {
    private static var isSwizzled = false

    static func installTitlebarHitTestPassThrough() {
        guard !isSwizzled else { return }
        guard let containerClass = NSClassFromString("NSTitlebarContainerView") else { return }

        let originalSelector = #selector(NSView.hitTest(_:))
        let swizzledSelector = #selector(NSView.cu_titlebarHitTest(_:))

        guard let originalMethod = class_getInstanceMethod(containerClass, originalSelector),
              let swizzledMethod = class_getInstanceMethod(NSView.self, swizzledSelector) else {
            return
        }

        method_exchangeImplementations(originalMethod, swizzledMethod)
        isSwizzled = true
    }
}

private extension NSView {
    @objc func cu_titlebarHitTest(_ point: NSPoint) -> NSView? {
        let hitView = self.cu_titlebarHitTest(point)
        guard let hit = hitView else { return nil }

        // Keep standard traffic lights (close, minimize, zoom) clickable
        if hit is NSButton || hit.superview is NSButton {
            return hit
        }

        let className = NSStringFromClass(type(of: hit))
        if className.contains("Widget") || className.contains("Button") {
            return hit
        }

        // Return nil on the titlebar container or background view so hit testing
        // falls through to the underlying contentView hosting SwiftUI.
        if className == "NSTitlebarContainerView" || className == "NSTitlebarView" {
            return nil
        }

        return hit
    }
}

/// Title-bar toggle button as a pure SwiftUI Button.
///
/// SwiftUI buttons hit-test exactly where they draw, avoiding NSViewRepresentable
/// frame desync issues during animated panel transitions.
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
        .pointingHand()
        .onHover { isHovered = $0 }
        .help(tooltip)
    }
}

/// Draggable surface for custom titlebar areas:
/// Clicking and dragging moves the window via `performDrag(with:)`.
/// Double-clicking toggles window zoom (standard macOS behavior).
/// Controls sitting alongside this view receive their mouse events normally.
struct TitleBarDragSurface: NSViewRepresentable {
    func makeNSView(context: Context) -> DragSurfaceView {
        let view = DragSurfaceView(frame: .zero)
        view.setContentHuggingPriority(.defaultLow, for: .horizontal)
        view.setContentHuggingPriority(.defaultLow, for: .vertical)
        return view
    }

    func updateNSView(_ nsView: DragSurfaceView, context: Context) {}
}

final class DragSurfaceView: NSView {
    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 {
            window?.zoom(nil)
        } else {
            window?.performDrag(with: event)
        }
    }
}

/// Cosmic sidebar header: aligns with the window traffic-lights row.
/// Contains the traffic light clearance on the left and the sidebar toggle button on the right.
struct CosmicSidebarHeader: View {
    @ObservedObject var panelsStore: PanelsStore

    var body: some View {
        HStack(spacing: 0) {
            Spacer()
                .frame(width: Theme.trafficLightClearance)

            TitleBarDragSurface()
                .frame(maxWidth: .infinity, maxHeight: Theme.titleRowHeight)

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
    }
}
