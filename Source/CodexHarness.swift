import AppKit
import SwiftUI

struct CodexModelOption: Decodable, Identifiable {
    var id: String
    var name: String
    var context: Int?
    var description: String
}

extension BridgeModel {
    var desktopOwnedHarnessRunning: Bool {
        (claudeRunning && profileActive) || (codexRunning && codexRecoveryNeeded)
    }
    var anyOwnedHarnessRunning: Bool { desktopOwnedHarnessRunning || chatWindowOpen || chatWorking }

    func readCodexState(_ object: [String: Any]) {
        if let raw = object["codex_models"], let data = try? JSONSerialization.data(withJSONObject: raw),
           let decoded = try? JSONDecoder().decode([CodexModelOption].self, from: data) { codexModels = decoded }
        if let path = object["codex_app_path"] as? String { codexAppPath = path }
        if let value = object["codex_running"] as? Bool { codexRunning = value }
        if let value = object["codex_profile_active"] as? Bool { codexProfileActive = value }
        if let value = object["codex_recovery_needed"] as? Bool { codexRecoveryNeeded = value }
    }

    func updateCodexRunning() {
        codexRunning = NSWorkspace.shared.runningApplications.contains { $0.bundleIdentifier == "com.openai.codex" && !$0.isTerminated }
    }

    /// A warning naming Desktop projects whose primary folder is the disk
    /// root, from the worker's `codex_disk_root_projects`, or nil when there
    /// are none. Desktop accepts one message in such a project's chats, then
    /// refuses the rest with “Select a project to continue” (codex_projects.py).
    func diskRootProjectSummary(_ result: [String: Any]) -> String? {
        guard let names = result["codex_disk_root_projects"] as? [String], !names.isEmpty else { return nil }
        let quoted = names.map { "“\($0)”" }
        let list = quoted.count == 1 ? quoted[0] : quoted.dropLast().joined(separator: ", ") + " and " + quoted[quoted.count - 1]
        let one = names.count == 1
        return (one ? "Desktop project \(list) uses" : "Desktop projects \(list) use")
            + " the whole disk (/) as \(one ? "its" : "their") folder, so Desktop refuses every message after the first in \(one ? "its" : "their") chats with “Select a project to continue”."
            + " In Desktop, choose Edit project, add a specific folder (your home folder works) and make it primary, then start a new chat; chats already started there can stay blocked."
    }

    func launchCodex() async {
        guard !busy else { return }
        page = .config
        updateCodexRunning()
        if codexRunning && codexProfileActive {
            NSWorkspace.shared.runningApplications.first { $0.bundleIdentifier == "com.openai.codex" }?.activate(options: [.activateAllWindows])
            return
        }
        guard let appPath = codexAppPath else { tell("Install the Codex / ChatGPT desktop app first.", error: true); return }
        guard settings.codex_model != nil else { tell("Choose a model for Codex / ChatGPT first.", error: true); return }
        busy = true; defer { busy = false }
        do {
            if codexRecoveryNeeded && !codexRunning {
                _ = try await command("codex-restore")
                codexRecoveryNeeded = false; codexProfileActive = false
            }
            tell("Preparing the Codex / ChatGPT model catalogue…")
            await waitForCatalogueRefresh()
            // ``saveForLaunch`` persists Codex-only edits without stopping
            // the gateway and without blocking on Chat. The live-harness
            // refusal (``codexLive`` while editing Codex) still applies —
            // see ``resolveLaunchPlan`` for the named rules.
            try await saveForLaunch()
            let prepared = try await command("codex-prepare")
            readCatalogue(prepared, modelsKey: "models")
            try await startGateway()
            try await ensureGatewaySnapshot(fingerprint: nil, digest: prepared["catalogue_digest"] as? String, otherHarness: "Claude")
            updateCodexRunning()
            if codexRunning {
                let alert = NSAlert()
                alert.messageText = "Restart Codex / ChatGPT with Provider Hub?"
                alert.informativeText = "Restarting closes its current desktop sessions and interrupts work still running there. Conversations remain saved. Your previous model configuration will be restored when Codex / ChatGPT quits."
                alert.addButton(withTitle: "Cancel")
                alert.addButton(withTitle: "Restart with Provider Hub")
                guard alert.runModal() == .alertSecondButtonReturn else {
                    if mayStopGateway() { await stopGateway() }
                    tell("Codex / ChatGPT setup is ready. Launch it here when you are ready to restart.")
                    return
                }
                guard let app = NSWorkspace.shared.runningApplications.first(where: { $0.bundleIdentifier == "com.openai.codex" }), app.terminate() else {
                    throw WorkerError(message: "Codex / ChatGPT could not quit. Finish its current work and quit it, then launch here.")
                }
                for _ in 0..<60 {
                    try await Task.sleep(nanoseconds: 250_000_000)
                    updateCodexRunning()
                    if !codexRunning { break }
                }
                guard !codexRunning else { throw WorkerError(message: "Codex / ChatGPT is still closing. Its configuration has not been switched.") }
            }
            readCodexState(try await command("codex-activate"))
            codexRecoveryNeeded = true; codexProfileActive = true
            observedOwnedCodex = false; codexLaunchTime = Date()
            if savedSettings.codex_accent_slider || savedSettings.codex_quick_composer {
                try await launchCodexWithAccentBridge(appPath: appPath)
                let base = "Codex / ChatGPT is opening with your provider catalogue and desktop preferences. Its previous configuration will be restored after it quits."
                let notes = [omissionSummary(prepared), diskRootProjectSummary(prepared)].compactMap { $0 }
                if notes.isEmpty { tell(base) } else { tell(([base] + notes).joined(separator: " "), warning: true) }
            } else {
                let configuration = NSWorkspace.OpenConfiguration()
                configuration.activates = true
                try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                    NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: appPath), configuration: configuration) { _, error in
                        if let error { continuation.resume(throwing: error) } else { continuation.resume() }
                    }
                }
                let base = "Codex / ChatGPT is opening with your provider catalogue. Its previous configuration will be restored after it quits."
                let notes = [omissionSummary(prepared), diskRootProjectSummary(prepared)].compactMap { $0 }
                if notes.isEmpty { tell(base) } else { tell(([base] + notes).joined(separator: " "), warning: true) }
            }
        } catch {
            updateCodexRunning()
            if codexRecoveryNeeded && !codexRunning {
                if (try? await command("codex-restore")) != nil { codexRecoveryNeeded = false; codexProfileActive = false }
            }
            tell(error.localizedDescription, error: true)
        }
    }

    /// Start the desktop app through the worker's codex-accent helper, which
    /// holds a DevTools pipe into the app and tints the power slider per model.
    /// The helper lives as long as the app and is never terminated from here:
    /// closing its pipe is the app's cue to quit.
    func launchCodexWithAccentBridge(appPath: String) async throws {
        guard let python else { throw WorkerError(message: "Provider Hub needs Python 3.11 or newer for the accent helper.") }
        if let existing = codexAccentProcess, existing.isRunning {
            throw WorkerError(message: "A previous Codex accent helper is still running. Quit Codex / ChatGPT, then launch again.")
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [helper.path, "codex-accent", "--app", appPath]
        process.environment = workerEnvironment
        let output = Pipe(), errors = Pipe(), input = Pipe()
        process.standardOutput = output; process.standardError = errors
        // The helper reads the hub's commands for the quick-composer panel on
        // its stdin (codex_quick_host.py); nothing else travels that way.
        process.standardInput = input
        try process.run()
        codexAccentProcess = process
        codexAccentInput = input
        quickPanel.send = { [weak self] line in
            guard let handle = self?.codexAccentInput?.fileHandleForWriting, let data = line.data(using: .utf8) else { return }
            try? handle.write(contentsOf: data)
        }
        // The helper reports the app's spawn as a JSON line; an early exit
        // means the app never started (or handed off to a running copy).
        // The same reader then relays the panel's events for as long as the
        // helper lives, so its output never blocks on a full pipe.
        let launched = await withCheckedContinuation { (continuation: CheckedContinuation<Bool, Never>) in
            let lock = NSLock()
            var finished = false
            func finish(_ value: Bool) {
                lock.lock(); defer { lock.unlock() }
                if !finished { finished = true; continuation.resume(returning: value) }
            }
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                var buffer = Data()
                while true {
                    let chunk = output.fileHandleForReading.availableData
                    if chunk.isEmpty { break }
                    buffer.append(chunk)
                    while let newline = buffer.firstIndex(of: UInt8(ascii: "\n")) {
                        let line = buffer[buffer.startIndex..<newline]
                        buffer.removeSubrange(buffer.startIndex...newline)
                        guard let object = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any],
                              let event = object["event"] as? String else { continue }
                        if event == "launched" { finish(true) }
                        if event == "host" || event.hasPrefix("quick-") {
                            DispatchQueue.main.async { self?.quickPanel.handle(event: object) }
                        }
                    }
                }
                finish(false)
                DispatchQueue.main.async {
                    guard let self, self.codexAccentInput === input else { return }
                    self.codexAccentInput = nil
                    self.quickPanel.helperGone()
                }
            }
            DispatchQueue.global(qos: .utility).async { _ = errors.fileHandleForReading.readDataToEndOfFile() }
            DispatchQueue.global().asyncAfter(deadline: .now() + 20) { finish(false) }
        }
        guard launched else {
            if process.isRunning { process.terminate() }
            codexAccentProcess = nil
            codexAccentInput = nil
            throw WorkerError(message: "Codex / ChatGPT did not start through the accent helper. Turn the power-slider colour switch off to launch it the usual way.")
        }
        quickPanel.send?("{\"command\":\"hello\"}\n")
    }

    func restoreCodex() async {
        guard !busy else { return }
        busy = true; defer { busy = false }
        do {
            let result = try await command("codex-restore")
            codexRecoveryNeeded = false; codexProfileActive = false; observedOwnedCodex = false
            // ``mayStopGateway`` is the explicit predicate that honours
            // ChatModel's ``hasActiveWork`` in addition to ``chatWorking``.
            // Closing Codex while a Chat turn is in flight must NOT tear
            // the gateway down underneath it.
            if mayStopGateway() { await stopGateway() }
            let preserved = result["preserved_external_changes"] as? Int ?? 0
            tell(preserved > 0 ? "Codex / ChatGPT setup restored. Later configuration edits were preserved." : "The previous Codex / ChatGPT configuration was restored. Conversations remain saved.")
        } catch { tell(error.localizedDescription, error: true) }
    }

    func pollCodexRecovery() async {
        if codexProfileActive && codexRunning { observedOwnedCodex = true }
        let failedLaunch = codexProfileActive && !codexRunning && !observedOwnedCodex && Date().timeIntervalSince(codexLaunchTime) > 20
        guard codexRecoveryNeeded && !codexRunning && (observedOwnedCodex || failedLaunch) && !busy && !finishingSession else { return }
        finishingSession = true; defer { finishingSession = false }
        do {
            _ = try await command("codex-restore")
            codexRecoveryNeeded = false; codexProfileActive = false; observedOwnedCodex = false
            // See the matching comment in MistralBridge.swift's ``poll()``:
            // ``mayStopGateway`` honours ChatModel's ``hasActiveWork`` so
            // an in-flight Chat turn keeps the gateway alive.
            if savedSettings.auto_stop && mayStopGateway() { await stopGateway() }
            tell("Codex / ChatGPT closed. Its previous configuration has been restored.")
        } catch { tell(error.localizedDescription, error: true) }
    }
}

/// Catalogue controls shared by the wide and narrow Models layouts.
struct CodexModelsPane: View {
    @ObservedObject var model: BridgeModel
    @State private var search = ""
    @State private var expandedRoutes: Set<String> = []

    // A legacy all-compatible catalogue stays intact until the first edit.
    // Reading or switching tabs must never make unsaved configuration changes.
    var curatedRoutes: [String] {
        if let curated = model.settings.codex_catalogue { return curated }
        var routes = model.codexModels.map(\.id)
        if let starting = model.settings.codex_model, !routes.contains(starting) { routes.append(starting) }
        return routes
    }
    var advertised: Set<String> { Set(model.availableModels.flatMap { [$0.id] + ($0.aliases ?? []) }) }
    var missingSelections: [String] { curatedRoutes.filter { !advertised.contains($0) } }
    var defaultOptions: [CodexModelOption] {
        curatedRoutes.map { route in
            model.codexModels.first { $0.id == route }
                ?? CodexModelOption(id: route, name: model.modelLabel(route),
                                    context: model.modelEntry(route)?.context,
                                    description: model.modelFacts(route))
        }
    }
    var filteredRoutes: [String] {
        let query = search.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return curatedRoutes }
        return curatedRoutes.filter { model.modelLabel($0).localizedCaseInsensitiveContains(query) || $0.localizedCaseInsensitiveContains(query) }
    }
    static let subagentPoolSize = 5
    var subagentRanks: [String: Int] { model.settings.codex_subagent_rank ?? [:] }

    func materializeCatalogue() {
        if model.settings.codex_catalogue == nil && !curatedRoutes.isEmpty {
            model.settings.codex_catalogue = curatedRoutes
        }
    }

    func pruneSelections(to routes: [String]) {
        let kept = subagentRanks.filter { routes.contains($0.key) }
        model.settings.codex_subagent_rank = kept.isEmpty ? nil : kept
        if let route = model.settings.codex_subagent_route, !routes.contains(route) {
            model.settings.codex_subagent_route = nil
        }
    }

    func removeRoute(_ route: String) {
        guard curatedRoutes.count > 1 else { return }
        let list = curatedRoutes.filter { $0 != route }
        model.settings.codex_catalogue = list
        if model.settings.codex_model == route { model.settings.codex_model = list.first }
        pruneSelections(to: list)
    }

    func addRoute(_ route: String) {
        guard !curatedRoutes.contains(route) else { return }
        model.settings.codex_catalogue = curatedRoutes + [route]
        if model.settings.codex_model == nil { model.settings.codex_model = route }
    }

    func addAll() {
        var routes = curatedRoutes
        for entry in model.availableModels where (entry.tools ?? true) && !routes.contains(entry.id) {
            routes.append(entry.id)
        }
        guard !routes.isEmpty else { return }
        model.settings.codex_catalogue = routes
        if model.settings.codex_model == nil { model.settings.codex_model = routes.first }
    }

    func keepStartingModel() {
        guard let keep = curatedRoutes.contains(model.settings.codex_model ?? "") ? model.settings.codex_model : curatedRoutes.first else { return }
        model.settings.codex_catalogue = [keep]
        model.settings.codex_model = keep
        pruneSelections(to: [keep])
    }

    func setRank(_ route: String, _ rank: Int?) {
        materializeCatalogue()
        var ranks = subagentRanks
        if let rank { ranks[route] = rank } else { ranks.removeValue(forKey: route) }
        model.settings.codex_subagent_rank = ranks.isEmpty ? nil : ranks
    }

    /// The model in a priority slot. The per-card picker can leave two models
    /// on one rank; the slot shows the one Codex orders first (the starting
    /// model, then by name) and says how many more share it.
    func slotRoutes(_ rank: Int) -> [String] {
        subagentRanks.filter { $0.value == rank }.map(\.key).sorted {
            let lhs = model.settings.codex_model == $0, rhs = model.settings.codex_model == $1
            return lhs != rhs ? lhs : model.modelLabel($0) < model.modelLabel($1)
        }
    }

    /// Put one model in a slot, replacing whatever held it; nil empties it.
    func setSlot(_ rank: Int, _ route: String?) {
        materializeCatalogue()
        var ranks = subagentRanks.filter { $0.value != rank }
        if let route { ranks[route] = rank }
        model.settings.codex_subagent_rank = ranks.isEmpty ? nil : ranks
    }

    func contextWindow(_ route: String) -> Int? {
        if let entry = model.modelEntry(route) {
            if let runtime = entry.runtime_context, runtime > 0 { return runtime }
            if let context = entry.context, context > 0 { return context }
            if let context = entry.context_options?.filter({ $0 > 0 }).max() { return context }
        }
        return model.codexModels.first { $0.id == route }?.context
    }

    func compactionDescription(_ route: String) -> String {
        guard let context = contextWindow(route), context > 0 else { return "Automatic · context limit not reported" }
        let maxInput = model.modelEntry(route)?.max_input ?? context
        let effective = min(context, maxInput > 0 ? maxInput : context)
        return "\(Int(Double(effective) * 0.85).formatted()) tokens · automatic (85% of input window)"
    }

    var body: some View {
        Panel {
            HubPaneHeading(title: "Codex / ChatGPT", subtitle: "Curated catalogue", icon: "terminal")
                HStack {
                    Text("\(curatedRoutes.count) models in Codex’s picker").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    addMenu
                }
                TextField("Find a model or provider", text: $search)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Search Codex catalogue")
                if curatedRoutes.isEmpty {
                    Text("Add a model from a connected provider to get started.")
                        .font(.callout).foregroundStyle(.secondary)
                } else if filteredRoutes.isEmpty {
                    Text("No models match your search.").font(.callout).foregroundStyle(.secondary)
                }
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        ForEach(filteredRoutes, id: \.self) { route in catalogueRow(route) }
                    }.padding(.trailing, 4)
                }.frame(height: 360).accessibilityLabel("Codex model catalogue")
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 10) { catalogueActions }
                    VStack(alignment: .leading, spacing: 10) { catalogueActions }
                }
                if !missingSelections.isEmpty {
                    Text("\(missingSelections.count) selected \(missingSelections.count == 1 ? "model is" : "models are") not currently advertised and will be left out of the next launch. Refresh the provider catalogue, or expand the model to remove it.")
                        .font(.caption).foregroundStyle(.orange)
                }
                Text("Compaction follows each model’s effective input window automatically. Unknown limits stay unknown.")
                    .font(.caption).foregroundStyle(.secondary)
            Divider()
                VStack(alignment: .leading, spacing: 8) {
                    Text("Starting model").font(.headline)
                    defaultMenu
                    Text("The model new tasks start with. Every catalogue model remains available in the app’s picker.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Divider()
                VStack(alignment: .leading, spacing: 8) {
                    Text("Subagents").font(.headline)
                    Picker("Default model", selection: Binding(
                        get: { model.settings.codex_subagent_route ?? "" },
                        set: { materializeCatalogue(); model.settings.codex_subagent_route = $0.isEmpty ? nil : $0 }
                    )) {
                        Text("Use the parent task’s model").tag("")
                        ForEach(curatedRoutes, id: \.self) { route in Text(model.modelLabel(route)).tag(route) }
                        if let route = model.settings.codex_subagent_route, !curatedRoutes.contains(route) {
                            Text(model.modelLabel(route) + " · outside catalogue").tag(route)
                        }
                    }.pickerStyle(.menu).disabled(model.busy)
                    Text("Used when a parent does not choose a model for its subagent.")
                        .font(.caption).foregroundStyle(.secondary)
                    DisclosureGroup("Model priorities") {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Every model in this catalogue is available to subagents. Assign priorities 1–\(Self.subagentPoolSize) to put preferred models first. Unranked models follow. Ties prefer the starting model, then model name.")
                                .foregroundStyle(.secondary)
                            ForEach(1...Self.subagentPoolSize, id: \.self) { rank in
                                let holders = slotRoutes(rank)
                                HStack(spacing: 8) {
                                    Text("\(rank)").font(.system(size: 12, weight: .semibold, design: .rounded))
                                        .frame(width: 18, alignment: .trailing)
                                    Picker("Priority \(rank)", selection: Binding(
                                        get: { holders.first ?? "" },
                                        set: { setSlot(rank, $0.isEmpty ? nil : $0) }
                                    )) {
                                        Text("Automatic").tag("")
                                        ForEach(curatedRoutes, id: \.self) { route in Text(model.modelLabel(route)).tag(route) }
                                    }.labelsHidden().pickerStyle(.menu).disabled(model.busy)
                                    if holders.count > 1 {
                                        Text("+\(holders.count - 1) sharing").foregroundStyle(.orange)
                                            .help("Several models share this priority; choosing one here gives the slot to it alone.")
                                    }
                                }
                            }
                        }.font(.caption).padding(.top, 6)
                    }.font(.caption)
                }
        }
    }

    var defaultMenu: some View {
        Menu {
            ForEach(model.providerDefinitions) { provider in
                let options = defaultOptions.filter { $0.id.hasPrefix(provider.id + "/") }
                if !options.isEmpty {
                    Menu(provider.presentation.displayProvider) {
                        ForEach(options) { option in
                            Button {
                                materializeCatalogue()
                                model.settings.codex_model = option.id
                            } label: {
                                if model.settings.codex_model == option.id { Label(option.name, systemImage: "checkmark") }
                                else { Text(option.name) }
                            }
                        }
                    }
                }
            }
        } label: {
            Text(model.settings.codex_model.map { model.modelLabel($0) } ?? "Choose a model…")
                .lineLimit(2).frame(maxWidth: .infinity, alignment: .leading)
        }.disabled(model.busy || defaultOptions.isEmpty)
            .accessibilityLabel("Codex starting model")
    }

    func catalogueRow(_ route: String) -> some View {
        DisclosureGroup(isExpanded: Binding(
            get: { expandedRoutes.contains(route) },
            set: { if $0 { expandedRoutes.insert(route) } else { expandedRoutes.remove(route) } }
        )) {
            VStack(alignment: .leading, spacing: 12) {
                Divider()
                VStack(alignment: .leading, spacing: 4) {
                    Text("Auto-compaction").font(.caption.bold())
                    Text(compactionDescription(route)).font(.caption).foregroundStyle(.secondary)
                }
                Picker("Subagent priority", selection: Binding(
                    get: { subagentRanks[route] ?? 0 },
                    set: { setRank(route, $0 == 0 ? nil : $0) }
                )) {
                    Text("Automatic").tag(0)
                    ForEach(1...Self.subagentPoolSize, id: \.self) { rank in Text("\(rank)").tag(rank) }
                }.pickerStyle(.menu).disabled(model.busy)
                    .help("Priority 1 is offered first. Automatic fills remaining delegation places.")
                if !advertised.contains(route) {
                    Text("Not currently advertised by its provider.").font(.caption).foregroundStyle(.orange)
                }
                Button(role: .destructive) { removeRoute(route) } label: {
                    Label("Remove from catalogue", systemImage: "minus.circle")
                }.disabled(curatedRoutes.count <= 1 || model.busy)
                    .help(curatedRoutes.count <= 1 ? "Keep at least one model in the catalogue" : "Remove this model from Codex’s picker")
            }.padding(.top, 7)
        } label: {
            VStack(alignment: .leading, spacing: 5) {
                Text(model.modelLabel(route)).font(.system(size: 12, weight: .semibold)).lineLimit(2)
                Text(model.modelFacts(route)).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2)
                HStack(spacing: 8) {
                    if model.settings.codex_model == route {
                        Label("Starting model", systemImage: "star.fill").foregroundStyle(.orange)
                    }
                    if let rank = subagentRanks[route] { Text("Subagent priority \(rank)").foregroundStyle(.secondary) }
                }.font(.system(size: 11, weight: .medium))
            }.frame(maxWidth: .infinity, alignment: .leading)
        }.padding(10)
            .background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 8))
    }

    @ViewBuilder var catalogueActions: some View {
        Button("Add all") { addAll() }.disabled(model.busy || model.availableModels.isEmpty)
        Button("Keep starting model") { keepStartingModel() }.disabled(model.busy || curatedRoutes.count <= 1)
    }

    var addMenu: some View {
        Menu {
            ForEach(model.providerDefinitions) { provider in
                let options = model.availableModels.filter {
                    $0.provider_id == provider.id && ($0.tools ?? true) && !curatedRoutes.contains($0.id)
                }
                if !options.isEmpty {
                    Menu(provider.presentation.displayProvider) {
                        ForEach(options) { entry in
                            Button(model.modelLabel(entry.id)) { addRoute(entry.id) }.help(entry.id)
                        }
                    }
                }
            }
        } label: { Label("Add", systemImage: "plus") }
            .disabled(model.busy || model.availableModels.isEmpty)
    }
}

/// Reports the applied launch configuration, never a guessed account entitlement.
struct CodexNativeCapabilitiesStatus: View {
    @ObservedObject var model: BridgeModel
    @State private var accountMode = "unknown"

    private var refreshKey: String {
        "\(model.codexProfileActive):\(model.codexRunning):\(model.savedSettings.codex_chatgpt_account)"
    }

    private var accountSummary: String {
        switch accountMode {
        case "enabled":
            return model.settings.codex_chatgpt_account
                ? "Native account access is active for this launch."
                : "Native account access stays active until Desktop quits."
        case "disabled":
            return model.settings.codex_chatgpt_account
                ? "Save, then relaunch Desktop to apply native account access."
                : "This launch uses an accountless provider session."
        case "inactive":
            return model.settings.codex_chatgpt_account
                ? "Native account access will apply at the next launch."
                : "Native account access is off for Hub launches."
        default:
            return "Current launch status is unavailable."
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(accountSummary)
            if model.settings.codex_chatgpt_account || accountMode == "enabled" {
                Text("Voice and image availability are checked by Desktop using your sign-in, plan, workspace and rollout. Native requests retain ChatGPT account limits.")
                Text("Start Voice in Desktop; request an image in the chat. With the desktop helper enabled, allow microphone access for Provider Hub.")
            }
        }
        .font(.caption).foregroundStyle(.secondary)
        .fixedSize(horizontal: false, vertical: true)
        .task(id: refreshKey) {
            do {
                let state = try await model.command("codex-status")
                guard !Task.isCancelled else { return }
                let native = state["codex_native_capabilities"] as? [String: Any]
                accountMode = native?["account_mode"] as? String ?? "unknown"
            } catch {
                if !Task.isCancelled { accountMode = "unknown" }
            }
        }
    }
}

/// Names Desktop projects rooted at the disk root before a chat in one meets
/// “Select a project to continue”. Nothing is shown when there are none. The
/// check reruns when the pane appears and when Desktop starts or stops, so a
/// project fixed in Desktop clears it.
struct CodexDiskRootProjectsWarning: View {
    @ObservedObject var model: BridgeModel
    @State private var warning: String?

    var body: some View {
        // A stack, not a Group: a Group with no content has nothing to appear.
        VStack(alignment: .leading, spacing: 0) {
            if let warning {
                Label { Text(warning) } icon: { Image(systemName: "exclamationmark.triangle.fill") }
                    .font(.caption).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .task(id: "\(model.codexRunning):\(model.codexProfileActive)") {
            guard let state = try? await model.command("codex-projects"), !Task.isCancelled else { return }
            warning = model.diskRootProjectSummary(state)
        }
    }
}

struct CodexConfigPane: View {
    @ObservedObject var model: BridgeModel

    var applyPatchExclusionNote: String {
        let excluded = model.settings.codex_apply_patch_exclude?.count ?? 0
        return excluded == 0 ? "" : " except \(excluded) excluded in the settings file"
    }
    var applyPatchListNote: String {
        let listed = model.settings.codex_apply_patch?.count ?? 0
        return listed == 0 ? "" : " \(listed) model\(listed == 1 ? " is" : "s are") qualified individually in the settings file."
    }
    // Mirrors codex_profile: search is on when any launched route runs it,
    // and the gateway drops the tool for the rest.
    var searchNote: String {
        var routes = model.settings.codex_catalogue ?? model.codexModels.map(\.id)
        if let starting = model.settings.codex_model, !routes.contains(starting) { routes.append(starting) }
        let searching = routes.filter { model.modelEntry($0)?.web_search == true }
        guard !searching.isEmpty else {
            return "File editing and terminal tools are available. Web search is off: no route in this catalogue runs search of its own."
        }
        let names = searching.prefix(3).map { model.modelEntry($0)?.display_name ?? $0 }
        let more = searching.count > 3 ? " and \(searching.count - 3) more" : ""
        let count = searching.count == 1 ? "1 route" : "\(searching.count) routes"
        return "File editing and terminal tools are available. Web search is on for \(count) (\(names.joined(separator: ", "))\(more)); other routes run without it."
    }

    var body: some View {
        Panel {
            HubPaneHeading(title: "Codex / ChatGPT", subtitle: "App preferences", icon: "terminal")
                CodexDiskRootProjectsWarning(model: model)
                Text("Native ChatGPT capabilities").font(.headline)
                CodexPreference(isOn: $model.settings.codex_chatgpt_account, disabled: model.busy,
                    title: "Use native ChatGPT capabilities",
                    summary: "Keep ChatGPT sign-in for native Voice and OpenAI image generation alongside provider models.",
                    details: "Retains native account access and enables Desktop’s Voice and image-generation controls for this launch. Sign in to ChatGPT in Desktop; account, workspace and rollout availability still apply. Codex routes can use the native image tool; other models use Desktop’s image-generation host tool when offered. Native capabilities use their OpenAI account limits; model text uses the selected provider account. Extra Codex CLI accounts can differ from the Desktop account. The local gateway credential is stored separately for the session and removed on restore. Save, then launch.")
                CodexNativeCapabilitiesStatus(model: model)
                Divider()
                Text("Appearance").font(.headline)
                CodexPreference(isOn: $model.settings.codex_accent_slider, disabled: model.busy,
                    title: "Use provider accent colours",
                    summary: "Match the slider, activity and sidebar spinners to the provider.",
                    details: "Provider Hub launches Codex through a DevTools pipe held only by its helper; no network port listens. This is unsupported by OpenAI and an app update may disable the colouring. A Codex self-relaunch runs without it until launched here again. In this mode, macOS attributes Codex’s privacy prompts—including microphone, camera, folders and automation—to Provider Hub. The pipe is a full control channel into Codex. Its helper outlives Provider Hub and stays until Codex quits; killing the helper closes the pipe and asks Codex to quit. Save, then launch.")
                Divider()
                CodexPreference(isOn: $model.settings.codex_quick_composer, disabled: model.busy,
                    title: "Show the recent-thread quick composer",
                    summary: "Message or steer recent local Codex chats from a floating popover.",
                    details: "Adds a button beside search and notifications in Codex’s sidebar showing the first ten Recents rows. Choose a local Codex chat, read its latest response preview and send from a compact prompt capsule. Each chat keeps its own draft. Previews are read while the popover is open; sends use the chat’s existing settings and Desktop’s normal queue and steering behavior. Remote-host and ChatGPT chats are unavailable in this version. This preference uses the same private desktop helper as provider colours and can be enabled independently. The popover is a renderer-side overlay inside Codex’s window — it can sit anywhere within the Codex window but cannot float over other apps or onto a different display. Sending is enabled only for the Desktop builds the helper has been verified against (currently 26.930.51102 and 26.930.61225); an unverified build fails sends with a diagnostic that names the bundle. Unsupported by OpenAI; an app update may disable the button or sending. Save, then launch.")
                Divider()
                CodexPreference(isOn: $model.settings.codex_quick_composer_window, disabled: model.busy || !model.settings.codex_quick_composer,
                    title: "Open the quick composer in its own window",
                    summary: "A separate window you can move to any display, instead of an overlay inside Codex.",
                    details: "The sidebar button opens Provider Hub’s own floating glass window instead of the overlay: it can sit on any display, stays over other apps while pinned, and reopens where you left it. The helper relays the same recent-chat rows, previews and sends through the DevTools pipe; sending is unchanged, and prompts never enter the helper’s log. Recent chats… in the menu bar opens the same window. If Provider Hub has quit while Codex keeps running, the helper opens Codex’s in-app browser panel with the same composer instead. Save, then launch.")
                Divider()
                CodexPreference(isOn: $model.settings.codex_hide_usage_banner, disabled: model.busy || !model.settings.codex_accent_slider,
                    title: "Hide the ChatGPT usage banner",
                    summary: model.settings.codex_accent_slider
                        ? "Hide plan-usage banners above the composer while using provider models."
                        : "Requires provider accent colours to be enabled above.",
                    details: "The accent helper hides model-text usage banners, their per-model variants and the “Get 250 credits” referral card. Provider-routed text uses its provider account; native Voice and images still have ChatGPT account limits. Native mode keeps combined account/image-limit and unrecognized notices visible, so some text usage banners may remain. Account and usage pages, and any rate-limit prompt on submit, stay visible. Save, then launch.")
                Divider()
                CodexPreference(isOn: $model.settings.codex_unlock_composer, disabled: model.busy || !model.settings.codex_accent_slider,
                    title: "Keep sending when ChatGPT usage runs out",
                    summary: model.settings.codex_accent_slider
                        ? "Let provider models send after the ChatGPT plan’s usage is exhausted."
                        : "Requires provider accent colours to be enabled above.",
                    details: "Once the ChatGPT plan’s usage is exhausted, Codex disables the composer’s send button for every model, including provider routes. The accent helper reports the plan’s core limit as allowing sends; usage windows, per-model limits and credits are left as they are. Native OpenAI models, Voice and images keep their real server limits. When native capabilities are enabled, recognized image-limit notices stay visible. The core usage meter can show at least 1% remaining while native requests are unavailable; it is not an availability check. Unsupported by OpenAI; a changed usage payload switches this behavior off. Save, then launch.")
            Divider()
                Text("Tools & goals").font(.headline)
                CodexPreference(isOn: $model.settings.codex_apply_patch_all, disabled: model.busy,
                    title: "Enable patch-based file editing",
                    summary: "Include supported file edits in Review and Undo.",
                    details: model.settings.codex_apply_patch_all
                        ? "The tool is offered to every catalogue model\(applyPatchExclusionNote). A model that fails the patch format falls back to shell edits, which do not appear in the close-out diff card. Save, then relaunch Codex."
                        : "Models otherwise edit through the shell, without close-out diff cards, Undo or Review for those edits.\(applyPatchListNote) Save, then relaunch Codex.")
                Divider()
                CodexPreference(isOn: $model.settings.codex_goal_budget, disabled: model.busy,
                    title: "Allow goal token budgets",
                    summary: "Let models set a token cap when creating or updating a goal.",
                    details: "A capped goal can stop with its objective unfinished when its budget is reached. Codex asks models to set a budget only when you request one, but some models may ignore that instruction. When off, Provider Hub removes token_budget from create_goal and update_goal; goals run until complete, blocked or stopped. Your plan’s usage limits still apply. Save, then launch.")
                Text(searchNote)
                    .font(.caption).foregroundStyle(.secondary)
                DisclosureGroup("Provider usage") {
                    Text("Grok uses xAI API billing; Fast requests premium Priority processing. Ollama uses your existing daemon and its configured local or cloud access.")
                        .font(.caption).foregroundStyle(.secondary).padding(.top, 5)
                }.font(.caption)
            Divider()
                Text("Desktop session").font(.headline)
                Label(model.codexProfileActive ? "Provider profile active" : "Previous setup retained",
                      systemImage: model.codexProfileActive ? "checkmark.circle.fill" : "arrow.uturn.backward.circle")
                    .font(.callout).foregroundStyle(model.codexProfileActive ? Color.green : Color.secondary)
                Button {
                    Task { await model.launchCodex() }
                } label: {
                    Text(model.codexProfileActive && model.codexRunning ? "Show Codex / ChatGPT" : "Launch Codex / ChatGPT")
                        .foregroundStyle(HubTheme.Control.ink)
                }.buttonStyle(.borderedProminent).tint(HubTheme.Accent.brand).controlSize(.large)
                    .disabled(model.busy || model.codexAppPath == nil || model.settings.codex_model == nil)
                Button("Restore previous setup") { Task { await model.restoreCodex() } }
                    .disabled(model.busy || !model.codexRecoveryNeeded || model.codexRunning)
                Text("Your previous model configuration is restored after the app quits. Conversations, credentials, projects and unrelated settings are retained.")
                    .font(.caption).foregroundStyle(.secondary)
                if model.claudeRunning && model.profileActive {
                    Text("Claude shares this gateway. Changing the Codex catalogue before launch briefly restarts the gateway; Claude reconnects automatically.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if model.codexRunning && !model.codexProfileActive {
                    Text("The app is already open. Launching here asks before restarting it.")
                        .font(.caption).foregroundStyle(.orange)
                }
                if model.codexAppPath == nil {
                    Text("Install Codex / ChatGPT Desktop to launch it here.").font(.caption).foregroundStyle(.secondary)
                } else if model.settings.codex_model == nil {
                    Text("Choose a starting model on the Models tab first.").font(.caption).foregroundStyle(.secondary)
                }
        }
    }
}

private struct CodexPreference: View {
    @Binding var isOn: Bool
    var disabled: Bool
    var title: String
    var summary: String
    var details: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Toggle(isOn: $isOn) { Text(title).font(.system(size: 13, weight: .medium)).fixedSize(horizontal: false, vertical: true).frame(maxWidth: .infinity, alignment: .leading) }
                .toggleStyle(.switch).disabled(disabled)
            Text(summary).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            DisclosureGroup("Details") {
                Text(details).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true).padding(.top, 5)
            }.font(.caption).accessibilityLabel(title + " details")
        }
    }
}
