import AppKit
import SwiftUI

/// Clean, deep black background for the center stage top bar when sidebar is hidden.
struct CosmicBannerBackground: View {
    var body: some View {
        LinearGradient(
            colors: [
                Theme.canvas,
                Theme.sidebar,
                Theme.canvas,
            ],
            startPoint: .top,
            endPoint: .bottom
        )
    }
}

/// Native AppKit NSButton wrapper for titlebar toggle buttons.
/// Prevents macOS hidden titlebar drag-gesture interception so clicks always register instantly.
struct NativeTitleBarButton: NSViewRepresentable {
    let iconName: String
    let isActive: Bool
    let tooltip: String
    let action: () -> Void

    func makeNSView(context: Context) -> TitleBarNSButton {
        let button = TitleBarNSButton(frame: NSRect(x: 0, y: 0, width: 28, height: 28))
        button.target = context.coordinator
        button.action = #selector(Coordinator.clicked)
        return button
    }

    func updateNSView(_ button: TitleBarNSButton, context: Context) {
        context.coordinator.action = action
        button.toolTip = tooltip
        button.isActive = isActive
        button.setIcon(name: iconName)
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(action: action)
    }

    class Coordinator: NSObject {
        var action: () -> Void
        init(action: @escaping () -> Void) { self.action = action }
        @objc func clicked() {
            action()
        }
    }
}

final class TitleBarNSButton: NSButton {
    var isActive: Bool = false {
        didSet { updateAppearance() }
    }
    private var isHovered = false
    private var trackingArea: NSTrackingArea?

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
        if isHovered {
            layer?.backgroundColor = NSColor.white.withAlphaComponent(0.08).cgColor
            layer?.borderColor = NSColor(Theme.color97948E).withAlphaComponent(0.35).cgColor
            contentTintColor = NSColor(Theme.colorF5F4F2)
        } else if isActive {
            layer?.backgroundColor = NSColor.white.withAlphaComponent(0.05).cgColor
            layer?.borderColor = NSColor(Theme.color97948E).withAlphaComponent(0.2).cgColor
            contentTintColor = NSColor(Theme.colorFFB27F)
        } else {
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
            options: [.mouseEnteredAndExited, .activeAlways],
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
        updateAppearance()
        NSCursor.arrow.set()
    }

    override func resetCursorRects() {
        addCursorRect(bounds, cursor: .pointingHand)
    }
}

/// Top strip of the sidebar. Reserves the macOS traffic-light zone on the left
/// and houses the sidebar collapse button on the right edge of the sidebar.
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
    }
}
