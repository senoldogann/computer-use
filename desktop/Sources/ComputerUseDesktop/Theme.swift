import AppKit
import SwiftUI

/// Shared warm obsidian & peach styling tokens for ComputerUse Desktop.
/// Palette strictly limited to the 7 defined tokens:
/// - #121110 (Deep Canvas / Background)
/// - #1A1918 (Surface / Sidebar / Base Container)
/// - #1C1B19 (Elevated Surface / Card / Input / Hover)
/// - #97948E (Borders / Dividers / Muted Text)
/// - #AAA8A3 (Secondary Text / Subheadings / Soft Highlights)
/// - #F5F4F2 (Primary Text / High-Contrast Elements)
/// - #FFB27F (Warm Peach / Radiant Accent / Active / Badges / Buttons)
enum Theme {
    static let titleZoneHeight: CGFloat = 28
    static let trafficLightCenterY: CGFloat = 14.75
    static let titleRowHeight: CGFloat = 28
    static let trafficLightClearance: CGFloat = 76
    static let titleHeaderHeight: CGFloat = 28
    static let titleRowTopPadding: CGFloat = trafficLightCenterY - titleRowHeight / 2
    static let appVersion = "0.3.9"
    static let workspaceName = "computeruse"

    // 7 Core Colors
    static let color121110 = Color(hex: 0x121110)  // Deep Canvas Base
    static let color1A1918 = Color(hex: 0x1A1918)  // Sidebar / Base Surface
    static let color1C1B19 = Color(hex: 0x1C1B19)  // Elevated Surface / Card / Input / Hover
    static let color97948E = Color(hex: 0x97948E)  // Borders / Dividers / Muted Text
    static let colorAAA8A3 = Color(hex: 0xAAA8A3)  // Secondary Text / Subtitles / Badges
    static let colorF5F4F2 = Color(hex: 0xF5F4F2)  // Primary High-Contrast Text
    static let colorFFB27F = Color(hex: 0xFFB27F)  // Radiant Peach Accent

    // Canvas & structure
    static let canvas = color121110
    static let sidebar = color1A1918
    static let card = color1C1B19
    static let cardElevated = color1C1B19

    // Borders & dividers
    static let border = color97948E.opacity(0.35)
    static let borderOverlay = color97948E.opacity(0.25)
    static let borderActive = colorFFB27F.opacity(0.80)

    // Accents & status (Strictly 7-color palette: Peach represents active, highlights & alerts)
    static let accent = colorFFB27F
    static let success = colorFFB27F
    static let danger = colorFFB27F
    static let warning = colorFFB27F

    // Typography
    static let textPrimary = colorF5F4F2
    static let textSecondary = colorAAA8A3
    static let textMuted = color97948E

    // Washes & fills
    static let sidebarWash = color1A1918.opacity(0.92)
    static let canvasWash = color121110.opacity(0.92)
    static let cardFill = color1C1B19
    static let cardFillHover = color1C1B19.opacity(0.85)
}

extension Color {
    /// Builds an opaque color from 0xRRGGBB.
    init(hex: UInt32) {
        let red = Double((hex >> 16) & 0xFF) / 255.0
        let green = Double((hex >> 8) & 0xFF) / 255.0
        let blue = Double(hex & 0xFF) / 255.0
        self.init(red: red, green: green, blue: blue)
    }
}

struct PointerOnHover: ViewModifier {
    @State private var isHovering = false
    var hoverBackground: Color? = Theme.cardElevated
    var hoverScale: CGFloat = 1.0
    var cornerRadius: CGFloat = 8

    func body(content: Content) -> some View {
        let base = content
            .background(
                Group {
                    if let hoverBackground {
                        RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                            .fill(isHovering ? hoverBackground : Color.clear)
                    }
                }
            )
            .scaleEffect(isHovering ? hoverScale : 1.0)
            .onContinuousHover { phase in
                switch phase {
                case .active:
                    NSCursor.pointingHand.set()
                case .ended:
                    NSCursor.arrow.set()
                }
            }
            .onHover { inside in
                withAnimation(.easeInOut(duration: 0.15)) {
                    isHovering = inside
                }
                if inside {
                    NSCursor.pointingHand.set()
                } else {
                    NSCursor.arrow.set()
                }
            }
            .onDisappear {
                NSCursor.arrow.set()
            }

        if #available(macOS 15.0, *) {
            base.pointerStyle(.link)
        } else {
            base
        }
    }
}

/// Vibrancy panel: content readable over dark wash.
struct GlassPanel: View {
    var material: NSVisualEffectView.Material
    var wash: Color

    var body: some View {
        VisualEffectView(material: material, blendingMode: .behindWindow)
            .overlay(wash)
    }
}

extension View {
    /// Card container using the palette elevated charcoal (#1C1B19) and muted border (#97948E).
    func cardStyle(cornerRadius: CGFloat = 14) -> some View {
        self
            .background(
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                    .fill(Theme.cardFill)
            )
            .clipShape(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                    .stroke(Theme.borderOverlay, lineWidth: 1)
            )
    }

    /// Single 1px hairline border at top or bottom edge.
    func edgeHairline(alignment: Alignment) -> some View {
        self.overlay(alignment: alignment) {
            Theme.borderOverlay.frame(height: 1)
        }
    }

    /// Pointer cursor plus hover wash using elevated surface token (#1C1B19).
    func hoverPointer(
        bg: Color? = Theme.color1C1B19,
        scale: CGFloat = 1.0,
        radius: CGFloat = 8
    ) -> some View {
        modifier(PointerOnHover(hoverBackground: bg, hoverScale: scale, cornerRadius: radius))
    }

    /// Pointer cursor with soft hover background using elevated surface token (#1C1B19).
    func pointingHand(bg: Color? = Theme.color1C1B19, radius: CGFloat = 8) -> some View {
        modifier(PointerOnHover(hoverBackground: bg, cornerRadius: radius))
    }
}
