import AppKit
import SwiftUI
import Security

private let bridgeOrange = Color(red: 1.0, green: 0.43, blue: 0.18)
let slots: [(id: String, label: String)] = [
    ("claude-fable-5", "Fable 5"), ("claude-opus-5", "Opus 5"),
    ("claude-sonnet-5", "Sonnet 5"), ("claude-haiku-4-5", "Haiku 4.5"), ("claude-sonnet-4-6", "Sonnet 4.6")
]
/// Family tier behind each slot; seeds a curated catalogue from the mappings.
let slotTiers: [String: String] = ["claude-fable-5": "fable", "claude-opus-5": "opus", "claude-sonnet-5": "sonnet",
                                   "claude-haiku-4-5": "haiku", "claude-sonnet-4-6": "sonnet"]
let claudeTiers: [(id: String, label: String, model: String)] = [
    ("fable", "Fable", "claude-fable-5"), ("opus", "Opus", "claude-opus-5"),
    ("sonnet", "Sonnet", "claude-sonnet-5"), ("haiku", "Haiku", "claude-haiku-4-5")
]

struct ActivityEntry: Identifiable {
    let id = UUID()
    var time: String
    var event: String
    var model: String
    var status: Int?
}

enum Page: String, CaseIterable, Identifiable {
    case connection = "Providers", models = "Models", agents = "Agents", claude = "Claude", codex = "Codex / ChatGPT", activity = "Activity"
    var id: String { rawValue }
    var icon: String {
        switch self { case .connection: return "point.3.connected.trianglepath.dotted"; case .models: return "square.stack.3d.up"; case .agents: return "sparkles.rectangle.stack"; case .claude: return "macwindow"; case .codex: return "terminal"; case .activity: return "waveform.path" }
    }
}

struct WorkerError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

@MainActor
final class BridgeModel: ObservableObject {
    @Published var page: Page = .connection
    @Published var settings = RouteSettings()
    @Published var savedSettings = RouteSettings()
    @Published var busy = false
    @Published var gatewayState = "Stopped"
    @Published var credentialSource = "Checking provider setup…"
    @Published var credentialFound = false
    @Published var vibeAlias = ""
    @Published var vibeModel = "mistral-vibe-cli-latest"
    @Published var vibeConfigured: [String] = []
    @Published var availableModels: [ModelEntry] = []
    @Published var friendlyNames: [String: String] = [:]
    @Published var catalogueSummary = "Loading provider models…"
    @Published var catalogueRefreshing = false
    @Published var catalogueNotice = ""
    @Published var providerRefreshIssues: [String: String] = [:]
    var catalogueRefreshTask: Task<Void, Never>?
    @Published var claudeInstalled = false
    @Published var claudeRunning = false
    /// The codex-accent helper holding the DevTools pipe into a Codex session
    /// launched with the power-slider colour switch on.
    var codexAccentProcess: Process?
    @Published var profileActive = false
    @Published var recoveryNeeded = false
    @Published var codexModels: [CodexModelOption] = []
    @Published var codexAppPath: String?
    @Published var codexRunning = false
    @Published var codexProfileActive = false
    @Published var codexRecoveryNeeded = false
    var observedOwnedCodex = false
    var codexLaunchTime = Date.distantPast
    @Published var activeRequests = 0
    @Published var completed = 0
    @Published var failures = 0
    @Published var inputTokens = 0
    @Published var outputTokens = 0
    @Published var activity: [ActivityEntry] = []
    @Published var notice = ""
    @Published var noticeIsError = false
    @Published var secretDraft = ""
    @Published var selectedProvider = "mistral"
    @Published var providerDefinitions: [ProviderDefinition] = []
    @Published var providerStates: [String: ProviderState] = [:]
    @Published var providerSummaries: [String: ProviderSummary] = [:]
    @Published var showRoutingIDs = false
    @Published var runtimeFound = true
    @Published var devinSessions: [DevinSessionSummary] = []
    @Published var devinModes: [DevinModeOption] = []
    @Published var devinOrganizations: [DevinOrganizationOption] = []
    @Published var devinOrgId = ""
    @Published var devinSessionTask = ""
    @Published var devinSessionMode = "normal"
    @Published var devinLoading = false
    @Published var devinError = ""
    @Published var devinNotice = ""
    var gatewayProcess: Process?
    var parentPipe: Pipe?
    var processBuffer = Data()
    var pollTimer: Timer?
    var checkingStatus = false
    var finishingSession = false
    var hasObservedOwnedClaude = false
    var lastLaunchTime = Date.distantPast
    let root: URL
    let helper: URL
    var python: String?
    var shuttingDown = false

    init() {
        let stateName = Bundle.main.object(forInfoDictionaryKey: "BridgeStateName") as? String ?? hubName
        root = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support/" + stateName, isDirectory: true)
        helper = Bundle.main.resourceURL!.appendingPathComponent("worker/gateway.py")
        python = Self.findPython()
        runtimeFound = python != nil
        pollTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.poll() }
        }
        Task { await reconnect(recover: true) }
    }

    var running: Bool { gatewayState == "Ready" }
    var changed: Bool { settings != savedSettings }

    func omitSystem(for slot: String) -> Binding<Bool> {
        Binding(
            get: { self.settings.mapping_options[slot]?.omit_system ?? false },
            set: { self.setMappingOmit(slot, omitSystem: $0) }
        )
    }
    func omitTools(for slot: String) -> Binding<Bool> {
        Binding(
            get: { self.settings.mapping_options[slot]?.omit_tools ?? false },
            set: { self.setMappingOmit(slot, omitTools: $0) }
        )
    }
    private func setMappingOmit(_ slot: String, omitSystem: Bool? = nil, omitTools: Bool? = nil) {
        var options = settings.mapping_options[slot] ?? MappingOptions()
        if let omitSystem { options.omit_system = omitSystem }
        if let omitTools { options.omit_tools = omitTools }
        storeMappingOptions(slot, options)
    }
    // MARK: Curated Claude catalogue (replaces the slot mappings while set)

    var claudeCatalogue: [ClaudeCatalogueEntry] { settings.claude_catalogue ?? [] }

    var claudeCatalogueMode: Binding<String> {
        Binding(
            get: { self.settings.claude_catalogue == nil ? "slots" : "catalogue" },
            set: { value in
                if value == "catalogue" {
                    if self.settings.claude_catalogue == nil { self.settings.claude_catalogue = self.seededClaudeCatalogue() }
                } else {
                    self.settings.claude_catalogue = nil
                }
            }
        )
    }

    /// Each distinct mapped route keeps its slot's tier; the first Sonnet
    /// route becomes the Sonnet default once the worker normalizes the list.
    private func seededClaudeCatalogue() -> [ClaudeCatalogueEntry]? {
        var seed: [ClaudeCatalogueEntry] = []
        for slot in slots {
            guard let route = settings.mappings[slot.id], !route.isEmpty, !seed.contains(where: { $0.route == route }) else { continue }
            seed.append(ClaudeCatalogueEntry(route: route, tier: slotTiers[slot.id] ?? "sonnet", tier_default: true))
        }
        if seed.isEmpty, let first = availableModels.first(where: { $0.tools ?? true }) {
            seed = [ClaudeCatalogueEntry(route: first.id, tier: "fable", tier_default: true)]
        }
        return seed.isEmpty ? nil : seed
    }

    func isClaudeTierDefault(_ entry: ClaudeCatalogueEntry) -> Bool {
        let members = claudeCatalogue.filter { $0.tier == entry.tier }
        let chosen = members.first { $0.tier_default == true } ?? members.first
        return chosen?.route == entry.route
    }

    func makeClaudeTierDefault(_ route: String) {
        guard var list = settings.claude_catalogue, let tier = list.first(where: { $0.route == route })?.tier else { return }
        for index in list.indices where list[index].tier == tier { list[index].tier_default = list[index].route == route }
        settings.claude_catalogue = list
    }

    func claudeTier(for route: String) -> Binding<String> {
        Binding(
            get: { self.claudeCatalogue.first { $0.route == route }?.tier ?? "sonnet" },
            set: { tier in
                guard var list = self.settings.claude_catalogue, let index = list.firstIndex(where: { $0.route == route }) else { return }
                list[index].tier = tier
                list[index].tier_default = !list.contains { $0.tier == tier && $0.route != route && $0.tier_default == true }
                self.settings.claude_catalogue = list
            }
        )
    }

    func addClaudeRoute(_ route: String, tier: String = "sonnet") {
        guard var list = settings.claude_catalogue, !list.contains(where: { $0.route == route }) else { return }
        list.append(ClaudeCatalogueEntry(route: route, tier: tier, tier_default: !list.contains { $0.tier == tier && $0.tier_default == true }))
        settings.claude_catalogue = list
    }

    func addAllClaudeRoutes() {
        for entry in availableModels where entry.tools ?? true { addClaudeRoute(entry.id) }
    }

    func removeClaudeRoute(_ route: String) {
        guard var list = settings.claude_catalogue, list.count > 1 else { return }
        list.removeAll { $0.route == route }
        settings.claude_catalogue = list
    }

    func claudeCompactLimit(for route: String) -> Binding<String> {
        Binding(
            get: { self.claudeCatalogue.first { $0.route == route }?.compact_limit.map(String.init) ?? "" },
            set: { text in
                guard var list = self.settings.claude_catalogue, let index = list.firstIndex(where: { $0.route == route }) else { return }
                let trimmed = text.trimmingCharacters(in: .whitespaces)
                if trimmed.isEmpty { list[index].compact_limit = nil }
                else if let value = Int(trimmed), 1...15000000 ~= value { list[index].compact_limit = value }
                else { return }
                self.settings.claude_catalogue = list
            }
        )
    }

    /// An approximation of the id Claude sees for a row, for the technical-ID
    /// display only. It is NOT a mirror of hub_config.claude_row_id and has
    /// not been one since the worker began defusing third-party family names
    /// out of the slug: this builds the tier's model id as the prefix, where
    /// the worker builds one ladder prefix for every row, and it splits any
    /// "a/b" as provider/model where the worker only splits known providers.
    /// Routing never reads this value. Port the worker's rules here before
    /// trusting it for anything but a rough label.
    func claudeRowID(_ entry: ClaudeCatalogueEntry) -> String {
        func slug(_ text: String) -> String {
            var out = ""; var dash = false
            for character in text.lowercased() {
                if character.isASCII, character.isLetter || character.isNumber { out.append(character); dash = false }
                else if !dash { out.append("-"); dash = true }
            }
            return out.trimmingCharacters(in: CharacterSet(charactersIn: "-"))
        }
        let base = claudeTiers.first { $0.id == entry.tier }?.model ?? "claude-sonnet-5"
        let parts = entry.route.split(separator: "/", maxSplits: 1).map(String.init)
        let provider = slug(parts.count > 1 ? parts[0] : "mistral")
        let modelPart = slug(parts.count > 1 ? parts[1] : (parts.first ?? ""))
        return base + "-" + (modelPart.hasPrefix(provider + "-") ? modelPart : provider + "-" + modelPart)
    }

    func compactLimit(for slot: String) -> Binding<String> {
        Binding(
            get: {
                if let limit = self.settings.mapping_options[slot]?.compact_limit { return String(limit) }
                return ""
            },
            set: { self.setMappingCompactLimit(slot, $0) }
        )
    }
    private func setMappingCompactLimit(_ slot: String, _ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        var options = settings.mapping_options[slot] ?? MappingOptions()
        if trimmed.isEmpty {
            options.compact_limit = nil
        } else if let value = Int(trimmed), 1...15000000 ~= value {
            options.compact_limit = value
        } else {
            return
        }
        storeMappingOptions(slot, options)
    }
    private func storeMappingOptions(_ slot: String, _ options: MappingOptions) {
        if options.omit_system || options.omit_tools || options.compact_limit != nil {
            settings.mapping_options[slot] = options
        } else {
            settings.mapping_options.removeValue(forKey: slot)
        }
    }

    /// The gateway's own lease when the Ollama connection names none.
    /// Kept equal to ollama_lifecycle.DEFAULT_LEASE_SECONDS so the picker
    /// shows the seconds the worker will actually use.
    static let defaultIdleUnloadSeconds = 90

    /// Seconds an Ollama model stays resident after the turn that used it.
    /// Reading falls back to the gateway default rather than to an empty
    /// selection, because a connection without the key is not unset — it
    /// is that default.
    var ollamaIdleUnload: Binding<Int> {
        Binding(
            get: { self.settings.providers["ollama"]?.idle_unload_seconds ?? Self.defaultIdleUnloadSeconds },
            set: { self.settings.providers["ollama"]?.idle_unload_seconds = $0 }
        )
    }

    enum ChangeKind {
        /// Nothing to write.
        case unchanged
        /// Only Swift-side preferences (auto stop/mode) differ; no gateway material.
        case prefs
        /// Only the Codex catalogue or default differs.
        case codexOnly
        /// Claude-side routing (mappings, mapping_options, providers, port, branding) differs.
        case claudeRouting
        /// Both harnesses' selections or shared provider settings differ.
        case mixed
    }

    var changeKind: ChangeKind {
        if settings == savedSettings { return .unchanged }
        var mine = settings
        mine.codex_model = savedSettings.codex_model
        mine.codex_catalogue = savedSettings.codex_catalogue
        mine.codex_chatgpt_account = savedSettings.codex_chatgpt_account
        mine.codex_apply_patch_all = savedSettings.codex_apply_patch_all
        mine.codex_apply_patch = savedSettings.codex_apply_patch
        mine.codex_apply_patch_exclude = savedSettings.codex_apply_patch_exclude
        mine.codex_accent_slider = savedSettings.codex_accent_slider
        mine.codex_hide_usage_banner = savedSettings.codex_hide_usage_banner
        mine.codex_subagent_rank = savedSettings.codex_subagent_rank
        mine.codex_subagent_route = savedSettings.codex_subagent_route
        if mine == savedSettings { return .codexOnly }
        var prefsOnly = mine
        prefsOnly.auto_stop = savedSettings.auto_stop
        prefsOnly.auto_mode = savedSettings.auto_mode
        prefsOnly.claude_features = savedSettings.claude_features
        prefsOnly.claude_code_settings = savedSettings.claude_code_settings
        prefsOnly.claude_workflows = savedSettings.claude_workflows
        if prefsOnly == savedSettings { return .prefs }
        let codexChanged = settings.codex_model != savedSettings.codex_model
            || settings.codex_catalogue != savedSettings.codex_catalogue
            || settings.codex_chatgpt_account != savedSettings.codex_chatgpt_account
            || settings.codex_apply_patch_all != savedSettings.codex_apply_patch_all
            || settings.codex_apply_patch != savedSettings.codex_apply_patch
            || settings.codex_apply_patch_exclude != savedSettings.codex_apply_patch_exclude
            || settings.codex_accent_slider != savedSettings.codex_accent_slider
            || settings.codex_hide_usage_banner != savedSettings.codex_hide_usage_banner
            || settings.codex_subagent_rank != savedSettings.codex_subagent_rank
            || settings.codex_subagent_route != savedSettings.codex_subagent_route
        return codexChanged ? .mixed : .claudeRouting
    }
    var routeOptions: [String] {
        let known = Set(availableModels.flatMap { ($0.aliases ?? []) + [$0.id] })
        let extra = Set(settings.mappings.values.filter { !known.contains($0) })
        return Array(Set(availableModels.map(\.id))).sorted { modelLabel($0) < modelLabel($1) } + extra.sorted()
    }
    var endpoint: String { "http://127.0.0.1:\(savedSettings.port)" }

    func modelLabel(_ identifier: String) -> String {
        let entry = modelEntry(identifier)
        let name = friendlyNames[identifier] ?? entry?.display_name ?? identifier
        guard let provider = entry?.provider_id, let definition = providerDefinitions.first(where: { $0.id == provider }) else { return name }
        let brand = entry?.presentation?.displayProvider ?? definition.presentation.displayProvider
        let account = definition.presentation.displayProvider
        return name + " · " + (brand == account ? account : brand + " via " + account)
    }

    func modelEntry(_ identifier: String) -> ModelEntry? {
        availableModels.first { $0.id == identifier || ($0.aliases ?? []).contains(identifier) }
    }

    func modelFacts(_ identifier: String) -> String {
        guard let entry = modelEntry(identifier) else { return "Metadata missing · refresh this provider" }
        let state: String
        switch entry.inference_status {
        case "responded": state = "Previously responded"
        case "quota_limited": state = "Quota or rate limited"
        case "unavailable": state = "Rejected by provider"
        default: state = entry.discovery_source?.contains("documentation") == true ? "Documented · not tested" : "Advertised · not tested"
        }
        let context = entry.context.map { "\($0.formatted()) tokens" } ?? "Provider-managed context"
        return "\(context) · \(state)"
    }

    func readCatalogue(_ object: [String: Any], modelsKey: String) {
        readCodexState(object)
        func decode<T: Decodable>(_ type: T.Type, _ raw: Any?) -> T? {
            guard let raw, let data = try? JSONSerialization.data(withJSONObject: raw) else { return nil }
            return try? JSONDecoder().decode(type, from: data)
        }
        if let models = decode([ModelEntry].self, object[modelsKey]) { availableModels = models }
        if let definitions = decode([ProviderDefinition].self, object["provider_definitions"]) { providerDefinitions = definitions }
        if let states = decode([String: ProviderState].self, object["provider_states"]) { providerStates = states }
        friendlyNames = object["friendly_names"] as? [String: String] ?? friendlyNames
        if let summary = object["catalog_summary"] as? [String: Any] {
            providerSummaries = decode([String: ProviderSummary].self, summary["providers"]) ?? [:]
        }
        let count = Set(availableModels.compactMap(\.provider_id)).count
        catalogueSummary = "\(availableModels.count) models across \(count) providers · aliases grouped by model and context"
    }

    var workerEnvironment: [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["MISTRAL_BRIDGE_STATE_DIR"] = root.path
        env["MISTRAL_BRIDGE_PROFILE_ID"] = hubProfileID
        env["MISTRAL_BRIDGE_KEYCHAIN_SERVICE"] = hubKeychainService
        env["MISTRAL_BRIDGE_DISPLAY_NAME"] = hubName
        env["MISTRAL_BRIDGE_DEFAULT_PORT"] = String(hubDefaultPort)
        if Bundle.main.object(forInfoDictionaryKey: "BridgeSeedMistralMetadata") as? Bool == true {
            env["MISTRAL_BRIDGE_SEED_CATALOG"] = NSHomeDirectory() + "/Library/Application Support/Mistral Bridge/catalog.json"
        }
        // Bytecode caches live in the hub's state directory, never inside the
        // signed bundle: a __pycache__ under Resources breaks the app's
        // resource seal, which Gatekeeper and future updaters check.
        env["PYTHONPYCACHEPREFIX"] = root.appendingPathComponent("pycache", isDirectory: true).path
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONNOUSERSITE"] = "1"
        env.removeValue(forKey: "PYTHONHOME")
        env.removeValue(forKey: "PYTHONPATH")
        return env
    }

    static func findVibe() -> String? {
        let candidates = [NSHomeDirectory() + "/.local/bin/vibe", "/opt/homebrew/bin/vibe", "/usr/local/bin/vibe"]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    static func findPython() -> String? {
        if let bundled = Bundle.main.resourceURL?.appendingPathComponent("python/bin/python3").path,
           FileManager.default.isExecutableFile(atPath: bundled) { return bundled }
        if let vibe = findVibe(), let first = try? String(contentsOfFile: vibe, encoding: .utf8).components(separatedBy: .newlines).first,
           first.hasPrefix("#!/"), !first.contains("/usr/bin/env") {
            let candidate = String(first.dropFirst(2)).trimmingCharacters(in: .whitespaces)
            if candidate.contains("python"), FileManager.default.isExecutableFile(atPath: candidate) { return candidate }
        }
        let candidates = [NSHomeDirectory() + "/.local/share/uv/tools/mistral-vibe/bin/python3", "/opt/homebrew/bin/python3.13", "/opt/homebrew/bin/python3.12", "/opt/homebrew/bin/python3.11", "/usr/local/bin/python3.13", "/usr/local/bin/python3.12", "/usr/local/bin/python3.11"]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    /// Remove any bytecode cache that an outside Python run left inside the
    /// worker directory, so the bundle matches its signature again before the
    /// helper runs. Caches are disposable; nothing else in the bundle is touched.
    nonisolated static func scrubWorkerCaches(helper: String) {
        let worker = URL(fileURLWithPath: helper).deletingLastPathComponent()
        let manager = FileManager.default
        guard let walker = manager.enumerator(at: worker, includingPropertiesForKeys: [.isDirectoryKey]) else { return }
        var caches: [URL] = []
        for case let url as URL in walker where url.lastPathComponent == "__pycache__" {
            caches.append(url)
            walker.skipDescendants()
        }
        for url in caches { try? manager.removeItem(at: url) }
    }

    nonisolated static func execute(python: String, helper: String, environment: [String: String], command: String, provider: String?, input: Data?) async throws -> Data {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                scrubWorkerCaches(helper: helper)
                let process = Process()
                process.executableURL = URL(fileURLWithPath: python)
                process.arguments = [helper, command]
                if let provider { process.arguments! += ["--provider", provider] }
                process.environment = environment
                let output = Pipe(), errors = Pipe(), stdin = Pipe()
                process.standardOutput = output; process.standardError = errors; process.standardInput = stdin
                do {
                    try process.run()
                    if let input { try stdin.fileHandleForWriting.write(contentsOf: input) }
                    try stdin.fileHandleForWriting.close()
                    let timeout: Double = ["refresh-all", "prepare-launch", "discover", "activate", "codex-prepare", "codex-activate"].contains(command) ? 120 : 45
                    DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
                        if process.isRunning { process.terminate() }
                    }
                    let data = output.fileHandleForReading.readDataToEndOfFile()
                    _ = errors.fileHandleForReading.readDataToEndOfFile()
                    process.waitUntilExit()
                    if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any], object["ok"] as? Bool == false {
                        throw WorkerError(message: object["error"] as? String ?? "The operation failed.")
                    }
                    guard process.terminationStatus == 0 else { throw WorkerError(message: "The gateway helper could not complete this operation. Reconnect and try again.") }
                    continuation.resume(returning: data)
                } catch { continuation.resume(throwing: error) }
            }
        }
    }

    func command(_ name: String, provider: String? = nil, input: Data? = nil) async throws -> [String: Any] {
        guard let python else { throw WorkerError(message: "Provider Hub needs Python 3.11 or newer. Install a supported Python runtime, then click Reconnect.") }
        let data = try await Self.execute(python: python, helper: helper.path, environment: workerEnvironment, command: name, provider: provider, input: input)
        return (try JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    func tell(_ message: String, error: Bool = false) {
        notice = message; noticeIsError = error
    }

    func reconnect(recover: Bool = false) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        await waitForCatalogueRefresh()
        python = Self.findPython(); runtimeFound = python != nil
        do {
            let info = try await command("inspect")
            if let raw = info["settings"], let data = try? JSONSerialization.data(withJSONObject: raw), let decoded = try? JSONDecoder().decode(RouteSettings.self, from: data) {
                settings = decoded; savedSettings = decoded
            }
            credentialFound = info["credential_found"] as? Bool ?? false
            credentialSource = info["credential_source"] as? String ?? "Not connected"
            claudeInstalled = info["claude_installed"] as? Bool ?? false
            claudeRunning = info["claude_running"] as? Bool ?? false
            profileActive = info["profile_active"] as? Bool ?? false
            recoveryNeeded = info["recovery_needed"] as? Bool ?? false
            if let vibe = info["vibe"] as? [String: Any] {
                vibeAlias = vibe["active_display_name"] as? String ?? vibe["active_alias"] as? String ?? ""
                vibeModel = vibe["active_model"] as? String ?? "mistral-vibe-cli-latest"
                vibeConfigured = vibe["configured_models"] as? [String] ?? []
            }
            readCatalogue(info, modelsKey: "catalog")
            if recover && codexRecoveryNeeded && !codexRunning {
                _ = try await command("codex-restore")
                codexRecoveryNeeded = false; codexProfileActive = false
            } else if recover && codexRecoveryNeeded && codexProfileActive && codexRunning {
                try await startGateway()
                observedOwnedCodex = true
            }
            if recover && recoveryNeeded && !claudeRunning {
                _ = try await command("restore")
                recoveryNeeded = false; profileActive = false
                tell("Recovered the previous Claude configuration after an interrupted launch.")
            } else if recover && recoveryNeeded && profileActive && claudeRunning {
                try await startGateway()
                hasObservedOwnedClaude = true
                tell("Reconnected the gateway for Claude’s existing Provider Hub session.")
            } else if !recover { tell("Provider settings reloaded. Updating model catalogues…") }
            beginCatalogueRefresh()
        } catch { tell(error.localizedDescription, error: true) }
        loadActivity()
    }

    func applyCatalogueDiagnostics(_ result: [String: Any]) {
        guard let lifecycle = result["catalogue_lifecycle"] as? [String: Any],
              let providers = lifecycle["providers"] as? [String: [String: Any]] else { return }
        for (identifier, state) in providers {
            if ["refreshed", "current", "skipped_unconfigured"].contains(state["status"] as? String ?? "") { providerRefreshIssues.removeValue(forKey: identifier) }
            if let error = (state["error"] as? [String: Any])?["message"] as? String { providerRefreshIssues[identifier] = error }
            else if let warning = (state["warning"] as? [String: Any])?["message"] as? String { providerRefreshIssues[identifier] = warning }
        }
        let count = (lifecycle["refreshed_provider_ids"] as? [String])?.count ?? 0
        let failures = (lifecycle["errors"] as? [[String: Any]])?.count ?? 0
        if lifecycle["mode"] as? String == "prepare_launch", lifecycle["ready"] as? Bool == true {
            catalogueNotice = "Selected models are ready for Claude"
        } else {
            catalogueNotice = failures == 0 ? "Updated \(count) provider catalogues" : "Updated \(count) providers · \(failures) need attention"
        }
    }

    func beginCatalogueRefresh(userInitiated: Bool = false) {
        guard catalogueRefreshTask == nil, runtimeFound, !shuttingDown else { return }
        catalogueRefreshing = true
        catalogueNotice = "Updating provider models…"
        catalogueRefreshTask = Task { [weak self] in
            guard let self else { return }
            defer { self.catalogueRefreshing = false; self.catalogueRefreshTask = nil }
            do {
                let result = try await self.command("refresh-all")
                self.readCatalogue(result, modelsKey: "models")
                self.applyCatalogueDiagnostics(result)
                if let info = try? await self.command("inspect") { self.readCatalogue(info, modelsKey: "catalog") }
                if userInitiated { self.tell(self.catalogueNotice + ". Each provider card shows any refresh issue.") }
            } catch {
                self.catalogueNotice = "Could not refresh models; saved catalogues remain available."
                if userInitiated { self.tell(error.localizedDescription, error: true) }
            }
        }
    }

    func waitForCatalogueRefresh() async {
        if let task = catalogueRefreshTask { await task.value }
    }

    func discover() async {
        guard !busy else { return }
        let providerID = selectedProvider
        busy = true; defer { busy = false }
        await waitForCatalogueRefresh()
        do {
            if changed { try await save() }
            let result = try await command("discover", provider: providerID)
            readCatalogue(result, modelsKey: "models")
            providerRefreshIssues.removeValue(forKey: providerID)
            tell("Models refreshed. The latest catalogue is ready for your next desktop launch.")
        } catch {
            providerRefreshIssues[providerID] = error.localizedDescription
            tell(error.localizedDescription, error: true)
        }
    }

    func save() async throws {
        await waitForCatalogueRefresh()
        updateClaudeRunning()
        updateCodexRunning()
        let kind = changeKind
        if kind == .unchanged { return }
        let claudeLive = claudeRunning && profileActive
        let codexLive = codexRunning && codexRecoveryNeeded
        if activeRequests > 0 || claudeLive || codexLive {
            // While one desktop session is live, only changes scoped to the
            // idle harness's selection are writable; the launch flow offers a
            // gateway restart when the new selection needs a fresh snapshot.
            switch kind {
            case .prefs:
                try await writeSettings()
                return
            case .codexOnly:
                guard !codexLive else {
                    throw WorkerError(message: "Quit Codex / ChatGPT before changing its model catalogue or default.")
                }
            case .claudeRouting:
                guard !claudeLive else {
                    throw WorkerError(message: "Quit Claude before changing its provider settings.")
                }
            case .mixed:
                throw WorkerError(message: "Quit the desktop sessions using this gateway before changing provider settings.")
            case .unchanged:
                return
            }
            try await saveScoped()
            return
        }
        if running { try await checkIdleGateway() }
        if gatewayProcess?.isRunning == true { await stopGateway() }
        try await writeSettings()
    }

    func saveScoped() async throws {
        // Persist a change scoped to the idle harness without disturbing the
        // gateway owned by the live one.
        if running { try await checkIdleGateway() }
        try await writeSettings()
    }

    private func writeSettings() async throws {
        let data = try JSONEncoder().encode(settings)
        let result = try await command("save", input: data)
        readCatalogue(result, modelsKey: "models")
        if let raw = result["settings"], let bytes = try? JSONSerialization.data(withJSONObject: raw), let normalized = try? JSONDecoder().decode(RouteSettings.self, from: bytes) { settings = normalized }
        savedSettings = settings
    }

    func gatewayMatches(fingerprint: String?, digest: String?) async throws -> Bool? {
        var request = try authorizedRequest(path: "/_bridge/status")
        request.timeoutInterval = 10
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200,
              let status = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        activeRequests = status["active"] as? Int ?? activeRequests
        let fingerprintMatches = fingerprint.map { status["catalogue_fingerprint"] as? String == $0 } ?? true
        let digestMatches = digest.map { status["codex_catalogue_digest"] as? String == $0 } ?? true
        return fingerprintMatches && digestMatches
    }

    func ensureGatewaySnapshot(fingerprint: String?, digest: String?, otherHarness: String) async throws {
        guard running, fingerprint != nil || digest != nil else { return }
        if try await gatewayMatches(fingerprint: fingerprint, digest: digest) == true { return }
        if activeRequests > 0 {
            throw WorkerError(message: "Wait for the active model requests to finish, then launch again so the gateway can load your latest selection.")
        }
        let alert = NSAlert()
        alert.messageText = "Restart the gateway for this launch?"
        alert.informativeText = anyOwnedHarnessRunning
            ? "The gateway is running with an earlier model selection, and \(otherHarness) is connected to it. Restarting briefly interrupts \(otherHarness), which reconnects automatically."
            : "The gateway is running with an earlier model selection. Restarting it loads your latest selection."
        alert.addButton(withTitle: "Cancel")
        alert.addButton(withTitle: "Restart Gateway")
        guard alert.runModal() == .alertSecondButtonReturn else {
            throw WorkerError(message: "Launch cancelled. The gateway keeps its earlier selection until it is restarted.")
        }
        await stopGateway()
        try await startGateway()
        if try await gatewayMatches(fingerprint: fingerprint, digest: digest) != true {
            throw WorkerError(message: "The restarted gateway did not load the prepared selection. Reconnect and try again.")
        }
    }

    func checkIdleGateway() async throws {
        let request = try authorizedRequest(path: "/_bridge/status")
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200,
              let status = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let count = status["active"] as? Int else { throw WorkerError(message: "Could not verify that the gateway is idle.") }
        activeRequests = count
        if count > 0 { throw WorkerError(message: "Wait for active model requests to finish before changing this connection.") }
    }

    func saveFromUI() async {
        guard !busy else { return }
        busy = true; defer { busy = false }
        do { try await save(); tell("Settings saved. They will be used for your next desktop session.") }
        catch { tell(error.localizedDescription, error: true) }
    }

    func startGateway() async throws {
        if running { return }
        guard gatewayProcess == nil else { throw WorkerError(message: "The gateway is still starting or stopping.") }
        guard let python else { throw WorkerError(message: "Provider Hub needs Python 3.11 or newer. Install a supported Python runtime, then reconnect.") }
        Self.scrubWorkerCaches(helper: helper.path)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [helper.path, "serve", "--parent-pipe"]
        process.environment = workerEnvironment
        let stdout = Pipe(), stderr = Pipe(), stdin = Pipe()
        process.standardOutput = stdout; process.standardError = stderr; process.standardInput = stdin
        parentPipe = stdin; processBuffer = Data(); gatewayState = "Starting"
        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            Task { @MainActor in self?.consumeOutput(data) }
        }
        stderr.fileHandleForReading.readabilityHandler = { handle in
            // Worker errors are returned as redacted JSON on stdout.
            if handle.availableData.isEmpty { handle.readabilityHandler = nil }
        }
        process.terminationHandler = { [weak self] _ in
            Task { @MainActor in
                guard let self, self.gatewayProcess === process else { return }
                self.gatewayProcess = nil; self.parentPipe = nil
                if self.gatewayState != "Error" { self.gatewayState = "Stopped" }
            }
        }
        do { try process.run(); gatewayProcess = process }
        catch { gatewayState = "Error"; parentPipe = nil; throw error }
        for _ in 0..<150 {
            if running { return }
            if gatewayState == "Error" || !process.isRunning { break }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        if !running {
            if process.isRunning { process.terminate() }
            throw WorkerError(message: noticeIsError ? notice : "The gateway could not start. Check the runtime and gateway port.")
        }
    }

    func consumeOutput(_ data: Data) {
        processBuffer.append(data)
        while let range = processBuffer.range(of: Data([10])) {
            let line = processBuffer.subdata(in: 0..<range.lowerBound)
            processBuffer.removeSubrange(0..<range.upperBound)
            guard let json = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
            if json["ready"] as? Bool == true {
                gatewayState = "Ready"
                credentialSource = json["credential_source"] as? String ?? credentialSource
            } else if let error = json["error"] as? String {
                gatewayState = "Error"; tell(error, error: true)
            }
        }
    }

    func stopGateway() async {
        guard let process = gatewayProcess else { return }
        gatewayState = "Stopping"
        try? parentPipe?.fileHandleForWriting.close()
        parentPipe = nil
        for _ in 0..<30 {
            if !process.isRunning { break }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        if process.isRunning { process.terminate() }
        gatewayProcess = nil; gatewayState = "Stopped"; activeRequests = 0
    }

    func toggleGateway() async {
        guard !busy else { return }
        busy = true; defer { busy = false }
        do {
            if running {
                if anyOwnedHarnessRunning || activeRequests > 0 { throw WorkerError(message: "Close the desktop sessions using this gateway before stopping it.") }
                await stopGateway()
                tell("Gateway stopped. Your model mappings are saved for the next launch.")
            } else { try await save(); try await startGateway(); tell("Gateway ready. You can test a model or launch a desktop session.") }
        } catch { tell(error.localizedDescription, error: true) }
    }

    func authorizedRequest(path: String, method: String = "GET", body: [String: Any]? = nil) throws -> URLRequest {
        let token = try String(contentsOf: root.appendingPathComponent("gateway-token"), encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
        var request = URLRequest(url: URL(string: endpoint + path)!)
        request.httpMethod = method
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 90
        if let body { request.httpBody = try JSONSerialization.data(withJSONObject: body); request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        return request
    }

    func testRoute(_ slot: String = "claude-fable-5") async {
        guard !busy else { return }
        busy = true; defer { busy = false }
        do {
            await waitForCatalogueRefresh()
            if changed { try await save() }
            updateClaudeRunning()
            updateCodexRunning()
            if running && !anyOwnedHarnessRunning {
                try await checkIdleGateway()
                await stopGateway()
            }
            try await startGateway()
            var body: [String: Any] = [
                "model": slot, "max_tokens": 512,
                "messages": [["role": "user", "content": "Reply with exactly: Provider Hub is connected."]]
            ]
            let entry = modelEntry(savedSettings.mappings[slot] ?? slot)
            if entry?.reasoning == false || entry?.effort_modes?.contains("none") == true { body["thinking"] = ["type": "disabled"] }
            else if entry?.effort_modes?.contains("low") == true { body["output_config"] = ["effort": "low"] }
            let request = try authorizedRequest(path: "/v1/messages", method: "POST", body: body)
            let (data, response) = try await URLSession.shared.data(for: request)
            let object = (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
            if (response as? HTTPURLResponse)?.statusCode != 200 {
                let error = object["error"] as? [String: Any]
                throw WorkerError(message: error?["message"] as? String ?? "The model test failed.")
            }
            let content = object["content"] as? [[String: Any]] ?? []
            let reply = content.compactMap { $0["text"] as? String }.joined()
            guard !reply.isEmpty else { throw WorkerError(message: "The route responded but returned no visible text. Try a different model or reasoning level.") }
            tell("Connected · \(modelLabel(savedSettings.mappings[slot] ?? slot))\n\(reply)")
        } catch { tell(error.localizedDescription, error: true) }
        await refreshStatus()
        if let result = try? await command("inspect") { readCatalogue(result, modelsKey: "catalog") }
    }

    func launchClaude() async {
        guard !busy else { return }
        busy = true; defer { busy = false }
        updateClaudeRunning()
        if claudeRunning {
            if profileActive {
                NSWorkspace.shared.runningApplications.first { $0.bundleIdentifier == "com.anthropic.claudefordesktop" }?.activate(options: [.activateAllWindows])
            } else { tell("Claude is already open with another profile. Finish your current work and quit Claude, then launch it here.", error: true) }
            return
        }
        do {
            if recoveryNeeded { _ = try await command("restore"); recoveryNeeded = false }
            tell("Preparing the selected models for Claude…")
            updateCodexRunning()
            if changed || !anyOwnedHarnessRunning { try await save() }
            else { await waitForCatalogueRefresh() }
            let prepared = try await command("prepare-launch")
            readCatalogue(prepared, modelsKey: "models")
            applyCatalogueDiagnostics(prepared)
            // save() stops an idle gateway before preparation. The new worker
            // must snapshot the prepared catalogue, not the old startup cache.
            try await startGateway()
            try await ensureGatewaySnapshot(fingerprint: prepared["catalogue_fingerprint"] as? String, digest: nil, otherHarness: "Codex / ChatGPT")
            _ = try await command("activate")
            profileActive = true; recoveryNeeded = true
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = true
            // Profile isolation uses Claude's native 3P mode. Do not change HOME,
            // Chromium flags, the application bundle, or the user's CLI config.
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: "/Applications/Claude.app"), configuration: configuration) { _, error in
                    if let error { continuation.resume(throwing: error) }
                    else { continuation.resume() }
                }
            }
            lastLaunchTime = Date(); hasObservedOwnedClaude = false
            tell("Claude is opening with your provider profile. Its previous configuration will be restored after Claude quits.")
        } catch {
            updateClaudeRunning()
            if !claudeRunning && recoveryNeeded {
                _ = try? await command("restore")
                recoveryNeeded = false; profileActive = false
            }
            tell(error.localizedDescription, error: true)
        }
    }

    func restore() async {
        guard !busy else { return }
        busy = true; defer { busy = false }
        do {
            let result = try await command("restore")
            recoveryNeeded = false; profileActive = false; hasObservedOwnedClaude = false
            if !anyOwnedHarnessRunning { await stopGateway() }
            let kept = result["preserved_external_changes"] as? Int ?? 0
            tell(kept > 0 ? "Disconnected. Changes made by another app were preserved." : "Restored Claude’s previous configuration. Your conversations remain saved.")
        } catch { tell(error.localizedDescription, error: true) }
    }

    func updateClaudeRunning() {
        claudeRunning = NSWorkspace.shared.runningApplications.contains { $0.bundleIdentifier == "com.anthropic.claudefordesktop" && !$0.isTerminated }
    }

    func poll() async {
        guard !shuttingDown else { return }
        updateClaudeRunning()
        updateCodexRunning()
        let metaPath = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support/Claude-3p/configLibrary/_meta.json")
        if let data = try? Data(contentsOf: metaPath), let meta = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            profileActive = meta["appliedId"] as? String == hubProfileID
        }
        if profileActive && claudeRunning { hasObservedOwnedClaude = true }
        let launchFailed = profileActive && !claudeRunning && !hasObservedOwnedClaude && Date().timeIntervalSince(lastLaunchTime) > 20
        if recoveryNeeded && !claudeRunning && (hasObservedOwnedClaude || launchFailed) && !busy && !finishingSession {
            finishingSession = true
            do {
                _ = try await command("restore")
                recoveryNeeded = false; profileActive = false; hasObservedOwnedClaude = false
                if savedSettings.auto_stop && !anyOwnedHarnessRunning && activeRequests == 0 { await stopGateway() }
                tell("Claude closed. Its previous configuration has been restored.")
            } catch { tell(error.localizedDescription, error: true) }
            finishingSession = false
        }
        await pollCodexRecovery()
        if running && !checkingStatus { await refreshStatus() }
        loadActivity()
    }

    func refreshStatus() async {
        guard running, !checkingStatus else { return }
        checkingStatus = true; defer { checkingStatus = false }
        do {
            var request = try authorizedRequest(path: "/_bridge/status")
            request.timeoutInterval = 2
            let (data, _) = try await URLSession.shared.data(for: request)
            guard let status = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            activeRequests = status["active"] as? Int ?? 0; completed = status["completed"] as? Int ?? 0
            failures = status["failed"] as? Int ?? 0; inputTokens = status["input_tokens"] as? Int ?? 0
            outputTokens = status["output_tokens"] as? Int ?? 0
        } catch { }
    }

    // The 2s poll only re-reads the log when the gateway actually rewrote it,
    // and then only the tail: the activity list shows just the newest rows.
    private var activityFileStamp: (size: Int, mtime: Date)?

    func loadActivity() {
        let url = root.appendingPathComponent("activity.jsonl")
        guard let values = try? url.resourceValues(forKeys: [.fileSizeKey, .contentModificationDateKey]),
              let size = values.fileSize, let mtime = values.contentModificationDate else { return }
        if let stamp = activityFileStamp, stamp.size == size, stamp.mtime == mtime { return }
        guard let handle = try? FileHandle(forReadingFrom: url) else { return }
        defer { try? handle.close() }
        let window = 64 * 1024
        try? handle.seek(toOffset: size > window ? UInt64(size - window) : 0)
        guard let data = try? handle.readToEnd(), let text = String(data: data, encoding: .utf8) else { return }
        activityFileStamp = (size, mtime)
        let entries = text.split(separator: "\n").suffix(30).reversed().compactMap { line -> ActivityEntry? in
            guard let raw = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any] else { return nil }
            return ActivityEntry(time: raw["time"] as? String ?? "", event: raw["event"] as? String ?? "", model: raw["model"] as? String ?? "", status: raw["status"] as? Int)
        }
        // Avoid replacing row identities on every timer tick.
        if entries.count != activity.count || entries.first?.time != activity.first?.time || entries.first?.event != activity.first?.event { activity = entries }
    }

    // MARK: - Devin agent sessions

    func devinRequest(path: String, method: String = "GET", body: [String: Any]? = nil) throws -> URLRequest {
        try authorizedRequest(path: path, method: method, body: body)
    }

    func devinAgentCall<T: Decodable>(_ type: T.Type, path: String, method: String = "GET", body: [String: Any]? = nil) async throws -> T {
        let request = try devinRequest(path: path, method: method, body: body)
        let (data, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse, http.statusCode >= 300 {
            let object = (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
            let message = (object["error"] as? [String: Any])?["message"] as? String ?? "Devin request failed (HTTP \(http.statusCode))."
            throw WorkerError(message: message)
        }
        return try JSONDecoder().decode(type, from: data)
    }

    func devinFetchCatalogue() async {
        do {
            let catalogue: DevinCatalogue = try await devinAgentCall(DevinCatalogue.self, path: "/v1/agents/catalogue")
            devinModes = catalogue.modes
            devinNotice = catalogue.warnings?.first ?? ""
        } catch {
            devinError = error.localizedDescription
            // Fall back to the static five modes when the gateway is offline.
            if devinModes.isEmpty {
                devinModes = [DevinModeOption(id: "normal"), DevinModeOption(id: "fast"), DevinModeOption(id: "lite"), DevinModeOption(id: "ultra"), DevinModeOption(id: "fusion")]
            }
        }
    }

    func devinLoadSessions() async {
        devinLoading = true; defer { devinLoading = false }
        do {
            let list: DevinSessionListResponse = try await devinAgentCall(DevinSessionListResponse.self, path: "/v1/agents/sessions")
            devinSessions = list.data.sorted { ($0.created_at ?? "") > ($1.created_at ?? "") }
            devinError = ""
        } catch {
            devinError = error.localizedDescription
        }
    }

    func devinCreateSession() async {
        guard !devinSessionTask.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            devinError = "Enter a task description first."
            return
        }
        devinLoading = true; defer { devinLoading = false }
        do {
            var body: [String: Any] = ["task": devinSessionTask, "mode": devinSessionMode]
            if !devinOrgId.isEmpty { body["org_id"] = devinOrgId }
            let created: DevinSessionSummary = try await devinAgentCall(DevinSessionSummary.self, path: "/v1/agents/sessions", method: "POST", body: body)
            devinSessionTask = ""
            devinError = ""
            devinNotice = "Session \(created.id) started in \(created.mode ?? devinSessionMode) mode."
            await devinLoadSessions()
        } catch {
            devinError = error.localizedDescription
        }
    }

    func devinCancel(_ session: DevinSessionSummary) async {
        do {
            let request = try devinRequest(path: "/v1/agents/sessions/\(session.id)/cancel", method: "POST")
            _ = try await URLSession.shared.data(for: request)
            devinNotice = "Session \(session.id) cancelled."
            await devinLoadSessions()
        } catch {
            devinError = error.localizedDescription
        }
    }

    func devinArchive(_ session: DevinSessionSummary) async {
        do {
            let request = try devinRequest(path: "/v1/agents/sessions/\(session.id)/archive", method: "POST")
            _ = try await URLSession.shared.data(for: request)
            devinNotice = "Session \(session.id) archived."
            await devinLoadSessions()
        } catch {
            devinError = error.localizedDescription
        }
    }

    func saveKey() async {
        guard !busy, let provider = providerDefinitions.first(where: { $0.id == selectedProvider }), let account = provider.credential_account else { return }
        let providerID = provider.id
        let key = secretDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else { tell("Enter the provider API key first.", error: true); return }
        busy = true; defer { busy = false }
        await waitForCatalogueRefresh()
        do {
            updateClaudeRunning()
            if running { try await checkIdleGateway() }
            guard activeRequests == 0 && !anyOwnedHarnessRunning else { throw WorkerError(message: "Quit the desktop sessions using this gateway before changing its key.") }
            var proposed = settings
            proposed.providers[providerID]?.credential_mode = "keychain"
            proposed.providers[providerID]?.credential_revision += 1
            _ = try await command("validate", input: JSONEncoder().encode(proposed))
            if gatewayProcess?.isRunning == true { await stopGateway() }
            // Invalidate account-scoped metadata before replacing the credential.
            // Even a subsequent settings-write failure cannot reuse the old account's list.
            let cache = root.appendingPathComponent("catalogues/" + providerID + ".json")
            if FileManager.default.fileExists(atPath: cache.path) { try FileManager.default.removeItem(at: cache) }
            if providerID == "mistral" {
                let legacy = root.appendingPathComponent("catalog.json")
                if FileManager.default.fileExists(atPath: legacy.path) { try FileManager.default.removeItem(at: legacy) }
            }
            let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: hubKeychainService, kSecAttrAccount as String: account]
            let attributes: [String: Any] = [kSecValueData as String: Data(key.utf8)]
            var status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
            if status == errSecItemNotFound {
                var entry = query; entry.merge(attributes) { _, new in new }
                entry[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
                status = SecItemAdd(entry as CFDictionary, nil)
            }
            guard status == errSecSuccess else { throw WorkerError(message: "Keychain could not save the key (\(status)).") }
            secretDraft = ""; settings = proposed
            try await save()
            if let info = try? await command("inspect") { readCatalogue(info, modelsKey: "catalog") }
            do {
                let catalogue = try await command("discover", provider: providerID)
                readCatalogue(catalogue, modelsKey: "models")
                providerRefreshIssues.removeValue(forKey: providerID)
                let count = availableModels.filter { $0.provider_id == providerID }.count
                tell("Key saved. \(count) \(provider.name) models are ready to choose.")
            } catch {
                providerRefreshIssues[providerID] = error.localizedDescription
                tell("Key saved. Model discovery needs attention: " + error.localizedDescription, error: true)
            }
        } catch { tell(error.localizedDescription, error: true) }
    }

    func openVibe() {
        guard let executable = Self.findVibe() else { tell("Mistral Vibe was not found. Install Vibe, then reconnect.", error: true); return }
        let shell = "'" + executable.replacingOccurrences(of: "'", with: "'\\''") + "'"
        let quoted = shell.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
        let source = "tell application \"Terminal\"\nactivate\ndo script \"\(quoted)\"\nend tell"
        var error: NSDictionary?
        NSAppleScript(source: source)?.executeAndReturnError(&error)
        if error != nil { tell("Terminal could not open Vibe. Start Vibe normally, then reconnect.", error: true) }
    }
}

struct Panel<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 18) { content }
            .padding(22).frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.white.opacity(0.07), lineWidth: 1))
    }
}

struct StatusPill: View {
    let text: String
    var good: Bool
    var body: some View {
        HStack(spacing: 6) { Circle().fill(good ? Color.green : Color.secondary).frame(width: 6, height: 6); Text(text).font(.system(size: 11, weight: .medium)) }
            .padding(.horizontal, 10).padding(.vertical, 6).background(Color.white.opacity(0.055), in: Capsule())
    }
}

struct BridgeWindow: View {
    @ObservedObject var model: BridgeModel
    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 28) {
                HStack(spacing: 10) {
                    Image(systemName: "point.3.connected.trianglepath.dotted").font(.system(size: 27, weight: .semibold)).foregroundStyle(bridgeOrange)
                    VStack(alignment: .leading, spacing: 2) { Text("Provider").font(.system(size: 18, weight: .semibold)); Text("HUB PREVIEW").font(.system(size: 10, weight: .semibold)).tracking(2).foregroundStyle(.secondary) }
                }.padding(.top, 12)
                VStack(spacing: 5) {
                    ForEach(Page.allCases) { page in
                        Button { model.page = page } label: {
                            HStack(spacing: 11) { Image(systemName: page.icon).frame(width: 18); Text(page == .codex ? "Codex" : page.rawValue).lineLimit(1); Spacer() }
                                .font(.system(size: 13, weight: model.page == page ? .semibold : .regular))
                                .padding(.horizontal, 12).padding(.vertical, 10)
                                .background(model.page == page ? Color.white.opacity(0.09) : .clear, in: RoundedRectangle(cornerRadius: 8))
                        }.buttonStyle(.plain).foregroundStyle(model.page == page ? .primary : .secondary)
                    }
                }
                Spacer()
                VStack(alignment: .leading, spacing: 9) {
                    StatusPill(text: model.running ? "Gateway ready" : model.gatewayState, good: model.running)
                    Text("\(hubVersion) · PROVIDER HUB").font(.system(size: 9, weight: .medium)).tracking(1).foregroundStyle(.tertiary)
                }
            }.padding(20).frame(width: 200).background(Color.black.opacity(0.15))
            Rectangle().fill(Color.white.opacity(0.07)).frame(width: 1)
            VStack(spacing: 0) {
                ScrollView {
                    VStack(alignment: .leading, spacing: 22) {
                        header
                        switch model.page {
                        case .connection: ProviderPage(model: model)
                        case .models: modelsPage
                        case .agents: DevinAgentsPage(model: model)
                        case .claude: claudePage
                        case .codex: CodexPage(model: model)
                        case .activity: activityPage
                        }
                    }.padding(30).disabled(model.busy)
                }
                if !model.notice.isEmpty {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: model.noticeIsError ? "exclamationmark.circle" : "checkmark.circle").foregroundStyle(model.noticeIsError ? Color.orange : .green)
                        Text(model.notice).font(.system(size: 12)).lineSpacing(3).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                        Button { model.notice = "" } label: { Image(systemName: "xmark").font(.system(size: 10)) }.buttonStyle(.plain).foregroundStyle(.secondary)
                    }.padding(16).background(Color.white.opacity(0.045)).overlay(alignment: .top) { Divider() }
                }
            }.frame(maxWidth: .infinity)
        }.background(Color(red: 0.095, green: 0.098, blue: 0.102)).accentColor(bridgeOrange)
            .frame(minWidth: 870, idealWidth: 940, minHeight: 680, idealHeight: 740)
    }

    var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 7) {
                Text(model.page.rawValue).font(.system(size: 27, weight: .semibold))
                Text(subtitle).font(.system(size: 13)).foregroundStyle(.secondary)
            }
            Spacer()
            if model.busy { ProgressView().controlSize(.small).padding(.top, 8) }
        }.padding(.bottom, 2)
    }

    var subtitle: String {
        switch model.page {
        case .connection: return "Connect model providers for your desktop apps."
        case .models: return "Choose the provider and model behind each Claude option."
        case .agents: return "Create and monitor Devin agent sessions."
        case .claude: return "Launch Claude with your provider configuration."
        case .codex: return "Choose a provider model for Codex / ChatGPT Desktop."
        case .activity: return "Requests through your local gateway."
        }
    }

    var modelsPage: some View {
        VStack(spacing: 18) {
            Panel {
                HStack {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(model.settings.claude_catalogue == nil ? "Model mappings" : "Curated catalogue").font(.headline)
                        Text(model.settings.claude_catalogue == nil
                             ? "Choose a model by name. Show technical IDs to enter a custom route."
                             : "Pick the models Claude’s picker lists and the Claude family tier each one plays.").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Provider catalogues") { model.page = .connection }.disabled(model.busy)
                }
                HStack {
                    Text(model.catalogueSummary).font(.system(size: 11)).foregroundStyle(.secondary)
                    Spacer()
                    if model.catalogueRefreshing { ProgressView().controlSize(.small) }
                    Button("Refresh all") { model.beginCatalogueRefresh(userInitiated: true) }.disabled(model.busy || model.catalogueRefreshing)
                }
                HStack {
                    Text("Claude’s picker").font(.system(size: 13, weight: .medium))
                    Spacer()
                    Picker("Claude’s picker", selection: model.claudeCatalogueMode) {
                        Text("Five Claude slots").tag("slots")
                        Text("Curated catalogue").tag("catalogue")
                    }.pickerStyle(.segmented).fixedSize().disabled(model.busy)
                }
                if model.settings.claude_catalogue != nil {
                    claudeCatalogueEditor
                } else {
                ForEach(slots, id: \.id) { slot in
                    HStack(spacing: 10) {
                        VStack(alignment: .leading, spacing: 4) { Text(slot.label).font(.system(size: 12, weight: .medium)); if model.showRoutingIDs { Text(slot.id).font(.system(size: 8, design: .monospaced)).foregroundStyle(.tertiary) } }.frame(width: 100, alignment: .leading)
                        Image(systemName: "arrow.right").foregroundStyle(.tertiary)
                        VStack(alignment: .leading, spacing: 6) {
                            Menu {
                                ForEach(model.providerDefinitions) { provider in
                                    Menu(provider.presentation.displayProvider) {
                                        ForEach(model.routeOptions.filter { $0.hasPrefix(provider.id + "/") }, id: \.self) { identifier in
                                            Button("\(model.modelLabel(identifier)) — \(model.modelFacts(identifier))") { model.settings.mappings[slot.id] = identifier }.help(identifier)
                                        }
                                    }
                                }
                                Divider()
                                Button("Enter a custom model ID…") { model.showRoutingIDs = true }
                            } label: {
                                Text(model.modelLabel(model.settings.mappings[slot.id] ?? "")).font(.system(size: 12, weight: .medium)).lineLimit(1)
                            }.menuStyle(.borderlessButton).menuIndicator(.hidden)
                                .frame(maxWidth: .infinity, alignment: .leading).padding(10)
                                .background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 7))
                                .help("API ID: \(model.settings.mappings[slot.id] ?? "")")
                            Text(model.modelFacts(model.settings.mappings[slot.id] ?? "")).font(.system(size: 9)).foregroundStyle(.secondary)
                            if model.showRoutingIDs {
                                TextField("provider/exact-model-id", text: Binding(get: { model.settings.mappings[slot.id] ?? "" }, set: { model.settings.mappings[slot.id] = $0 }))
                                    .textFieldStyle(.roundedBorder).font(.system(size: 10, design: .monospaced))
                            }
                            HStack(spacing: 14) {
                                Toggle("Omit system", isOn: model.omitSystem(for: slot.id)).toggleStyle(.checkbox)
                                Toggle("Omit tools", isOn: model.omitTools(for: slot.id)).toggleStyle(.checkbox)
                            }.font(.caption).foregroundStyle(.secondary)
                            HStack(spacing: 6) {
                                Text("Compact at").font(.caption).foregroundStyle(.secondary)
                                TextField("auto", text: model.compactLimit(for: slot.id))
                                    .textFieldStyle(.roundedBorder).font(.system(size: 10, design: .monospaced)).frame(width: 90)
                                    .help("Per-model auto-compact threshold in tokens. Blank follows the catalogue window at 85%.")
                                Text("tokens · blank = auto 85%").font(.caption).foregroundStyle(.secondary)
                            }
                        }.frame(maxWidth: .infinity)
                        Button("Test") { Task { await model.testRoute(slot.id) } }.disabled(model.busy)
                    }
                }
                Text("At your own risk. Claude still sends system and tools; checked boxes drop those fields here before the model. This is a first-turn smoke test for small contexts, not a coding-agent mode — tool loops and Claude Code will break.")
                    .font(.caption).foregroundStyle(.secondary).lineSpacing(3)
                Divider()
                HStack {
                    Toggle("Show technical IDs", isOn: $model.showRoutingIDs).toggleStyle(.checkbox).font(.caption)
                    Spacer()
                    Button("Use Vibe for all") { for slot in slots { model.settings.mappings[slot.id] = "mistral/" + model.vibeModel } }
                }
                }
            }
            Panel {
                Text("Model controls").font(.headline)
                HStack { Text("Context").font(.system(size: 13, weight: .medium)); Spacer(); Text("Reported per model; never an editable guess").font(.caption).foregroundStyle(.secondary) }
                Divider()
                HStack { Text("Effort").font(.system(size: 13, weight: .medium)); Spacer(); Text("Use Claude’s Effort control").font(.caption).foregroundStyle(.secondary) }
                Text("Effort follows the selected provider’s supported controls. Mistral uses standard mode at Low and reasoning at Medium or above; native Messages providers receive their supported reasoning settings.").font(.caption).foregroundStyle(.secondary).lineSpacing(3)
                Divider()
                HStack { Text("Fast").font(.system(size: 13, weight: .medium)); Spacer(); Text("Only when supported by the provider").font(.caption).foregroundStyle(.secondary) }
                Text("A speed toggle never silently swaps models. Distinct models or context variants keep separate catalogue entries. Unsupported Fast requests return a clear compatibility message.").font(.caption).foregroundStyle(.secondary)
            }
            HStack { Spacer(); Button(model.settings.claude_catalogue == nil ? "Save mappings" : "Save catalogue") { Task { await model.saveFromUI() } }.disabled(model.busy || !model.changed).controlSize(.large); Button("Launch Claude") { Task { await model.launchClaude() } }.buttonStyle(.borderedProminent).controlSize(.large).disabled(model.busy || !model.claudeInstalled) }
        }
    }

    var claudeCatalogueEditor: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("\(model.claudeCatalogue.count) model\(model.claudeCatalogue.count == 1 ? "" : "s") will appear in Claude’s picker, each under the family tier you assign. Claude starts on the Fable tier’s default (Opus if there is no Fable row); each tier’s default also answers Claude Code’s own fable, opus, sonnet and haiku requests, with the nearest tier standing in for a missing one.")
                .font(.caption).foregroundStyle(.secondary).lineSpacing(3)
            ForEach(model.claudeCatalogue) { entry in
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 6) {
                            Text(model.modelLabel(entry.route)).font(.system(size: 12, weight: .medium)).lineLimit(1)
                            if model.isClaudeTierDefault(entry) {
                                Text("tier default").font(.system(size: 9, weight: .semibold)).padding(.horizontal, 5).padding(.vertical, 1).background(bridgeOrange.opacity(0.2), in: Capsule())
                            }
                        }
                        Text(model.modelFacts(entry.route)).font(.system(size: 9)).foregroundStyle(.secondary)
                        if model.showRoutingIDs { Text(model.claudeRowID(entry)).font(.system(size: 8, design: .monospaced)).foregroundStyle(.tertiary) }
                    }
                    Spacer()
                    Picker("Tier", selection: model.claudeTier(for: entry.route)) {
                        ForEach(claudeTiers, id: \.id) { tier in Text(tier.label).tag(tier.id) }
                    }.labelsHidden().frame(width: 96).help("Claude family tier this model plays")
                    Button { model.makeClaudeTierDefault(entry.route) } label: {
                        Image(systemName: model.isClaudeTierDefault(entry) ? "star.fill" : "star").foregroundStyle(model.isClaudeTierDefault(entry) ? bridgeOrange : .secondary)
                    }.buttonStyle(.plain).help("Make this the tier’s default").disabled(model.busy)
                    TextField("auto", text: model.claudeCompactLimit(for: entry.route))
                        .textFieldStyle(.roundedBorder).font(.system(size: 10, design: .monospaced)).frame(width: 78)
                        .help("Auto-compact threshold in tokens. Blank follows the catalogue window at 85%.")
                    Button("Test") { Task { await model.testRoute(entry.route) } }.disabled(model.busy)
                    Button { model.removeClaudeRoute(entry.route) } label: { Image(systemName: "minus.circle").foregroundStyle(.secondary) }
                        .buttonStyle(.plain)
                        .disabled(model.claudeCatalogue.count <= 1 || model.busy)
                        .help(model.claudeCatalogue.count <= 1 ? "Keep at least one model in the catalogue" : "Remove from the Claude catalogue")
                }
                .padding(.horizontal, 10).padding(.vertical, 7)
                .background(Color.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 7))
            }
            HStack(spacing: 12) {
                claudeAddMenu
                Button("Add all") { model.addAllClaudeRoutes() }.disabled(model.busy || model.availableModels.isEmpty)
                Spacer()
                Toggle("Show technical IDs", isOn: $model.showRoutingIDs).toggleStyle(.checkbox).font(.caption)
            }
            Text("The tier sets what Claude expects of a model. Fable, Opus and Sonnet rows carry Claude’s full effort ladder, Ultracode included; Haiku rows have no effort control. Claude Code borrows each tier’s Claude model for capabilities while “Teach Claude Code the catalogue ids” is on (Claude tab). The five slot mappings stay saved for switching back.")
                .font(.caption).foregroundStyle(.secondary).lineSpacing(3)
        }
    }

    var claudeAddMenu: some View {
        Menu {
            ForEach(model.providerDefinitions) { provider in
                let options = model.availableModels.filter { candidate in
                    candidate.provider_id == provider.id && (candidate.tools ?? true) && !model.claudeCatalogue.contains { $0.route == candidate.id }
                }
                if !options.isEmpty {
                    Menu(provider.presentation.displayProvider) {
                        ForEach(options) { entry in
                            Button("\(model.modelLabel(entry.id)) — \(model.modelFacts(entry.id))") { model.addClaudeRoute(entry.id) }.help(entry.id)
                        }
                    }
                }
            }
        } label: { Label("Add model…", systemImage: "plus") }
            .disabled(model.busy)
    }

    var claudePage: some View {
        VStack(spacing: 18) {
            Panel {
                HStack(spacing: 18) {
                    Image(systemName: "macwindow").font(.system(size: 38, weight: .light)).foregroundStyle(bridgeOrange).frame(width: 60, height: 60)
                    VStack(alignment: .leading, spacing: 6) { Text("Claude, with your models").font(.system(size: 19, weight: .semibold)); Text("Your usual Claude interface with the models you choose.").font(.system(size: 12)).foregroundStyle(.secondary) }
                }
                Divider()
                infoRow("Provider profile", model.profileActive ? "Active" : "Ready to launch", "person.crop.rectangle")
                infoRow("Conversations", "Saved by Claude’s third-party mode", "bubble.left.and.bubble.right")
                infoRow("Previous setup", "Restored after this Claude session closes", "arrow.uturn.backward")
                if model.claudeRunning && !model.profileActive {
                    Text("Claude is already open with another profile. Finish your current work and quit Claude before switching.").font(.system(size: 12)).foregroundStyle(.orange).padding(12).frame(maxWidth: .infinity, alignment: .leading).background(Color.orange.opacity(0.07), in: RoundedRectangle(cornerRadius: 8))
                }
                if model.codexRunning && model.codexRecoveryNeeded {
                    Text("Codex / ChatGPT is live on this gateway. Launching Claude shares it; changing Claude’s mappings first briefly restarts the gateway, and Codex reconnects automatically.")
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                }
                HStack {
                    Button(model.profileActive && model.claudeRunning ? "Show Claude" : "Launch Claude") { Task { await model.launchClaude() } }.buttonStyle(.borderedProminent).controlSize(.large).disabled(model.busy || !model.claudeInstalled)
                    if model.recoveryNeeded { Button("Restore previous setup") { Task { await model.restore() } }.disabled(model.busy || model.claudeRunning) }
                    Spacer()
                }
            }
            Panel {
                Toggle(isOn: $model.settings.auto_stop) { VStack(alignment: .leading, spacing: 5) { Text("Stop gateway after the desktop sessions close").font(.system(size: 13, weight: .medium)); Text("The menu bar app stays available for your next session.").font(.caption).foregroundStyle(.secondary) } }.toggleStyle(.switch)
                Divider()
                Toggle(isOn: $model.settings.auto_mode) { VStack(alignment: .leading, spacing: 5) { Text("Enable Claude Auto mode").font(.system(size: 13, weight: .medium)); Text("Use Claude’s approval classifier through the configured gateway. Claude chooses its reviewer model internally.").font(.caption).foregroundStyle(.secondary) } }.toggleStyle(.switch)
                Divider()
                Text("Claude features in this profile").font(.system(size: 13, weight: .medium))
                Text("Claude keeps these off in a third-party profile unless the profile asks for them. Each runs locally in Claude; its model calls still go through this gateway. They apply at the next launch.").font(.caption).foregroundStyle(.secondary).lineSpacing(3)
                Toggle(isOn: $model.settings.claude_features.dictation) { Text("Dictation").font(.system(size: 12)) }.toggleStyle(.switch).controlSize(.small)
                Toggle(isOn: $model.settings.claude_features.builtin_browser) { Text("Built-in browser").font(.system(size: 12)) }.toggleStyle(.switch).controlSize(.small)
                Toggle(isOn: $model.settings.claude_features.claude_in_chrome) { Text("Claude in Chrome").font(.system(size: 12)) }.toggleStyle(.switch).controlSize(.small)
                Toggle(isOn: $model.settings.claude_features.scheduled_tasks) { Text("Scheduled tasks").font(.system(size: 12)) }.toggleStyle(.switch).controlSize(.small)
                Toggle(isOn: $model.settings.claude_features.cowork_tab) { Text("Cowork tab").font(.system(size: 12)) }.toggleStyle(.switch).controlSize(.small)
                Text("Dictation and scheduled tasks depend on the installed Claude build honouring the profile field; if one stays hidden after relaunch, that build does not offer it in third-party mode yet.").font(.caption).foregroundStyle(.secondary).lineSpacing(3)
                Divider()
                Text("Claude Code and catalogue models").font(.system(size: 13, weight: .medium))
                Toggle(isOn: $model.settings.claude_code_settings) { Text("Teach Claude Code the catalogue ids").font(.system(size: 12)) }.toggleStyle(.switch).controlSize(.small)
                Text("In catalogue mode, Provider Hub adds a modelPicker row with behavesAs to ~/.claude/settings.json for each catalogue model while the profile is active, so the Claude Code inside Claude gives it the effort ladder, capabilities and context handling of its tier’s Claude model instead of treating it as an unknown model. The rows are removed when the previous setup is restored; a terminal Claude Code sees them meanwhile.").font(.caption).foregroundStyle(.secondary).lineSpacing(3)
                Toggle(isOn: $model.settings.claude_workflows) { Text("Enable dynamic workflows for Ultracode").font(.system(size: 12)) }.toggleStyle(.switch).controlSize(.small)
                Text("Adds enableWorkflows to the same file while the profile is active. Claude’s Ultracode effort level needs dynamic workflows and a model on the Fable, Opus or Sonnet tier. With Ultracode on, the gateway sends the provider its highest reasoning setting and adds an orchestration note so the external model reaches for the Workflow tool.").font(.caption).foregroundStyle(.secondary).lineSpacing(3)
                Divider()
                Text("Claude adds a standard and a 1M choice for models that support long context. New selections prefer 1M; existing session choices are preserved.").font(.caption).foregroundStyle(.secondary).lineSpacing(3)
                Text("This uses Claude’s native third-party profile system, like Ollama. It switches the installed app’s profile; it does not create a simultaneous second Claude app. Existing Ollama and Claude conversations are retained.").font(.caption).foregroundStyle(.secondary).lineSpacing(3)
            }
            HStack { Spacer(); Button("Save preferences") { Task { await model.saveFromUI() } }.disabled(model.busy || !model.changed) }
        }
    }

    func infoRow(_ label: String, _ value: String, _ icon: String) -> some View {
        HStack { Image(systemName: icon).foregroundStyle(.secondary).frame(width: 20); Text(label).font(.system(size: 12, weight: .medium)); Spacer(); Text(value).font(.system(size: 12)).foregroundStyle(.secondary) }
    }

    var activityPage: some View {
        VStack(spacing: 18) {
            HStack(spacing: 12) { metric("Completed", model.completed); metric("In progress", model.activeRequests); metric("Tokens used", model.inputTokens + model.outputTokens) }
            Panel {
                HStack { Text("Recent requests").font(.headline); Spacer(); Text("Content stays out of logs").font(.caption).foregroundStyle(.secondary) }
                if model.activity.isEmpty {
                    VStack(spacing: 12) { Image(systemName: "waveform.path").font(.system(size: 35, weight: .light)).foregroundStyle(.tertiary); Text("Ready for your first request").font(.system(size: 14, weight: .medium)); Text("Test a model or launch a desktop session to see activity here.").font(.caption).foregroundStyle(.secondary) }.frame(maxWidth: .infinity).padding(.vertical, 44)
                } else {
                    ForEach(model.activity) { entry in
                        HStack(spacing: 10) {
                            Image(systemName: entry.event == "completed" ? "checkmark.circle.fill" : entry.event == "cancelled" ? "stop.circle" : entry.event == "compacted" ? "arrow.down.circle" : "exclamationmark.circle").foregroundStyle(entry.event == "completed" ? Color.green : entry.event == "compacted" ? Color.blue : .orange)
                            VStack(alignment: .leading, spacing: 4) { Text(model.modelLabel(entry.model)).font(.system(size: 12, weight: .medium)).help(entry.model); Text(entry.event.capitalized + (entry.status.map { " · HTTP \($0)" } ?? "")).font(.caption).foregroundStyle(.secondary) }
                            Spacer(); Text(String(entry.time.dropFirst(11).prefix(8))).font(.system(size: 10, design: .monospaced)).foregroundStyle(.tertiary)
                        }
                    }
                }
            }
            HStack { Text("Counters cover this gateway run. Usage is reported by each provider.").font(.caption).foregroundStyle(.secondary); Spacer(); Button("Open data folder") { NSWorkspace.shared.open(model.root) } }
        }
    }

    func metric(_ label: String, _ number: Int) -> some View {
        VStack(alignment: .leading, spacing: 9) { Text(label).font(.caption).foregroundStyle(.secondary); Text(number.formatted()).font(.system(size: 25, weight: .medium, design: .rounded)) }
            .padding(18).frame(maxWidth: .infinity, alignment: .leading).background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 12))
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate, NSWindowDelegate {
    var model: BridgeModel!
    var statusItem: NSStatusItem!
    var window: NSWindow!
    var readyToQuit = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        NSApp.appearance = NSAppearance(named: .darkAqua)
        let mainMenu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu(title: hubName)
        let quitItem = NSMenuItem(title: "Quit " + hubName, action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self
        appMenu.addItem(quitItem); appItem.submenu = appMenu; mainMenu.addItem(appItem)
        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        for (title, action, key) in [("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
            editMenu.addItem(NSMenuItem(title: title, action: Selector(action), keyEquivalent: key))
        }
        editItem.submenu = editMenu; mainMenu.addItem(editItem); NSApp.mainMenu = mainMenu
        model = BridgeModel()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "point.3.connected.trianglepath.dotted", accessibilityDescription: hubName)
        statusItem.button?.image?.isTemplate = true
        statusItem.button?.toolTip = hubName
        let menu = NSMenu(); menu.delegate = self; statusItem.menu = menu
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 940, height: 740), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = hubName
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.contentView = NSHostingView(rootView: BridgeWindow(model: model))
        window.center()
        showWindow()
    }

    @objc func showWindow() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc func launchClaude() { Task { await model.launchClaude(); if model.noticeIsError { showWindow() } } }
    @objc func launchCodex() { Task { await model.launchCodex(); if model.noticeIsError { showWindow() } } }
    @objc func toggleGateway() { Task { await model.toggleGateway() } }
    @objc func openVibe() { model.openVibe() }
    @objc func quit() { NSApp.terminate(nil) }
    @objc func restore() { Task { await model.restore() } }

    func menuWillOpen(_ menu: NSMenu) {
        menu.removeAllItems()
        let title = NSMenuItem(title: "\(hubName) · \(model.gatewayState)", action: nil, keyEquivalent: ""); title.isEnabled = false; menu.addItem(title)
        menu.addItem(.separator())
        add(menu, "Launch Claude…", #selector(launchClaude), "l")
        add(menu, "Launch Codex / ChatGPT…", #selector(launchCodex), "")
        add(menu, "Models & Settings…", #selector(showWindow), ",")
        add(menu, "Open Mistral Vibe", #selector(openVibe), "")
        menu.addItem(.separator())
        add(menu, model.running ? "Stop Gateway" : "Start Gateway", #selector(toggleGateway), "")
        if model.recoveryNeeded { add(menu, "Restore Previous Claude Setup", #selector(restore), "") }
        menu.addItem(.separator())
        add(menu, "Quit " + hubName, #selector(quit), "q")
    }

    func add(_ menu: NSMenu, _ title: String, _ action: Selector, _ key: String) {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key); item.target = self; menu.addItem(item)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if readyToQuit { return .terminateNow }
        model.updateClaudeRunning()
        model.updateCodexRunning()
        if model.anyOwnedHarnessRunning {
            let alert = NSAlert()
            alert.messageText = "A desktop session is using " + hubName
            alert.informativeText = "Quit the desktop sessions using this gateway so their previous configuration can be restored and active work can finish."
            alert.addButton(withTitle: "Keep Provider Hub Open")
            alert.runModal()
            return .terminateCancel
        }
        if model.activeRequests > 0 || model.busy {
            model.tell("Wait for the current operation to finish before quitting.", error: true); showWindow(); return .terminateCancel
        }
        model.shuttingDown = true
        Task {
            if model.codexRecoveryNeeded && !model.codexRunning {
                do { _ = try await model.command("codex-restore"); model.codexRecoveryNeeded = false }
                catch { model.shuttingDown = false; model.tell(error.localizedDescription, error: true); showWindow(); NSApp.reply(toApplicationShouldTerminate: false); return }
            }
            if model.recoveryNeeded && !model.claudeRunning {
                do { _ = try await model.command("restore"); model.recoveryNeeded = false }
                catch { model.shuttingDown = false; model.tell(error.localizedDescription, error: true); showWindow(); NSApp.reply(toApplicationShouldTerminate: false); return }
            }
            await model.stopGateway()
            readyToQuit = true
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}

@main
struct MistralBridgeMain {
    @MainActor static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.run()
    }
}
