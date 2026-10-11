// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ProviderHubCompanion",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
    ],
    dependencies: [
        .package(path: "../../Packages/ProviderHubRemote"),
    ],
    targets: [
        .executableTarget(
            name: "ProviderHubCompanion",
            dependencies: [
                .product(name: "ProviderHubRemote", package: "ProviderHubRemote"),
            ]
        ),
    ],
    swiftLanguageModes: [.v5]
)
