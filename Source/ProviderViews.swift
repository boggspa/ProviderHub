import AppKit
import SwiftUI

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
            HStack(spacing: 8) {
                if model.catalogueRefreshing { ProgressView().controlSize(.small) }
                Text(model.catalogueNotice.isEmpty ? "Models update automatically when the app opens." : model.catalogueNotice).font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Refresh all") { model.beginCatalogueRefresh(userInitiated: true) }.disabled(model.busy || model.catalogueRefreshing)
            }
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
                        Button("Account setup") { if let url = URL(string: provider.setup_url) { NSWorkspace.shared.open(url) } }
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
                        Text("Uses the models already available in your Ollama daemon. Local and cloud-tagged models keep their actual Ollama IDs.").font(.caption).foregroundStyle(.secondary)
                    } else {
                        Picker("Credential source", selection: connectionField(\.credential_mode)) {
                            if provider.id == "mistral" { Text("Vibe saved API key").tag("vibe") }
                            Text("macOS Keychain").tag("keychain")
                            Text("Environment").tag("environment")
                        }.pickerStyle(.segmented)
                        if connection.credential_mode == "keychain" {
                            HStack {
                                SecureField(provider.id == "muse" ? "Meta Model API key" : "Provider API key", text: $model.secretDraft).textFieldStyle(.roundedBorder)
                                Button("Save key") { Task { await model.saveKey() } }.disabled(model.busy || model.secretDraft.isEmpty)
                            }
                            Text("Stored in this preview’s macOS Keychain entry. Keys stay out of settings and logs.").font(.caption).foregroundStyle(.secondary)
                        } else if connection.credential_mode == "vibe" {
                            HStack {
                                Text(model.vibeAlias.isEmpty ? "Start Vibe and sign in, then reconnect." : "Vibe model: " + model.vibeAlias).font(.caption).foregroundStyle(.secondary)
                                Spacer()
                                Button("Open Vibe") { model.openVibe() }
                            }
                        } else {
                            Text("Reads " + (provider.credential_env ?? "the provider key") + " from the app’s launch environment. Finder launches may not inherit shell variables.").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    if provider.id == "mistral" {
                        Text("Billing follows the Mistral workspace and usage policy attached to this key. The bridge does not select or verify a subscription tier.")
                            .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                    if provider.id == "muse" {
                        Text("Use a key created for the Meta Model API. Usage follows Meta’s API billing; this connection does not use your Muse Code subscription login.")
                            .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                    HStack {
                        Text(state?.credential_source ?? "Checking setup…").font(.system(size: 11)).foregroundStyle(.secondary)
                        Spacer()
                        Button("Reconnect") { Task { await model.reconnect() } }.disabled(model.busy)
                    }
                    Text(connection.base_url).font(.system(size: 10, design: .monospaced)).foregroundStyle(.tertiary).textSelection(.enabled)
                }.tint(provider.presentation.color)
                Panel {
                    HStack {
                        Text("Model catalogue").font(.headline)
                        Spacer()
                        Text("\(allModels.count) models").font(.caption).foregroundStyle(.secondary)
                        Button("Refresh catalogue") { Task { await model.discover() } }.disabled(model.busy)
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
                        Text("Models load automatically for configured accounts. Refresh to retry discovery; this fetches metadata without sending a chat request.").font(.caption).foregroundStyle(.secondary).lineSpacing(3)
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
                        Text("Personalize names and accents using TaskWraith’s presentation schema. The account and model route stay visible in technical IDs.").font(.caption).foregroundStyle(.secondary)
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
            Text("Grok Build and Muse Code subscriptions expose their own agent sessions. Their native-agent integration is documented separately; it needs a different connection from these model APIs.").font(.caption).foregroundStyle(.secondary).lineSpacing(3)
        }
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
        default: return "Provider model API"
        }
    }
    func sourceLabel(_ summary: ProviderSummary) -> String {
        if summary.needs_refresh == true { return "Refresh needed for this connection." }
        let source = summary.source ?? "provider metadata"
        return source.replacingOccurrences(of: "-", with: " ").replacingOccurrences(of: "_", with: " ").capitalized + " · discovery does not prove inference access"
    }
    func capabilities(_ model: ModelEntry) -> String {
        var values: [String] = []
        if model.tools == true { values.append("Tools") }
        if model.vision == true { values.append("Vision") }
        if model.reasoning == true { values.append("Reasoning") }
        if let modes = model.effort_modes, !modes.isEmpty { values.append("Effort: " + modes.joined(separator: ", ")) }
        if model.fast_mode == true { values.append("Fast supported") }
        if model.provider_id == "ollama", model.runtime_context == nil { values.append("Runtime context allocation unknown") }
        values.append(model.inference_status == "responded" ? "Previously responded" : "Not inference-tested")
        return values.joined(separator: " · ")
    }
}
