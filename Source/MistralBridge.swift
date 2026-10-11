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
    case connection = "Providers", models = "Models", config = "Config", activity = "Activity"
    var id: String { rawValue }
    var icon: String {
        switch self { case .connection: return "point.3.connected.trianglepath.dotted"; case .models: return "square.stack.3d.up"; case .config: return "slider.horizontal.3"; case .activity: return "waveform.path" }
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
    /// Last sign-in probe per CLI provider: the default login, then each extra account.
    @Published var cliAccountStates: [String: [CliAccountState]] = [:]
    @Published var cliAccountsChecking: Set<String> = []
    @Published var keyAccountStates: [String: [KeyAccountState]] = [:]
    @Published var keyAccountsChecking: Set<String> = []
    var catalogueRefreshTask: Task<Void, Never>?
    @Published var claudeInstalled = false
    @Published var claudeRunning = false
    /// The codex-accent helper holding the DevTools pipe into a Codex session
    /// launched with the power-slider colour switch on.
    var codexAccentProcess: Process?
    /// The helper's stdin, carrying the quick-composer panel's commands.
    var codexAccentInput: Pipe?
    /// The floating recent-chat composer (QuickComposerPanel.swift).
    let quickPanel = QuickPanelModel()
    var openChatWindow: (() -> Void)?
    @Published var chatWindowOpen = false
    @Published var chatWorking = false
    /// The ChatModel's own active-turn signal, propagated by AppController
    /// so the Hub's launch / restore paths can guard on it independently of
    /// ``chatWorking``. The two are normally in lock-step (``onActivity``
    /// forwards ``hasActiveWork``); the dedicated slot lets the auto-stop
    /// and restore paths prefer whichever signal is freshest at call time.
    @Published var chatHasActiveWork = false
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
    /// A launch that went ahead but left something out; the banner shows it
    /// and the menu bar launchers open the window so it is not missed.
    @Published var noticeIsWarning = false
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
    func openChat() { openChatWindow?() }

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

    /// Read legacy mappings without changing a running profile. The first edit
    /// materializes the effective list, including its compaction thresholds.
    var claudeCatalogue: [ClaudeCatalogueEntry] {
        if let curated = settings.claude_catalogue { return curated }
        let seed = seedClaudeCatalogue(mappings: settings.mappings, options: settings.mapping_options)
        if !seed.isEmpty { return seed }
        if let first = availableModels.first(where: { $0.tools ?? true }) {
            return [ClaudeCatalogueEntry(route: first.id, tier: "fable", tier_default: true)]
        }
        return []
    }

    func isClaudeTierDefault(_ entry: ClaudeCatalogueEntry) -> Bool {
        let members = claudeCatalogue.filter { $0.tier == entry.tier }
        let chosen = members.first { $0.tier_default == true } ?? members.first
        return chosen?.route == entry.route
    }

    func makeClaudeTierDefault(_ route: String) {
        var list = claudeCatalogue
        guard let tier = list.first(where: { $0.route == route })?.tier else { return }
        for index in list.indices where list[index].tier == tier { list[index].tier_default = list[index].route == route }
        settings.claude_catalogue = list
    }

    func claudeTier(for route: String) -> Binding<String> {
        Binding(
            get: { self.claudeCatalogue.first { $0.route == route }?.tier ?? "sonnet" },
            set: { tier in
                var list = self.claudeCatalogue
                guard let index = list.firstIndex(where: { $0.route == route }) else { return }
                list[index].tier = tier
                list[index].tier_default = !list.contains { $0.tier == tier && $0.route != route && $0.tier_default == true }
                self.settings.claude_catalogue = list
            }
        )
    }

    func addClaudeRoute(_ route: String, tier: String = "sonnet") {
        var list = claudeCatalogue
        guard !list.contains(where: { $0.route == route }) else { return }
        list.append(ClaudeCatalogueEntry(route: route, tier: tier, tier_default: !list.contains { $0.tier == tier && $0.tier_default == true }))
        settings.claude_catalogue = list
    }

    func addAllClaudeRoutes() {
        for entry in availableModels where entry.tools ?? true { addClaudeRoute(entry.id) }
    }

    func removeClaudeRoute(_ route: String) {
        var list = claudeCatalogue
        guard list.count > 1 else { return }
        list.removeAll { $0.route == route }
        settings.claude_catalogue = list
    }

    func claudeCompactLimit(for route: String) -> Binding<String> {
        Binding(
            get: { self.claudeCatalogue.first { $0.route == route }?.compact_limit.map(String.init) ?? "" },
            set: { text in
                var list = self.claudeCatalogue
                guard let index = list.firstIndex(where: { $0.route == route }) else { return }
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

    /// The category of pending settings change. Returns the top-level
    /// ``ChangeKind`` enum defined in ``LaunchPlan.swift`` so the
    /// resolver can take it as a parameter without coupling to the
    /// ``BridgeModel`` declaration.
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
        mine.codex_quick_composer = savedSettings.codex_quick_composer
        mine.codex_quick_composer_window = savedSettings.codex_quick_composer_window
        mine.codex_hide_usage_banner = savedSettings.codex_hide_usage_banner
        mine.codex_unlock_composer = savedSettings.codex_unlock_composer
        mine.codex_goal_budget = savedSettings.codex_goal_budget
        mine.codex_subagent_rank = savedSettings.codex_subagent_rank
        mine.codex_subagent_route = savedSettings.codex_subagent_route
        if mine == savedSettings { return .codexOnly }
        var prefsOnly = mine
        prefsOnly.auto_stop = savedSettings.auto_stop
        prefsOnly.auto_mode = savedSettings.auto_mode
        prefsOnly.claude_features = savedSettings.claude_features
        prefsOnly.claude_code_settings = savedSettings.claude_code_settings
        prefsOnly.claude_workflows = savedSettings.claude_workflows
        // The gateway re-reads the active CLI account on every turn, so
        // adding, renaming or switching accounts needs no restart either.
        for id in prefsOnly.providers.keys {
            prefsOnly.providers[id]?.cli_accounts = savedSettings.providers[id]?.cli_accounts
            prefsOnly.providers[id]?.cli_account = savedSettings.providers[id]?.cli_account
        }
        let codexChanged = settings.codex_model != savedSettings.codex_model
            || settings.codex_catalogue != savedSettings.codex_catalogue
            || settings.codex_chatgpt_account != savedSettings.codex_chatgpt_account
            || settings.codex_apply_patch_all != savedSettings.codex_apply_patch_all
            || settings.codex_apply_patch != savedSettings.codex_apply_patch
            || settings.codex_apply_patch_exclude != savedSettings.codex_apply_patch_exclude
            || settings.codex_accent_slider != savedSettings.codex_accent_slider
            || settings.codex_quick_composer != savedSettings.codex_quick_composer
            || settings.codex_quick_composer_window != savedSettings.codex_quick_composer_window
            || settings.codex_hide_usage_banner != savedSettings.codex_hide_usage_banner
            || settings.codex_unlock_composer != savedSettings.codex_unlock_composer
            || settings.codex_goal_budget != savedSettings.codex_goal_budget
            || settings.codex_subagent_rank != savedSettings.codex_subagent_rank
            || settings.codex_subagent_route != savedSettings.codex_subagent_route
        if prefsOnly == savedSettings { return codexChanged ? .codexOnly : .prefs }
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
                    let timeout: Double = ["refresh-all", "prepare-launch", "discover", "activate", "codex-prepare", "codex-activate", "cli-accounts"].contains(command) ? 120 : 45
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

    func tell(_ message: String, error: Bool = false, warning: Bool = false) {
        notice = message; noticeIsError = error; noticeIsWarning = warning && !error
    }

    /// One sentence naming what a launch left out, from the prepare step's
    /// `launch_omissions`, or nil when everything selected was served.
    func omissionSummary(_ result: [String: Any]) -> String? {
        guard let items = result["launch_omissions"] as? [[String: Any]], !items.isEmpty else { return nil }
        let parts = items.map { item -> String in
            let label = item["label"] as? String ?? item["route"] as? String ?? "a model"
            var detail = item["reason"] as? String ?? "not available"
            if let replacement = item["replacement"] as? String { detail += "; \(replacement) stands in" }
            return "\(label): \(detail)"
        }
        return "Left out \(items.count == 1 ? "one model" : "\(items.count) models") that cannot be served right now: \(parts.joined(separator: " · ")). The saved selection keeps them for when they return."
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

    /// Surface the existing save rules before a user reaches a failing Save.
    var settingsSaveBlocker: String? {
        guard changed else { return nil }
        if settings.claude_catalogue?.contains(where: { entry in
            entry.compact_limit.map { !(1000...15000000).contains($0) } ?? false
        }) == true { return "Claude compaction thresholds must be 1,000–15,000,000 tokens, or blank for automatic." }
        if changeKind == .prefs { return nil }
        if chatWorking { return "Stop the Chat turn before applying model or provider changes." }
        let claudeLive = claudeRunning && profileActive
        let codexLive = codexRunning && codexRecoveryNeeded
        if activeRequests > 0 { return "Wait for the current requests to finish before applying model changes." }
        switch changeKind {
        case .codexOnly where codexLive:
            return "Quit Codex / ChatGPT to apply its catalogue or configuration changes."
        case .claudeRouting where claudeLive:
            return "Quit Claude to apply its model or provider changes."
        case .mixed where claudeLive || codexLive:
            return "Quit the desktop sessions using this gateway to apply these changes."
        default: return nil
        }
    }

    func save() async throws {
        await waitForCatalogueRefresh()
        updateClaudeRunning()
        updateCodexRunning()
        let kind = changeKind
        if kind == .unchanged { return }
        if chatWorking && kind != .prefs {
            throw WorkerError(message: "Stop the Chat turn before changing model or provider settings.")
        }
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

    /// Settings save used by the launch flow.
    ///
    /// ``save()`` refuses when ``chatWorking`` is true because the Settings
    /// UI is for hand-edited model changes — the user has not asked to act.
    /// A launch *is* an action: the user has explicitly clicked Launch, and
    /// the launch should not be blocked by a Chat turn that is unaffected
    /// by the new settings. The gateway is also left running here; the
    /// snapshot match in ``ensureGatewaySnapshot`` decides whether the
    /// prepared plan still needs a fresh gateway or not.
    func saveForLaunch() async throws {
        await waitForCatalogueRefresh()
        updateClaudeRunning()
        updateCodexRunning()
        let kind = changeKind
        if kind == .unchanged { return }
        // Live-harness-vs-change rules remain: a live app's profile is never
        // rewritten from under it (AGENTS.md "never rewrite a live app's
        // profile under it"). Chat work is intentionally NOT a refusal.
        let claudeLive = claudeRunning && profileActive
        let codexLive = codexRunning && codexRecoveryNeeded
        if activeRequests > 0 || claudeLive || codexLive {
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
        try await writeSettings()
    }

    // MARK: - Launch-plan resolver
    //
    // The launch-decision policy lives in ``Source/LaunchPlan.swift`` as
    // a pure Swift module; the ``BridgeModel`` side just forwards state
    // and exposes thin wrappers so call sites elsewhere in the file read
    // naturally. Tests in ``Source/test_launch_plan.py`` exercise the
    // actual production ``LaunchPlan.resolve`` function via a Swift
    // harness; there is no parallel Python policy.

    /// Pure resolver entry point. Thin wrapper over the module-level
    /// ``resolveLaunchPlan`` so call sites can use Swift method syntax.
    func resolveLaunchPlan(surface: String,
                           gatewayFingerprint: String?,
                           gatewayDigest: String?,
                           preparedFingerprint: String?,
                           preparedDigest: String?) -> LaunchPlan {
        return LaunchPlanResolver.resolve(
            surface: surface,
            gatewayRunning: running,
            gatewayFingerprint: gatewayFingerprint,
            gatewayDigest: gatewayDigest,
            preparedFingerprint: preparedFingerprint,
            preparedDigest: preparedDigest,
            chatWorking: chatWorking,
            chatHasActiveWork: chatHasActiveWork,
            chatWindowOpen: chatWindowOpen,
            activeRequests: activeRequests,
            changeKind: changeKind,
            liveClaude: claudeRunning && profileActive,
            liveCodex: codexRunning && codexRecoveryNeeded)
    }

    /// Predicate for the restore / auto-stop paths. Thin wrapper over
    /// the module-level ``mayStopGateway``.
    func mayStopGateway() -> Bool {
        return LaunchPlanResolver.mayStop(
            liveClaude: claudeRunning && profileActive,
            liveCodex: codexRunning && codexRecoveryNeeded,
            chatWorking: chatWorking,
            chatHasActiveWork: chatHasActiveWork,
            chatWindowOpen: chatWindowOpen,
            activeRequests: activeRequests)
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
        // Snapshot check FIRST. If the live gateway already carries what
        // this launch needs, there is no restart to refuse — chat work and
        // in-flight requests are safe, and the app opens without a hitch.
        // This is the relaxed-launch win the user asked for.
        if try await gatewayMatches(fingerprint: fingerprint, digest: digest) == true { return }
        // Snapshot mismatch — a gateway restart is genuinely needed.
        // The gateway's ``active`` count is GLOBAL, not per-harness;
        // a request in flight could be either harness's during a brief
        // restart transition. The block reason names "the gateway",
        // not a specific harness, so the message is never wrong.
        let chatBusy = chatWorking || chatHasActiveWork
        let surfaceLabel = otherHarness == "Codex / ChatGPT" ? "Claude" : "Codex / ChatGPT"
        if activeRequests > 0 {
            let noun = activeRequests == 1 ? "request" : "requests"
            throw WorkerError(message: "The gateway has \(activeRequests) \(noun) in flight. Wait for them to finish, then launch again so the gateway can load \(surfaceLabel)'s selection.")
        }
        if chatBusy {
            throw WorkerError(message: "Chat is working. Stop the Chat turn, then launch again so the gateway can load \(surfaceLabel)'s selection.")
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
            // ``saveForLaunch`` persists unsaved settings WITHOUT blocking
            // on ``chatWorking`` and WITHOUT stopping the gateway. The
            // gateway-restart decision now lives in ``ensureGatewaySnapshot``
            // and only triggers when the prepared snapshot doesn't match
            // the live one — so a Codex turn in flight is no longer in the
            // way of launching Claude.
            try await saveForLaunch()
            let prepared = try await command("prepare-launch")
            readCatalogue(prepared, modelsKey: "models")
            applyCatalogueDiagnostics(prepared)
            // Start the gateway if it isn't already running for Codex.
            // When Codex is the live harness, the gateway is already up
            // and ``ensureGatewaySnapshot`` will short-circuit on a
            // matching fingerprint — no restart, no chat block.
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
            let base = "Claude is opening with your provider profile. Its previous configuration will be restored after Claude quits."
            if let omitted = omissionSummary(prepared) { tell(base + " " + omitted, warning: true) } else { tell(base) }
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
            // ``mayStopGateway`` is the explicit predicate that honours
            // ChatModel's ``hasActiveWork`` in addition to ``chatWorking``.
            // It used to be implicit in ``!anyOwnedHarnessRunning``, which
            // only folded ``chatWorking`` in via the AppController's
            // activity bridge — the dedicated predicate closes the gap
            // between the two signals at the call site.
            if mayStopGateway() { await stopGateway() }
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
                // ``mayStopGateway`` folds ChatModel's ``hasActiveWork`` into
                // the predicate in addition to ``chatWorking``, so a turn
                // still in flight is enough to keep the gateway alive even
                // if no desktop harness is currently running.
                if savedSettings.auto_stop && mayStopGateway() { await stopGateway() }
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
            failures = status["failed"] as? Int ?? 0
            inputTokens = status["total_input_tokens"] as? Int ?? status["input_tokens"] as? Int ?? 0
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
        guard !busy, let provider = providerDefinitions.first(where: { $0.id == selectedProvider }), provider.credential_account != nil else { return }
        let providerID = provider.id
        // The active extra account gets its own Keychain item; nil keeps
        // the item every earlier build wrote (see hub_config.keychain_account).
        let account = keychainAccount(providerID, id: settings.providers[providerID]?.key_account)
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
            if keyAccountStates[providerID] != nil { await checkKeyAccounts(providerID) }
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

    // MARK: extra API keys

    /// Keychain account name for one of a provider's key slots; nil id is the
    /// default slot. Mirrors hub_config.keychain_account so the gateway reads
    /// the item the app wrote.
    func keychainAccount(_ provider: String, id: String?) -> String {
        let base = providerDefinitions.first { $0.id == provider }?.credential_account ?? provider.uppercased() + "_API_KEY"
        return id.map { base + "." + $0 } ?? base
    }

    /// Ask Keychain which of a provider's key slots hold a key. Sends the
    /// pane's current settings, so a just-added account shows up unsaved.
    func checkKeyAccounts(_ provider: String) async {
        guard !keyAccountsChecking.contains(provider) else { return }
        keyAccountsChecking.insert(provider); defer { keyAccountsChecking.remove(provider) }
        do {
            let result = try await command("key-accounts", provider: provider, input: try JSONEncoder().encode(settings))
            keyAccountStates[provider] = (result["accounts"] as? [[String: Any]] ?? []).map {
                KeyAccountState(id: $0["id"] as? String, label: $0["label"] as? String ?? "",
                                active: $0["active"] as? Bool ?? false, found: $0["found"] as? Bool ?? false)
            }
        } catch { tell(error.localizedDescription, error: true) }
    }

    /// Add an empty key slot and make it active, so the next pasted key
    /// lands in it. The name is edited inline.
    func addKeyAccount(_ provider: String) {
        var accounts = settings.providers[provider]?.key_accounts ?? []
        guard accounts.count < 8 else { return }
        var number = accounts.count + 2
        while accounts.contains(where: { $0.id == "account-\(number)" }) { number += 1 }
        let account = KeyAccount(id: "account-\(number)", label: "Account \(number)")
        accounts.append(account)
        settings.providers[provider]?.key_accounts = accounts
        settings.providers[provider]?.key_account = account.id
        keyAccountStates[provider]?.append(KeyAccountState(id: account.id, label: account.label, active: true, found: false))
    }

    /// Forget an extra key slot and delete its Keychain item; the default
    /// slot cannot be removed.
    func removeKeyAccount(_ provider: String, id: String) {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: hubKeychainService,
                                    kSecAttrAccount as String: keychainAccount(provider, id: id)]
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { tell("Keychain could not remove that key (\(status)).", error: true); return }
        var accounts = settings.providers[provider]?.key_accounts ?? []
        accounts.removeAll { $0.id == id }
        settings.providers[provider]?.key_accounts = accounts.isEmpty ? nil : accounts
        if settings.providers[provider]?.key_account == id { settings.providers[provider]?.key_account = nil }
        keyAccountStates[provider]?.removeAll { $0.id == id }
    }

    /// Ask each of a provider's CLI logins whether it is signed in. Sends the
    /// pane's current settings, so an account added but not yet saved is
    /// checked too.
    func checkCliAccounts(_ provider: String) async {
        guard !cliAccountsChecking.contains(provider) else { return }
        cliAccountsChecking.insert(provider); defer { cliAccountsChecking.remove(provider) }
        do {
            let result = try await command("cli-accounts", provider: provider, input: try JSONEncoder().encode(settings))
            cliAccountStates[provider] = (result["accounts"] as? [[String: Any]] ?? []).map {
                CliAccountState(id: $0["id"] as? String, label: $0["label"] as? String ?? "",
                                config_dir: $0["config_dir"] as? String, active: $0["active"] as? Bool ?? false,
                                state: $0["state"] as? String ?? "unknown", detail: $0["detail"] as? String ?? "")
            }
        } catch {
            tell(error.localizedDescription, error: true)
        }
    }

    /// Pick (or create) the config folder for another CLI login. The CLI signs
    /// in to it itself; the hub only stores the path.
    func addCliAccount(_ provider: String) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true; panel.canChooseFiles = false
        panel.canCreateDirectories = true; panel.showsHiddenFiles = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = FileManager.default.homeDirectoryForCurrentUser
        panel.message = provider == "claude"
            ? "Choose or create a folder for this Claude account, e.g. ~/.claude-work. Claude Code keeps that account's login there."
            : "Choose or create a folder for this Codex account, e.g. ~/.codex-lite. Codex keeps that account's login there."
        panel.prompt = "Use Folder"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        var accounts = settings.providers[provider]?.cli_accounts ?? []
        guard !accounts.contains(where: { $0.config_dir == url.path }) else {
            tell("That folder is already one of this provider’s accounts.", error: true); return
        }
        let name = url.lastPathComponent.trimmingCharacters(in: CharacterSet(charactersIn: "."))
        let label = name.isEmpty ? "Account \(accounts.count + 2)" : String(name.prefix(60))
        let base = String(label.lowercased().map { $0.isASCII && ($0.isLetter || $0.isNumber) ? $0 : "-" }
            .drop { $0 == "-" }.prefix(24))
        var id = base.isEmpty ? "account" : base
        var suffix = 2
        while accounts.contains(where: { $0.id == id }) { id = (base.isEmpty ? "account" : base) + "-\(suffix)"; suffix += 1 }
        accounts.append(CliAccount(id: id, label: label, config_dir: url.path))
        settings.providers[provider]?.cli_accounts = accounts
    }

    func removeCliAccount(_ provider: String, id: String) {
        var accounts = settings.providers[provider]?.cli_accounts ?? []
        accounts.removeAll { $0.id == id }
        settings.providers[provider]?.cli_accounts = accounts.isEmpty ? nil : accounts
        if settings.providers[provider]?.cli_account == id { settings.providers[provider]?.cli_account = nil }
        cliAccountStates[provider]?.removeAll { $0.id == id }
    }

    /// Open Terminal on the CLI's own browser sign-in, pointed at the
    /// account's folder. The CLI owns the whole OAuth flow and the login it
    /// leaves behind; nothing passes through the hub.
    func signInCliAccount(_ provider: String, folder: String?) {
        func quote(_ value: String) -> String { "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'" }
        let variable = provider == "claude" ? "CLAUDE_CONFIG_DIR" : "CODEX_HOME"
        // Codex turns read the login from auth.json (see codex_cli_agent's
        // credentials-store override), so the sign-in stores it there too.
        let login = provider == "claude" ? "claude auth login && claude auth status"
            : "codex login -c " + quote("cli_auth_credentials_store=\"file\"") + " && codex login status"
        let script = folder.map { "export \(variable)=\(quote($0)) && mkdir -p \"$\(variable)\" && " + login } ?? login
        let escaped = script.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
        let source = "tell application \"Terminal\"\nactivate\ndo script \"\(escaped)\"\nend tell"
        var error: NSDictionary?
        NSAppleScript(source: source)?.executeAndReturnError(&error)
        if error != nil { tell("Terminal could not open. Run this yourself, then check again: " + script, error: true) }
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
    @State private var clientSelection: HubClientSelection = .both
    @State private var windowWidth: CGFloat = 1200
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
                            HStack(spacing: 11) { Image(systemName: page.icon).frame(width: 18); Text(page.rawValue).lineLimit(1); Spacer() }
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
                        case .models, .config:
                            HubClientWorkspace(model: model, selection: $clientSelection, allowsSplit: windowWidth >= 1080, configuration: model.page == .config)
                        case .activity: activityPage
                        }
                    }.padding(30).disabled(model.busy)
                }
                if model.page == .models || model.page == .config { settingsSaveBar }
                if !model.notice.isEmpty {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: model.noticeIsError ? "exclamationmark.circle" : (model.noticeIsWarning ? "exclamationmark.triangle" : "checkmark.circle"))
                            .foregroundStyle(model.noticeIsError ? Color.orange : (model.noticeIsWarning ? Color.yellow : .green))
                        Text(model.notice).font(.system(size: 12)).lineSpacing(3).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                        Button { model.notice = "" } label: { Image(systemName: "xmark").font(.system(size: 10)) }.buttonStyle(.plain).foregroundStyle(.secondary)
                    }.padding(16).background(Color.white.opacity(0.045)).overlay(alignment: .top) { Divider() }
                }
            }.frame(maxWidth: .infinity)
        }.background(Color(red: 0.095, green: 0.098, blue: 0.102)).accentColor(bridgeOrange)
            .frame(minWidth: 870, idealWidth: 1200, minHeight: 680, idealHeight: 820)
            .background(GeometryReader { proxy in
                Color.clear.onAppear { windowWidth = proxy.size.width }
                    .onChange(of: proxy.size.width) { _, width in windowWidth = width }
            })
    }

    private var settingsSaveBar: some View {
        HStack(spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text(model.changed ? "Unsaved changes" : "All changes saved").font(.system(size: 12, weight: .medium))
                Text(model.settingsSaveBlocker ?? "App preferences apply on the next launch.")
                    .font(.system(size: 11)).foregroundStyle(model.settingsSaveBlocker == nil ? Color.secondary : Color.orange)
            }.frame(maxWidth: .infinity, alignment: .leading)
            Button {
                Task { await model.saveFromUI() }
            } label: {
                Text("Save changes").foregroundStyle(HubTheme.Control.ink)
            }
                .buttonStyle(.borderedProminent)
                .tint(HubTheme.Accent.brand)
                .disabled(model.busy || !model.changed || model.settingsSaveBlocker != nil)
        }.padding(.horizontal, 24).padding(.vertical, 13)
            .background(Color.white.opacity(0.045)).overlay(alignment: .top) { Divider() }
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
        case .models: return "Choose what appears in each desktop app."
        case .config: return "Set up each app and your shared gateway."
        case .activity: return "Requests through your local gateway."
        }
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
    var chatWindow: NSWindow?
    var chatModel: ChatModel!
    var readyToQuit = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        HubTheme.Appearance.apply()
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
        editItem.submenu = editMenu; mainMenu.addItem(editItem)
        let viewItem = NSMenuItem()
        viewItem.submenu = ChatZoomCommands.shared.menu(); mainMenu.addItem(viewItem)
        NSApp.mainMenu = mainMenu
        model = BridgeModel()
        chatModel = ChatModel(bridge: model)
        // Mirror the ChatModel's own active-turn signal so restore/auto-stop
        // can guard on it independently of ``chatWorking``. ``onActivity``
        // fires on every transition of ``hasActiveWork``; the closures
        // below keep the two slots synchronised on the main actor.
        let mirrorActivity: (Bool) -> Void = { [weak self] working in
            self?.model.chatWorking = working
            self?.model.chatHasActiveWork = working
        }
        chatModel.onActivity = mirrorActivity
        chatModel.onSurfaceChange = { [weak self] in
            guard let self else { return }
            // Re-read on surface change so a session switch that leaves
            // active work behind still flips the slot.
            self.model.chatHasActiveWork = self.chatModel.hasActiveWork
            self.applyWindowStyle()
        }
        model.openChatWindow = { [weak self] in self?.showChat() }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "point.3.connected.trianglepath.dotted", accessibilityDescription: hubName)
        statusItem.button?.image?.isTemplate = true
        statusItem.button?.toolTip = hubName
        let menu = NSMenu(); menu.delegate = self; statusItem.menu = menu
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1200, height: 820), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = hubName
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.delegate = self
        applyDesign()
        window.center()
        showWindow()
        HubUpdater.shared.start(python: model.python, worker: model.helper.deletingLastPathComponent(), root: model.root) { [weak self] in
            guard let self else { return "Provider Hub is closing." }
            return self.updateRestartBlocker()
        }
    }

    func updateRestartBlocker() -> String? {
        let desktopIDs: Set<String> = ["com.openai.codex", "com.openai.chat", "com.anthropic.claudefordesktop"]
        return HubUpdateRestartPolicy.blocker(
            desktopOpen: NSWorkspace.shared.runningApplications.contains { !$0.isTerminated && desktopIDs.contains($0.bundleIdentifier ?? "") },
            activeWork: model.activeRequests > 0 || model.busy || model.chatWorking || chatModel.hasActiveWork,
            unsavedSettings: model.changed, drafts: chatModel.hasUnsentDrafts)
    }

    /// "compact" is the compact shell (CompactShell.swift); "classic" is the
    /// original pages. Both drive the same model, so switching mid-edit keeps
    /// unsaved changes. The choice persists in UserDefaults.
    var design: String { UserDefaults.standard.string(forKey: "hubDesign") ?? "compact" }
    func applyDesign() {
        if design == "classic" {
            window.styleMask.remove(.fullSizeContentView)
            window.titleVisibility = .visible
            window.contentView = NSHostingView(rootView: BridgeWindow(model: model))
            window.minSize = NSSize(width: 870, height: 680)
            window.setContentSize(NSSize(width: 1200, height: 820))
        } else {
            window.styleMask.insert(.fullSizeContentView)
            window.titleVisibility = .hidden
            window.contentView = NSHostingView(rootView: CompactShell(model: model))
            window.minSize = NSSize(width: 760, height: 500)
            window.setContentSize(NSSize(width: 840, height: 560))
        }
        applyWindowStyle()
    }
    /// Both Hub windows follow one surface preference without rebuilding state.
    func applyWindowStyle() {
        let glass = design != "classic" && HubTheme.WindowStyle.mode == .glass
        window.isOpaque = !glass
        window.backgroundColor = glass ? .clear : .windowBackgroundColor
        let chatGlass = HubTheme.WindowStyle.mode == .glass
        chatWindow?.isOpaque = !chatGlass
        chatWindow?.backgroundColor = chatGlass ? .clear : .windowBackgroundColor
    }
    @objc func chooseCompact() { UserDefaults.standard.set("compact", forKey: "hubDesign"); applyDesign() }
    @objc func chooseClassic() { UserDefaults.standard.set("classic", forKey: "hubDesign"); applyDesign() }

    /// System, Light or Dark. The choice persists beside the design choice and
    /// takes effect at once: NSApp.appearance drives every window and every
    /// dynamic colour in HubTheme.Semantic.
    @objc func showQuickComposer() { model.quickPanel.show() }

    @objc func chooseSystemAppearance() { chooseAppearance(.system) }
    @objc func chooseLightAppearance() { chooseAppearance(.light) }
    @objc func chooseDarkAppearance() { chooseAppearance(.dark) }
    func chooseAppearance(_ mode: HubTheme.Appearance.Mode) {
        HubTheme.Appearance.mode = mode
        HubTheme.Appearance.apply()
    }

    @objc func showWindow() {
        // The app lives in the menu bar (LSUIElement). While its window is
        // open it joins the Dock with the real icon; closing the window
        // returns it to the menu bar only. windowWillClose does the reverse.
        NSApp.setActivationPolicy(.regular)
        window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    @objc func showChat() {
        if chatWindow == nil {
            let chat = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 940, height: 700),
                                styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                                backing: .buffered, defer: false)
            chat.title = "Chat · Provider Hub"; chat.titleVisibility = .hidden
            chat.titlebarAppearsTransparent = true; chat.isOpaque = false; chat.backgroundColor = .clear
            chat.isReleasedWhenClosed = false; chat.delegate = self
            chat.minSize = NSSize(width: 720, height: 500)
            chat.contentView = NSHostingView(rootView: ChatWindow(model: chatModel))
            chat.setFrameAutosaveName("ProviderHubChat"); chat.center(); chatWindow = chat
            ChatZoomCommands.shared.window = chat
            applyWindowStyle()
        }
        model.chatWindowOpen = true
        NSApp.setActivationPolicy(.regular)
        chatWindow?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        Task { await chatModel.start() }
    }
    func windowWillClose(_ notification: Notification) {
        let closing = notification.object as? NSWindow
        if closing === chatWindow { model.chatWindowOpen = false }
        if (closing === window && chatWindow?.isVisible != true) || (closing === chatWindow && !window.isVisible) {
            NSApp.setActivationPolicy(.accessory)
        }
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if !hasVisibleWindows { showWindow() }
        return true
    }
    @objc func launchClaude() { Task { await model.launchClaude(); if model.noticeIsError || model.noticeIsWarning { showWindow() } } }
    @objc func launchCodex() { Task { await model.launchCodex(); if model.noticeIsError || model.noticeIsWarning { showWindow() } } }
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
        add(menu, "Chat with Model…", #selector(showChat), "k")
        if model.settings.codex_quick_composer && model.codexAccentInput != nil {
            add(menu, "Recent chats…", #selector(showQuickComposer), "")
        }
        add(menu, "Models & Settings…", #selector(showWindow), ",")
        let designItem = NSMenuItem(title: "Design", action: nil, keyEquivalent: "")
        let designMenu = NSMenu(title: "Design")
        for (title, key, action) in [("Compact", "compact", #selector(chooseCompact)), ("Classic", "classic", #selector(chooseClassic))] {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: ""); item.target = self
            item.state = design == key ? .on : .off; designMenu.addItem(item)
        }
        designItem.submenu = designMenu; menu.addItem(designItem)
        let appearanceItem = NSMenuItem(title: "Appearance", action: nil, keyEquivalent: "")
        let appearanceMenu = NSMenu(title: "Appearance")
        for (mode, action) in [(HubTheme.Appearance.Mode.system, #selector(chooseSystemAppearance)),
                               (.light, #selector(chooseLightAppearance)), (.dark, #selector(chooseDarkAppearance))] {
            let item = NSMenuItem(title: mode.title, action: action, keyEquivalent: ""); item.target = self
            item.state = HubTheme.Appearance.mode == mode ? .on : .off; appearanceMenu.addItem(item)
        }
        appearanceItem.submenu = appearanceMenu; menu.addItem(appearanceItem)
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
        if HubUpdater.shared.working { return .terminateCancel }
        if HubUpdater.shared.restartRequested, let reason = updateRestartBlocker() {
            HubUpdater.shared.deferRestart(reason)
            return .terminateCancel
        }
        model.updateClaudeRunning()
        model.updateCodexRunning()
        if model.desktopOwnedHarnessRunning {
            let alert = NSAlert()
            alert.messageText = "A desktop session is using " + hubName
            alert.informativeText = "Quit the desktop sessions using this gateway so their previous configuration can be restored and active work can finish."
            alert.addButton(withTitle: "Keep Provider Hub Open")
            alert.runModal()
            return .terminateCancel
        }
        if model.activeRequests > 0 || model.busy || model.chatWorking || chatModel.hasActiveWork {
            model.tell("Wait for the current operation to finish before quitting.", error: true); showWindow(); return .terminateCancel
        }
        model.shuttingDown = true
        chatModel.shutdown()
        Task {
            if model.codexRecoveryNeeded && !model.codexRunning {
                do { _ = try await model.command("codex-restore"); model.codexRecoveryNeeded = false }
                catch {
                    model.shuttingDown = false
                    if HubUpdater.shared.restartRequested { HubUpdater.shared.deferRestart("Click Restart to retry after recovery.") }
                    model.tell(error.localizedDescription, error: true); showWindow(); NSApp.reply(toApplicationShouldTerminate: false); return
                }
            }
            if model.recoveryNeeded && !model.claudeRunning {
                do { _ = try await model.command("restore"); model.recoveryNeeded = false }
                catch {
                    model.shuttingDown = false
                    if HubUpdater.shared.restartRequested { HubUpdater.shared.deferRestart("Click Restart to retry after recovery.") }
                    model.tell(error.localizedDescription, error: true); showWindow(); NSApp.reply(toApplicationShouldTerminate: false); return
                }
            }
            await model.stopGateway()
            do { try HubUpdater.shared.launchInstaller() }
            catch {
                model.shuttingDown = false
                HubUpdater.shared.deferRestart("Click Restart to retry.")
                model.tell(error.localizedDescription, error: true); showWindow()
                NSApp.reply(toApplicationShouldTerminate: false); return
            }
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
