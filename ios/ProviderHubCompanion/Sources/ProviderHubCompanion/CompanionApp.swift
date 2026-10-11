import ProviderHubRemote
import SwiftUI

/// Thin private-network companion; execution and provider credentials stay on Mac.
@main
struct ProviderHubCompanionApp: App {
    @StateObject private var model = CompanionModel()

    var body: some Scene {
        WindowGroup {
            CompanionView(model: model)
        }
    }
}
