import AppKit
import SwiftUI

let hubVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.5.0"
let hubName = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "Provider Hub Preview"
let hubProfileID = Bundle.main.object(forInfoDictionaryKey: "BridgeProfileID") as? String ?? "14c58c94-d7e8-4a15-96b8-81668956e474"
let hubKeychainService = Bundle.main.object(forInfoDictionaryKey: "BridgeKeychainService") as? String ?? "com.mistralbridge.providerhub"
let hubDefaultPort = Bundle.main.object(forInfoDictionaryKey: "BridgeDefaultPort") as? Int ?? 11438

struct ProviderConnection: Codable, Equatable {
    var region: String
    var base_url: String
    var credential_mode: String
    var credential_revision = 0
}
struct BrandLogo: Codable, Equatable {
    var light: String
    var dark: String?
    var scale: Double?
}
struct BrandOverride: Codable, Equatable {
    var displayProvider: String?
    var hueKey: String?
    var accent: String?
    var shortCode: String?
    var modelLabels: [String: String]?
    var logo: BrandLogo?
}
struct ProviderPresentation: Decodable {
    var runtimeProvider: String
    var displayProvider: String
    var hueKey: String
    var accent: String
    var shortCode: String
    var modelLabel: String?
    var logo: BrandLogo?
    var color: Color { Color(hex: accent) }
}
struct ProviderDefinition: Decodable, Identifiable {
    var id: String
    var name: String
    var protocolName: String
    var regions: [String: String]
    var credential_account: String?
    var credential_env: String?
    var setup_url: String
    var presentation: ProviderPresentation
    enum CodingKeys: String, CodingKey {
        case id, name, regions, credential_account, credential_env, setup_url, presentation
        case protocolName = "protocol"
    }
}
struct ProviderState: Decodable {
    var credential_found: Bool
    var credential_source: String
    var credential_error: String?
}
struct ProviderSummary: Decodable {
    var needs_refresh: Bool?
    var model_count: Int?
    var source: String?
    var warnings: [String]?
    var fetched_at: String?
}
struct MappingOptions: Codable, Equatable {
    var omit_system = false
    var omit_tools = false
}
struct RouteSettings: Codable, Equatable {
    var schema_version = 3
    var port = hubDefaultPort
    var mappings = Dictionary(uniqueKeysWithValues: slots.map { ($0.id, "mistral/mistral-vibe-cli-latest") })
    var mapping_options: [String: MappingOptions] = [:]
    var providers: [String: ProviderConnection] = [:]
    var branding_overrides: [String: BrandOverride] = [:]
    var auto_stop = true
    var auto_mode = false
    var codex_model: String?
    var codex_catalogue: [String]?
}
struct ModelEntry: Decodable, Identifiable {
    var id: String
    var model_id: String?
    var provider_id: String?
    var canonical_id: String?
    var display_name: String?
    var context: Int?
    var context_options: [Int]?
    var runtime_context: Int?
    var max_output: Int?
    var tools: Bool?
    var vision: Bool?
    var reasoning: Bool?
    var effort_modes: [String]?
    var fast_mode: Bool?
    var aliases: [String]?
    var inference_status: String?
    var last_success: String?
    var discovery_source: String?
    var presentation: ProviderPresentation?
}
extension Color {
    init(hex: String) {
        let value = UInt64(hex.trimmingCharacters(in: CharacterSet(charactersIn: "#")), radix: 16) ?? 0x986781
        self.init(red: Double((value >> 16) & 255) / 255, green: Double((value >> 8) & 255) / 255, blue: Double(value & 255) / 255)
    }
}
struct ProviderMark: View {
    var presentation: ProviderPresentation
    var size: CGFloat = 28
    var body: some View {
        Group {
            if let logo = presentation.logo,
               let url = Bundle.main.resourceURL?.appendingPathComponent("worker").appendingPathComponent(logo.dark ?? logo.light),
               let image = NSImage(contentsOf: url) {
                Image(nsImage: image).resizable().scaledToFit().scaleEffect(logo.scale ?? 1)
            } else {
                Text(presentation.shortCode).font(.system(size: size * 0.31, weight: .bold, design: .rounded)).foregroundStyle(presentation.color)
            }
        }.frame(width: size, height: size).accessibilityLabel(presentation.displayProvider)
    }
}

// MARK: - Devin agent-session models

struct DevinSessionSummary: Decodable, Identifiable, Equatable {
    var id: String
    var org_id: String?
    var task: String?
    var mode: String?
    var state: String?
    var created_at: String?
    var updated_at: String?
    var completed_at: String?
    var error: String?
    var tags: [String]?
    var metadata: [String: String]?

    var displayTitle: String { task ?? id }
    var isActive: Bool { state == "pending" || state == "running" }
}

struct DevinModeOption: Decodable, Identifiable, Equatable {
    var id: String
    var display_name: String?
    var description: String?
    var capabilities: [String: Bool]?

    var title: String { display_name ?? id }
}

struct DevinOrganizationOption: Decodable, Identifiable, Equatable {
    var id: String
    var name: String?
    var slug: String?

    var title: String { name ?? slug ?? id }
}

struct DevinCatalogue: Decodable {
    var provider_id: String?
    var modes: [DevinModeOption]
    var warnings: [String]?
    var documentation: String?
}

struct DevinSessionListResponse: Decodable {
    var data: [DevinSessionSummary]
    var cursor: String?
}
