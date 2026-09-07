import Combine
import Foundation

/// Owns the window chrome visibility state: sidebars and presented sheets
/// (AppState split, part 3/3). Sheet flags live here so `ContentView` and the
/// palette/settings entry points share one source of truth.
@MainActor
final class PanelsStore: ObservableObject {
    @Published var isSidebarVisible = true
    @Published var isRightPanelVisible = false
    @Published var showSettings = false
    @Published var showStats = false
    @Published var showModelPalette = false
    @Published var showCommandPalette = false

    func restoreDefaults() {
        isSidebarVisible = true
        isRightPanelVisible = false
    }
}