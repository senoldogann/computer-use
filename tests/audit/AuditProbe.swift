import AppKit
import Foundation

let app: NSApplication = NSApplication.shared
let outputPath: String = CommandLine.arguments[1]
let window: NSWindow = NSWindow(
    contentRect: NSRect(x: 150, y: 180, width: 480, height: 260),
    styleMask: [.titled, .closable], backing: .buffered, defer: false
)
window.title = "Computeruse Audit — disposable probe"
let content: NSView = window.contentView!
let normal: NSTextField = NSTextField(frame: NSRect(x: 30, y: 175, width: 420, height: 30))
normal.placeholderString = "Audit normal field"
normal.setAccessibilityLabel("Audit normal field")
let secure: NSSecureTextField = NSSecureTextField(frame: NSRect(x: 30, y: 100, width: 420, height: 30))
secure.setAccessibilityLabel("Audit secure field")
secure.stringValue = "dummy-audit-value"
content.addSubview(normal)
content.addSubview(secure)
app.setActivationPolicy(.regular)
window.makeKeyAndOrderFront(nil)
app.activate(ignoringOtherApps: true)
window.makeFirstResponder(normal)
let timer: Timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { (_: Timer) in
    let values: [String: String] = [
        "normal": normal.stringValue,
        "secure_class": NSStringFromClass(type(of: secure)),
        "secure_role": secure.cell?.accessibilityRole()?.rawValue ?? "missing",
        "secure_subrole": secure.cell?.accessibilitySubrole()?.rawValue ?? "missing"
    ]
    do {
        let data: Data = try JSONSerialization.data(withJSONObject: values, options: [.sortedKeys])
        try data.write(to: URL(fileURLWithPath: outputPath), options: .atomic)
    } catch {
        fputs("audit state write failed: \(error)\n", stderr)
        app.terminate(nil)
    }
}
let exitTimer: Timer = Timer.scheduledTimer(withTimeInterval: 40, repeats: false) { (_: Timer) in
    app.terminate(nil)
}
app.run()
