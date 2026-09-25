import AppKit
import SwiftUI

let hubVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.5.4"
let hubName = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "Provider Hub Preview"
let hubProfileID = Bundle.main.object(forInfoDictionaryKey: "BridgeProfileID") as? String ?? "14c58c94-d7e8-4a15-96b8-81668956e474"
let hubKeychainService = Bundle.main.object(forInfoDictionaryKey: "BridgeKeychainService") as? String ?? "com.mistralbridge.providerhub"
let hubDefaultPort = Bundle.main.object(forInfoDictionaryKey: "BridgeDefaultPort") as? Int ?? 11438

struct ProviderConnection: Codable, Equatable {
    var region: String
    var base_url: String
    var credential_mode: String
    var credential_revision = 0
    /// Ollama only: how long a model the hub ran stays resident after its
    /// turn. Optional so it encodes as an omitted key rather than a null:
    /// absent takes the gateway's own default, and no other provider's
    /// saved connection grows a field its daemon-less routes ignore.
    var idle_unload_seconds: Int?
    /// Claude and Codex CLI logins beyond the CLI's default one: each is a
    /// config folder (CLAUDE_CONFIG_DIR / CODEX_HOME) the CLI itself signed
    /// in to. `cli_account` names the active one; nil is the default login.
    /// Both stay omitted until used, like idle_unload_seconds.
    var cli_accounts: [CliAccount]?
    var cli_account: String?
}
struct CliAccount: Codable, Equatable, Identifiable {
    var id: String
    var label: String
    var config_dir: String
}
/// One row of the worker's `cli-accounts` answer: a sign-in probe per login.
struct CliAccountState: Identifiable {
    var id: String?
    var label: String
    var config_dir: String?
    var active: Bool
    var state: String
    var detail: String
}
struct BrandLogo: Codable, Equatable {
    var light: String
    var dark: String?
    var scale: Double?
    var leadingMarkAspectRatio: Double?
    var template: Bool?
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
    /// Credential sources this provider offers, picker order, from Python's
    /// hub_config.credential_modes. Optional so an older worker's payload
    /// still decodes; the picker falls back to its hardcoded set then.
    var credential_modes: [String]?
    var setup_url: String
    var presentation: ProviderPresentation
    enum CodingKeys: String, CodingKey {
        case id, name, regions, credential_account, credential_env, credential_modes, setup_url, presentation
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
/// Opt-in Claude Desktop third-party profile features; each maps to a profile
/// field written at launch (see bridge_core.ClaudeProfile.prepare).
struct ClaudeFeatures: Codable, Equatable {
    var dictation = false
    var builtin_browser = false
    var claude_in_chrome = false
    var scheduled_tasks = false
    var cowork_tab = false
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
    var claude_features = ClaudeFeatures()
    // Curated Claude catalogue; nil keeps the five slot mappings.
    var claude_catalogue: [ClaudeCatalogueEntry]?
    // Claude tab: teach Claude Code the catalogue ids (behavesAs rows) and
    // enable its dynamic workflows while the profile is active.
    var claude_code_settings = true
    var claude_workflows = false
    var codex_model: String?
    var codex_catalogue: [String]?
    var codex_chatgpt_account = false
    // Codex tab: launch the desktop app through the DevTools-pipe helper
    // that tints its power slider per model (see codex_accent.py).
    var codex_accent_slider = false
    // With that helper on, also hide the app's ChatGPT usage banner
    // ("You're out of Codex and Work usage") in its windows.
    var codex_hide_usage_banner = false
    // With that helper on, also keep the composer's send button usable for hub
    // routes once the ChatGPT plan's usage is exhausted (see codex_accent.py).
    var codex_unlock_composer = false
    // Leave the optional token budget on Codex's create_goal / update_goal
    // tools. Off - the default - deletes the property so a goal starts
    // unlimited on every route (see responses_tools.strip_goal_budget).
    var codex_goal_budget = false
    // Codex tab apply_patch switch plus the hand-edited per-route lists it
    // preserves across saves (see codex_catalogue.apply_patch_qualified).
    var codex_apply_patch_all = false
    var codex_apply_patch: [String]?
    var codex_apply_patch_exclude: [String]?
    // Which routes Codex offers as sub-agent model overrides, rank 1 first.
    // Codex reads one priority per catalogue row and takes the top five, so
    // this is a position in one global ordering rather than a per-model
    // allocation; a route with no rank is simply not offered.
    var codex_subagent_rank: [String: Int]?
    // The model a spawned sub-agent runs on when the parent names none.
    // Codex's own default_subagent_model is inert on 26.908, so the gateway
    // writes this into the spawn call's `model` argument instead.
    var codex_subagent_route: String?
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
    var max_input: Int?
    var max_output: Int?
    var tools: Bool?
    var vision: Bool?
    var reasoning: Bool?
    var effort_modes: [String]?
    var fast_mode: Bool?
    var speed_tier: String?
    var web_search: Bool?
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
    @Environment(\.colorScheme) private var colorScheme
    var presentation: ProviderPresentation
    var size: CGFloat = 28
    var body: some View {
        Group {
            if let logo = presentation.logo,
               let url = Bundle.main.resourceURL?.appendingPathComponent("worker").appendingPathComponent(logoFileName(logo)),
               let image = NSImage(contentsOf: url) {
                artwork(image, logo: logo).foregroundStyle(.primary).scaleEffect(logo.scale ?? 1)
            } else {
                Text(presentation.shortCode).font(.system(size: size * 0.31, weight: .bold, design: .rounded)).foregroundStyle(presentation.color)
            }
        }.frame(width: size, height: size).accessibilityLabel(presentation.displayProvider)
    }

    // Pick the artwork variant that matches the ambient colour scheme instead
    // of always taking the dark file: light uses `logo.light`, and dark keeps
    // the historical `logo.dark ?? logo.light` preference exactly. `light` is
    // non-optional in BrandLogo, so the light branch needs no dark fallback.
    private func logoFileName(_ logo: BrandLogo) -> String {
        colorScheme == .light ? logo.light : (logo.dark ?? logo.light)
    }

    @ViewBuilder
    private func artwork(_ image: NSImage, logo: BrandLogo) -> some View {
        let artwork = Image(nsImage: image).resizable()
            .renderingMode(logo.template == true ? .template : .original)
        if let markRatio = logo.leadingMarkAspectRatio {
            // Keep the source wordmark intact and show its complete leading
            // glyph, fitting wide marks such as Meta's loop inside the slot.
            let height = size / max(markRatio, 1)
            artwork.frame(width: height * image.size.width / image.size.height, height: height)
                .frame(width: height * markRatio, height: height, alignment: .leading)
                .clipped()
        } else {
            artwork.scaledToFit()
        }
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
