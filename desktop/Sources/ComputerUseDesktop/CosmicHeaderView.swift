import AppKit
import SwiftUI

/// Standardized native title bar icon button.
/// Subclasses NSButton with `mouseDownCanMoveWindow = false` to guarantee that clicks
/// inside the macOS top titlebar area (y < 28pt) are never captured by window-dragging.
struct NativeTitleBarButton: NSViewRepresentable {
    let iconName: String
    let isActive: Bool
    let tooltip: String
    let action: () -> Void

    func makeNSView(context: Context) -> TitleBarNSButton {
        let button = TitleBarNSButton(frame: NSRect(x: 0, y: 0, width: 28, height: 28))
        button.onClick = action
        button.toolTip = tooltip
        button.isActive = isActive
        button.setIcon(name: iconName)
        return button
    }

    func updateNSView(_ button: TitleBarNSButton, context: Context) {
        button.onClick = action
        button.toolTip = tooltip
        button.isActive = isActive
        button.setIcon(name: iconName)
    }
}

final class TitleBarNSButton: NSButton {
    var isActive: Bool = false {
        didSet { updateAppearance() }
    }
    var onClick: (() -> Void)?
    private var isHovered = false
    private var isPressed = false
    private var trackingArea: NSTrackingArea?

    override var mouseDownCanMoveWindow: Bool { false }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        setup()
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        setup()
    }

    private func setup() {
        bezelStyle = .regularSquare
        isBordered = false
        title = ""
        imagePosition = .imageOnly
        imageScaling = .scaleProportionallyDown
        wantsLayer = true
        focusRingType = .none
        layer?.cornerRadius = 6
        layer?.masksToBounds = true
        layer?.borderWidth = 1
        updateAppearance()
    }

    func setIcon(name: String) {
        if let img = NSImage(systemSymbolName: name, accessibilityDescription: nil) {
            let config = NSImage.SymbolConfiguration(pointSize: 13, weight: .medium)
            self.image = img.withSymbolConfiguration(config)
        }
    }

    func updateAppearance() {
        if isHovered || isPressed {
            layer?.backgroundColor = NSColor.white.withAlphaComponent(0.08).cgColor
            layer?.borderColor = NSColor(Theme.color97948E).withAlphaComponent(0.35).cgColor
            contentTintColor = NSColor(Theme.colorF5F4F2)
        } else {
            // Idle state: absolutely NO background color and NO border
            layer?.backgroundColor = NSColor.clear.cgColor
            layer?.borderColor = NSColor.clear.cgColor
            contentTintColor = NSColor(Theme.colorAAA8A3)
        }
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let trackingArea {
            removeTrackingArea(trackingArea)
        }
        let area = NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self,
            userInfo: nil
        )
        addTrackingArea(area)
        self.trackingArea = area
    }

    override func mouseEntered(with event: NSEvent) {
        super.mouseEntered(with: event)
        isHovered = true
        updateAppearance()
        NSCursor.pointingHand.set()
    }

    override func mouseExited(with event: NSEvent) {
        super.mouseExited(with: event)
        isHovered = false
        isPressed = false
        updateAppearance()
        NSCursor.arrow.set()
    }

    override func resetCursorRects() {
        addCursorRect(bounds, cursor: .pointingHand)
    }

    override func mouseDown(with event: NSEvent) {
        isPressed = true
        updateAppearance()
    }

    override func mouseUp(with event: NSEvent) {
        isPressed = false
        updateAppearance()
        let location = convert(event.locationInWindow, from: nil)
        if bounds.contains(location) {
            onClick?()
        }
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
