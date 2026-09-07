// swift-tools-version: 5.9
// computeruse macOS arayüz iskeleti — saf SwiftUI + AppKit, harici bağımlılık yok.
import PackageDescription

let package = Package(
    name: "computeruse-desktop",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(name: "ComputerUseDesktop", targets: ["ComputerUseDesktop"])
    ],
    targets: [
        .executableTarget(
            name: "ComputerUseDesktop",
            path: "Sources/ComputerUseDesktop"
        ),
        .testTarget(name: "DesktopSmokeTests", dependencies: ["ComputerUseDesktop"])
    ]
)
