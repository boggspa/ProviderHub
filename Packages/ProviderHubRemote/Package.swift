// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "ProviderHubRemote",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
    ],
    products: [
        .library(name: "ProviderHubRemote", targets: ["ProviderHubRemote"]),
    ],
    targets: [
        .target(name: "ProviderHubRemote"),
        .testTarget(name: "ProviderHubRemoteTests", dependencies: ["ProviderHubRemote"]),
    ],
    swiftLanguageVersions: [.v5]
)
