import AppKit
import SwiftUI

/// Latest decoded video frame, isolated in its own observable so the 60 fps
/// updates re-render only the viewfinder, never the surrounding panel.
@MainActor
final class FrameStore: ObservableObject {
    @Published var frame: CGImage?
    @Published var pixelSize = CGSize.zero

    var aspectRatio: CGFloat {
        guard pixelSize.height > 0 else { return 16.0 / 10.0 }
        return pixelSize.width / pixelSize.height
    }
}

/// Layer-backed presenter: assigns each decoded frame to the layer contents
/// without the per-frame NSImage allocation a stock image view would need.
struct StreamFrameView: NSViewRepresentable {
    @ObservedObject var store: FrameStore

    func makeNSView(context: Context) -> StreamFrameNSView {
        StreamFrameNSView()
    }

    func updateNSView(_ nsView: StreamFrameNSView, context: Context) {
        nsView.present(store.frame)
    }
}

final class StreamFrameNSView: NSView {
    private var shown: CGImage?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.contentsGravity = .resizeAspect
        layer?.backgroundColor = NSColor(red: 18.0 / 255.0, green: 17.0 / 255.0, blue: 16.0 / 255.0, alpha: 1.0).cgColor
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("StreamFrameNSView is code-only")
    }

    /// Assigns the frame unless it is the identical image object, so a
    /// redundant SwiftUI update pass never touches the layer.
    func present(_ image: CGImage?) {
        guard image !== shown else { return }
        shown = image
        layer?.contents = image
    }
}
