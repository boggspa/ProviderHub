import AppKit
import SwiftUI

/// Providers that may run on their own installed CLI's subscription login
/// instead of an API key. Mirrors `hub_config.CLI_AUTH_PROVIDERS`; the Python
/// side stays authoritative and rejects any mode it does not offer, so an entry
/// here that is not registered there simply never appears.
///
/// Local-only and opt-in per provider — there is no master flag. Choosing
/// "Installed CLI login" *is* turning the experiment on for that one provider.
fileprivate let cliAuthProviders: Set<String> = ["codex", "claude", "muse", "grok", "antigravity"]

struct ProviderPage: View {
    @ObservedObject var model: BridgeModel
    @State private var expandedBranding = false
    @State private var modelSearch = ""
    var selected: ProviderDefinition? { model.providerDefinitions.first { $0.id == model.selectedProvider } }
    var connection: ProviderConnection? { model.settings.providers[model.selectedProvider] }
    var state: ProviderState? { model.providerStates[model.selectedProvider] }
    var allModels: [ModelEntry] { model.availableModels.filter { $0.provider_id == model.selectedProvider } }
    var inventory: [ModelEntry] { allModels.filter { modelSearch.isEmpty || (($0.display_name ?? "") + " " + $0.id).localizedCaseInsensitiveContains(modelSearch) } }

    func connectionField(_ key: WritableKeyPath<ProviderConnection, String>) -> Binding<String> {
        Binding(get: { model.settings.providers[model.selectedProvider]?[keyPath: key] ?? "" }, set: { model.settings.providers[model.selectedProvider]?[keyPath: key] = $0 })
    }
    func brandingField(_ key: WritableKeyPath<BrandOverride, String?>, fallback: String) -> Binding<String> {
        Binding(get: { model.settings.branding_overrides[model.selectedProvider]?[keyPath: key] ?? fallback }, set: {
            var value = model.settings.branding_overrides[model.selectedProvider] ?? BrandOverride()
            value[keyPath: key] = $0.isEmpty ? nil : $0
            model.settings.branding_overrides[model.selectedProvider] = value
        })
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 10), count: 3), spacing: 10) {
                ForEach(model.providerDefinitions) { provider in
                    Button {
                        model.selectedProvider = provider.id
                        model.secretDraft = ""
                        modelSearch = ""
                    } label: {
                        HStack(spacing: 10) {
                            ProviderMark(presentation: provider.presentation)
                            VStack(alignment: .leading, spacing: 4) {
                                Text(provider.presentation.displayProvider).font(.system(size: 12, weight: .semibold))
                                Text(provider.id == "ollama" ? "Local daemon" : (model.providerStates[provider.id]?.credential_found == true ? "Configured" : "Set up account")).font(.system(size: 10)).foregroundStyle(.secondary)
                            }
                            Spacer(minLength: 0)
                        }.frame(maxWidth: .infinity, minHeight: 34, alignment: .leading).padding(12)
                            .background(provider.presentation.color.opacity(model.selectedProvider == provider.id ? 0.15 : 0.04), in: RoundedRectangle(cornerRadius: 10))
                            .overlay(RoundedRectangle(cornerRadius: 10).stroke(model.selectedProvider == provider.id ? provider.presentation.color.opacity(0.8) : Color.white.opacity(0.08), lineWidth: 1))
                    }.buttonStyle(.plain)
                }
            }
            if let provider = selected, let connection {
                Panel {
                    HStack(spacing: 12) {
                        ProviderMark(presentation: provider.presentation, size: 36)
                        VStack(alignment: .leading, spacing: 5) {
                            Text(provider.name).font(.system(size: 18, weight: .semibold))
                            Text(accountDescription(provider.id)).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button(provider.id == "ollama" ? "Daemon docs" : "Account setup") { if let url = URL(string: provider.setup_url) { NSWorkspace.shared.open(url) } }
                    }
                    if provider.regions.count > 1 {
                        Picker("Account region", selection: Binding(get: { connection.region }, set: { region in
                            model.settings.providers[provider.id]?.region = region
                            model.settings.providers[provider.id]?.base_url = provider.regions[region] ?? ""
                        })) {
                            ForEach(provider.regions.keys.sorted(), id: \.self) { Text(regionLabel($0)).tag($0) }
                        }
                    }
                    if provider.id == "ollama" {
                        TextField("Daemon address", text: connectionField(\.base_url)).textFieldStyle(.roundedBorder)
                        Text("Serves the models already installed in your Ollama daemon.").font(.caption).foregroundStyle(.secondary)
                        Picker("Unload a finished model after", selection: model.ollamaIdleUnload) {
                            ForEach(idleUnloadChoices, id: \.self) { Text(idleUnloadLabel($0)).tag($0) }
                        }.help("Ollama keeps a model loaded for five minutes after a turn; the hub sets this lease on the daemon after each turn it runs.")
                    } else {
                        Picker("Credential source", selection: connectionField(\.credential_mode)) {
                            // Python's hub_config.credential_modes is
                            // authoritative; the hardcoded fallback covers a
                            // worker too old to send it.
                            let modes = provider.credential_modes
                                ?? ((provider.id == "mistral" ? ["vibe"] : []) + ["keychain", "environment"]
                                    + (cliAuthProviders.contains(provider.id) ? ["cli"] : []))
                            if modes.contains("vibe") { Text("Vibe saved API key").tag("vibe") }
                            if modes.contains("keychain") { Text("macOS Keychain").tag("keychain") }
                            if modes.contains("environment") { Text("Environment").tag("environment") }
                            if modes.contains("cli") { Text("Installed CLI").tag("cli") }
                        }.pickerStyle(.segmented)
                        if connection.credential_mode == "keychain" {
                            HStack {
                                SecureField(provider.id == "muse" ? "Meta Model API key" : (provider.id == "devin" ? "Devin API key (cog_ / pat_ / apk_)" : "Provider API key"), text: $model.secretDraft).textFieldStyle(.roundedBorder)
                                Button("Save key") { Task { await model.saveKey() } }.disabled(model.busy || model.secretDraft.isEmpty)
                            }
                            Text("Stored in the app’s macOS Keychain entry — never in settings or logs.").font(.caption).foregroundStyle(.secondary)
                        } else if connection.credential_mode == "vibe" {
                            HStack {
                                Text(model.vibeAlias.isEmpty ? "Start Vibe and sign in, then reconnect." : "Vibe model: " + model.vibeAlias).font(.caption).foregroundStyle(.secondary)
                                Spacer()
                                Button("Open Vibe") { model.openVibe() }
                            }
                        } else if connection.credential_mode == "cli" {
                            Text("Local-only experiment. Runs this provider’s own installed CLI and lets it keep the subscription login it already has: the hub never reads, copies, or refreshes a credential, because these tokens rotate and a second holder revokes the first. Tools are withheld on this route, so it answers as a model and never executes against your workspace. Sign in through the CLI itself, then save.").font(.caption).foregroundStyle(.secondary)
                        } else {
                            Text("Reads " + (provider.credential_env ?? "the provider key") + " from the app’s launch environment; a Finder launch may not inherit shell variables.").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    if provider.id == "gemini" {
                        Text("Gemini replies arrive after generation finishes; the connection stays active while it waits.")
                            .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                    HStack {
                        if state?.credential_found == true { Text(state?.credential_source ?? "").font(.system(size: 11)).foregroundStyle(.secondary) }
                        Spacer()
                        Button("Reconnect") { Task { await model.reconnect() } }.disabled(model.busy)
                    }
                }.tint(provider.presentation.color)
                Panel {
                    HStack {
                        Text("Model catalogue").font(.headline)
                        Spacer()
                        if model.catalogueRefreshing { ProgressView().controlSize(.small) }
                        Text("\(allModels.count) models").font(.caption).foregroundStyle(.secondary)
                        Button("Refresh catalogue") { Task { await model.discover() } }.disabled(model.busy || model.catalogueRefreshing)
                    }
                    if !model.catalogueNotice.isEmpty {
                        Text(model.catalogueNotice).font(.caption).foregroundStyle(.secondary)
                    }
                    if let issue = model.providerRefreshIssues[provider.id] {
                        Text(issue).font(.caption).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                    }
                    if allModels.count > 8 { TextField("Find a model…", text: $modelSearch).textFieldStyle(.roundedBorder) }
                    if let summary = model.providerSummaries[provider.id] {
                        Text(sourceLabel(summary)).font(.caption).foregroundStyle(.secondary)
                        ForEach(summary.warnings ?? [], id: \.self) { warning in
                            Text(warning).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    if inventory.isEmpty {
                        Text("Models appear once the account is configured. Refresh retries discovery.").font(.caption).foregroundStyle(.secondary).lineSpacing(3)
                    } else {
                        ForEach(inventory) { entry in
                            VStack(alignment: .leading, spacing: 5) {
                                HStack {
                                    if let presentation = entry.presentation { ProviderMark(presentation: presentation, size: 19) }
                                    Text(entry.display_name ?? entry.id).font(.system(size: 12, weight: .medium))
                                    Spacer()
                                    Text(entry.context.map { "\($0.formatted()) tokens" } ?? "Provider-managed").font(.system(size: 10)).foregroundStyle(.secondary)
                                }
                                Text(capabilities(entry)).font(.system(size: 10)).foregroundStyle(.secondary)
                                if model.showRoutingIDs { Text(entry.id).font(.system(size: 9, design: .monospaced)).foregroundStyle(.tertiary).textSelection(.enabled) }
                            }
                        }
                    }
                    HStack {
                        Toggle("Show model IDs", isOn: $model.showRoutingIDs).toggleStyle(.checkbox).font(.caption)
                        Spacer()
                        Button("Choose Claude mappings →") { model.page = .models }
                    }
                }
                DisclosureGroup("Appearance", isExpanded: $expandedBranding) {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Rename providers and models, and set accent colors.").font(.caption).foregroundStyle(.secondary)
                        HStack {
                            TextField("Provider name", text: brandingField(\.displayProvider, fallback: provider.presentation.displayProvider))
                            TextField("#RRGGBB", text: brandingField(\.accent, fallback: provider.presentation.accent)).frame(width: 95)
                            TextField("Initials", text: brandingField(\.shortCode, fallback: provider.presentation.shortCode)).frame(width: 65)
                        }.textFieldStyle(.roundedBorder)
                        ForEach(inventory) { entry in
                            HStack {
                                Text(entry.display_name ?? entry.id).font(.caption).lineLimit(1).frame(width: 180, alignment: .leading)
                                TextField("Custom display name", text: Binding(get: {
                                    model.settings.branding_overrides[provider.id]?.modelLabels?[entry.model_id ?? entry.id] ?? ""
                                }, set: { value in
                                    var brand = model.settings.branding_overrides[provider.id] ?? BrandOverride()
                                    var labels = brand.modelLabels ?? [:]
                                    labels[entry.model_id ?? entry.id] = value.isEmpty ? nil : value
                                    brand.modelLabels = labels.isEmpty ? nil : labels
                                    model.settings.branding_overrides[provider.id] = brand
                                })).textFieldStyle(.roundedBorder)
                            }
                        }
                        Button("Reset this provider’s appearance") { model.settings.branding_overrides.removeValue(forKey: provider.id) }
                    }.padding(.top, 12)
                }.font(.system(size: 12))
            }
            Panel {
                HStack {
                    VStack(alignment: .leading, spacing: 6) { Text("Local gateway").font(.headline); Text(model.endpoint).font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary) }
                    Spacer()
                    Button(model.running ? "Stop gateway" : "Start gateway") { Task { await model.toggleGateway() } }.disabled(model.busy)
                }
                HStack {
                    Text("Port").font(.caption)
                    TextField(String(hubDefaultPort), value: $model.settings.port, format: .number.grouping(.never)).textFieldStyle(.roundedBorder).frame(width: 90)
                    Spacer()
                    Button("Save settings") { Task { await model.saveFromUI() } }.disabled(model.busy || !model.changed)
                }
            }
            Text("Grok Build and Muse Code subscriptions use their own agent sessions, not these model API connections.").font(.caption).foregroundStyle(.secondary).lineSpacing(3)
        }
    }
    /// Presets, plus whatever a hand-edited settings file already holds, so
    /// the picker can never silently rewrite a value it cannot display.
    /// "Keep in memory" sorts last: it is the end of the ladder, not -1.
    var idleUnloadChoices: [Int] {
        let presets = [0, 30, 60, 90, 300, 900, -1]
        guard let current = model.settings.providers["ollama"]?.idle_unload_seconds,
              !presets.contains(current) else { return presets }
        return (presets + [current]).sorted { ($0 < 0 ? Int.max : $0) < ($1 < 0 ? Int.max : $1) }
    }
    func idleUnloadLabel(_ seconds: Int) -> String {
        if seconds < 0 { return "Keep in memory" }
        if seconds == 0 { return "As soon as the turn ends" }
        if seconds < 60 { return "\(seconds) seconds" }
        if seconds % 60 != 0 { return "\(seconds) seconds" }
        let minutes = seconds / 60
        return minutes == 1 ? "1 minute" : "\(minutes) minutes"
    }
    func regionLabel(_ region: String) -> String {
        ["ams": "Amsterdam", "sgp": "Singapore", "cn": "China"][region] ?? region.capitalized
    }
    func accountDescription(_ id: String) -> String {
        switch id {
        case "mistral": return "Vibe’s configured API key or another Mistral key"
        case "kimi": return "Kimi Code subscription API key"
        case "mimo": return "MiMo Token Plan key for your account region"
        case "ollama": return "Your existing Ollama installation"
        case "deepseek": return "DeepSeek API key"
        case "cerebras": return "Cerebras API key"
        case "muse": return "Meta Model API key · API billing"
        case "grok": return "xAI API key · Pay as you go"
        case "qwen-token-plan": return "Token Plan subscription API key"
        case "openrouter": return "OpenRouter API key · Curated models"
        case "gemini": return "Google AI Studio API key"
        case "devin": return "Devin API key · Session-based agents"
        default: return "Provider model API"
        }
    }
    func sourceLabel(_ summary: ProviderSummary) -> String {
        if summary.needs_refresh == true { return "Refresh needed for this connection." }
        let source = summary.source ?? "provider metadata"
        return source.replacingOccurrences(of: "-", with: " ").replacingOccurrences(of: "_", with: " ").capitalized
    }
    func capabilities(_ model: ModelEntry) -> String {
        var values: [String] = []
        if model.tools == true { values.append("Tools") }
        if model.vision == true { values.append("Vision") }
        if model.reasoning == true { values.append("Reasoning") }
        if let modes = model.effort_modes, !modes.isEmpty { values.append("Effort: " + modes.joined(separator: ", ")) }
        if model.fast_mode == true { values.append("Fast supported") }
        if model.inference_status == "responded" { values.append("Previously responded") }
        return values.joined(separator: " · ")
    }
}
