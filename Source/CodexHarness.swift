import AppKit
import SwiftUI

struct CodexModelOption: Decodable, Identifiable {
    var id: String
    var name: String
    var context: Int?
    var description: String
}

extension BridgeModel {
    var anyOwnedHarnessRunning: Bool {
        (claudeRunning && profileActive) || (codexRunning && codexRecoveryNeeded)
    }

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

    func launchCodex() async {
        guard !busy else { return }
        page = .codex
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
            // While Claude is live on this gateway, save() admits only changes
            // scoped to Codex and refuses shared or Claude-side edits.
            if changed || !anyOwnedHarnessRunning { try await save() }
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
                    if !anyOwnedHarnessRunning && activeRequests == 0 { await stopGateway() }
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
            if savedSettings.codex_accent_slider {
                try await launchCodexWithAccentBridge(appPath: appPath)
                tell("Codex / ChatGPT is opening with your provider catalogue and a provider-coloured power slider. Its previous configuration will be restored after it quits.")
            } else {
                let configuration = NSWorkspace.OpenConfiguration()
                configuration.activates = true
                try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                    NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: appPath), configuration: configuration) { _, error in
                        if let error { continuation.resume(throwing: error) } else { continuation.resume() }
                    }
                }
                tell("Codex / ChatGPT is opening with your provider catalogue. Its previous configuration will be restored after it quits.")
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
        let output = Pipe(), errors = Pipe()
        process.standardOutput = output; process.standardError = errors
        process.standardInput = FileHandle.nullDevice
        try process.run()
        codexAccentProcess = process
        // The helper reports the app's spawn as a JSON line; an early exit
        // means the app never started (or handed off to a running copy).
        let launched = await withCheckedContinuation { (continuation: CheckedContinuation<Bool, Never>) in
            let lock = NSLock()
            var finished = false
            func finish(_ value: Bool) {
                lock.lock(); defer { lock.unlock() }
                if !finished { finished = true; continuation.resume(returning: value) }
            }
            DispatchQueue.global(qos: .userInitiated).async {
                var buffer = Data()
                while true {
                    let chunk = output.fileHandleForReading.availableData
                    if chunk.isEmpty { finish(false); return }
                    buffer.append(chunk)
                    if String(decoding: buffer, as: UTF8.self).contains("\"launched\"") { finish(true); return }
                }
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + 20) { finish(false) }
        }
        DispatchQueue.global(qos: .utility).async {
            // Drain the helper's remaining output so it never blocks on a full pipe.
            _ = output.fileHandleForReading.readDataToEndOfFile()
            _ = errors.fileHandleForReading.readDataToEndOfFile()
        }
        guard launched else {
            if process.isRunning { process.terminate() }
            codexAccentProcess = nil
            throw WorkerError(message: "Codex / ChatGPT did not start through the accent helper. Turn the power-slider colour switch off to launch it the usual way.")
        }
    }

    func restoreCodex() async {
        guard !busy else { return }
        busy = true; defer { busy = false }
        do {
            let result = try await command("codex-restore")
            codexRecoveryNeeded = false; codexProfileActive = false; observedOwnedCodex = false
            if !anyOwnedHarnessRunning && activeRequests == 0 { await stopGateway() }
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
            if savedSettings.auto_stop && !anyOwnedHarnessRunning && activeRequests == 0 { await stopGateway() }
            tell("Codex / ChatGPT closed. Its previous configuration has been restored.")
        } catch { tell(error.localizedDescription, error: true) }
    }
}

struct CodexPage: View {
    @ObservedObject var model: BridgeModel
    var curatedRoutes: [String] { model.settings.codex_catalogue ?? [] }
    var isCustom: Bool { model.settings.codex_catalogue != nil }
    var advertised: Set<String> { Set(model.availableModels.map(\.id)) }
    var missingSelections: [String] { curatedRoutes.filter { !advertised.contains($0) } }

    // The default picker follows the unsaved selection so newly added models
    // are pickable before the catalogue is saved.
    var defaultOptions: [CodexModelOption] {
        guard let curated = model.settings.codex_catalogue else { return model.codexModels }
        return curated.map { route in
            model.codexModels.first { $0.id == route }
                ?? CodexModelOption(id: route, name: model.modelLabel(route),
                                    context: model.modelEntry(route)?.context,
                                    description: model.modelFacts(route))
        }
    }

    var selected: CodexModelOption? { defaultOptions.first { $0.id == model.settings.codex_model } }

    var catalogueMode: Binding<String> {
        Binding(
            get: { isCustom ? "custom" : "all" },
            set: { value in
                if value == "custom" {
                    var seed: [String] = []
                    if let route = model.settings.codex_model { seed.append(route) }
                    if seed.isEmpty, let first = model.codexModels.first { seed = [first.id] }
                    model.settings.codex_catalogue = seed.isEmpty ? nil : seed
                } else {
                    model.settings.codex_catalogue = nil
                }
            }
        )
    }

    func removeRoute(_ route: String) {
        guard var list = model.settings.codex_catalogue, list.count > 1 else { return }
        list.removeAll { $0 == route }
        model.settings.codex_catalogue = list
        if model.settings.codex_model == route { model.settings.codex_model = list.first }
    }

    var applyPatchExclusionNote: String {
        let excluded = model.settings.codex_apply_patch_exclude?.count ?? 0
        return excluded == 0 ? "" : " except \(excluded) excluded in the settings file"
    }

    var applyPatchListNote: String {
        let listed = model.settings.codex_apply_patch?.count ?? 0
        return listed == 0 ? "" : " \(listed) model\(listed == 1 ? " is" : "s are") qualified individually in the settings file."
    }

    func clearCatalogue() {
        guard let keep = curatedRoutes.contains(model.settings.codex_model ?? "") ? model.settings.codex_model : curatedRoutes.first else { return }
        model.settings.codex_catalogue = [keep]
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Panel {
                Text("Codex / ChatGPT Desktop").font(.title2.bold())
                Text("Choose a provider model directly. Your Claude mappings stay independent.").foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        Text("Catalogue").font(.system(size: 13, weight: .medium))
                        Spacer()
                        Picker("Catalogue", selection: catalogueMode) {
                            Text("All compatible models").tag("all")
                            Text("Custom selection").tag("custom")
                        }.pickerStyle(.segmented).fixedSize()
                    }
                    if isCustom {
                        customCatalogue
                    } else {
                        Text("Codex’s picker lists every compatible model from your configured accounts; catalogues refresh in the background.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    if !missingSelections.isEmpty {
                        Text("\(missingSelections.count) selected \(missingSelections.count == 1 ? "model is" : "models are") not currently advertised: \(missingSelections.map { model.modelLabel($0) }.joined(separator: ", ")). Refresh their providers or remove \(missingSelections.count == 1 ? "it" : "them") from the catalogue.")
                            .font(.caption).foregroundStyle(.orange)
                    }
                }
                HStack {
                    Text("Default model").font(.system(size: 13, weight: .medium))
                    Menu {
                        ForEach(model.providerDefinitions) { provider in
                            let options = defaultOptions.filter { $0.id.hasPrefix(provider.id + "/") }
                            if !options.isEmpty {
                                Menu(provider.presentation.displayProvider) {
                                    ForEach(options) { option in
                                        Button { model.settings.codex_model = option.id } label: {
                                            if model.settings.codex_model == option.id { Label(option.name, systemImage: "checkmark") }
                                            else { Text(option.name) }
                                        }
                                    }
                                }
                            }
                        }
                    } label: { Text(selected?.name ?? "Choose a model…").frame(minWidth: 260, alignment: .leading) }
                }
                if let selected {
                    Text("\(selected.context.map { $0.formatted() + " tokens" } ?? "Provider-managed context") · \(selected.description)").font(.caption).foregroundStyle(.secondary)
                }
                Text("The default sets the starting model; the rest of the catalogue stays available in Codex’s picker.")
                    .font(.caption).foregroundStyle(.secondary)
                Text("Context limits follow each model’s metadata; unreported limits remain unknown.")
                    .font(.caption).foregroundStyle(.secondary)
                Text("File editing and terminal tools are available. Web search is off in this setup.")
                    .font(.caption).foregroundStyle(.secondary)
                Toggle(isOn: $model.settings.codex_chatgpt_account) {
                    Text("Show your ChatGPT account in Codex").font(.system(size: 13, weight: .medium))
                }
                .toggleStyle(.switch).disabled(model.busy)
                Text(model.settings.codex_chatgpt_account
                     ? "Codex keeps your ChatGPT sign-in visible while Provider Hub is active: the composer uses the native model pill (white label, chevron, Ultra colour) and the account chrome and usage banners reflect your ChatGPT plan. A ChatGPT sign-in is required to launch. The gateway credential is written into the Codex config for the session and removed when the previous setup is restored. Save, then launch."
                     : "Off: Codex runs as an accountless custom-provider session with a plain gray model pill. Turn on to present your ChatGPT sign-in and the native composer styling.")
                    .font(.caption).foregroundStyle(.secondary)
                Toggle(isOn: $model.settings.codex_apply_patch_all) {
                    Text("Offer apply_patch to catalogue models").font(.system(size: 13, weight: .medium))
                }
                .toggleStyle(.switch).disabled(model.busy)
                Text(model.settings.codex_apply_patch_all
                     ? "Codex offers its apply_patch editing tool to every model in the catalogue\(applyPatchExclusionNote). Edits made with it feed the close-out diff card, its per-file rows, Undo and Review. A model that keeps failing the patch format falls back to shell edits, which the card does not show. Save, then relaunch Codex."
                     : "Off: catalogue models edit through the shell, so Codex shows no close-out diff card, Undo or Review for their turns.\(applyPatchListNote) Turn on to offer apply_patch to every catalogue model.")
                    .font(.caption).foregroundStyle(.secondary)
                Toggle(isOn: $model.settings.codex_accent_slider) {
                    Text("Colour the power slider by provider").font(.system(size: 13, weight: .medium))
                }
                .toggleStyle(.switch).disabled(model.busy)
                Text(model.settings.codex_accent_slider
                     ? "Provider Hub starts Codex / ChatGPT itself with a DevTools pipe and installs a small watcher that colours the power slider and the pill’s effort word with the selected model’s provider accent and turns the activity text’s gray slightly cooler with a hint of the same hue; at Ultra the slider, the picker’s title and the pill’s word take a deeper, more saturated cut of that hue and the word shimmers. Only Provider Hub holds the pipe; nothing listens on a port. Unsupported by OpenAI: an app update that changes the picker switches the colour off with no other effect, and a Codex self-relaunch after an update runs without it until the next launch from here. In this mode macOS attributes Codex’s privacy prompts (microphone, camera, calendars, reminders, location, folders, automation) to Provider Hub, and the pipe is a full control channel into Codex that only this helper holds. The helper stays until Codex quits and outlives Provider Hub; if the helper itself is killed, Codex treats the closed pipe as a request to quit. Save, then launch."
                     : "Off: Codex / ChatGPT opens the usual way and the power slider keeps its standard blue. Turn on to tint it with each model’s provider accent, the same hues as the Providers page.")
                    .font(.caption).foregroundStyle(.secondary)
                Toggle(isOn: $model.settings.codex_hide_usage_banner) {
                    Text("Hide the ChatGPT usage banner").font(.system(size: 13, weight: .medium))
                }
                .toggleStyle(.switch).disabled(model.busy || !model.settings.codex_accent_slider)
                Text(model.settings.codex_hide_usage_banner
                     ? "The same watcher hides Codex’s “You’re out of Codex and Work usage” banner, and its per-model “out of usage” variant, above the composer. The banner appears only with your ChatGPT sign-in shown, reports that account’s plan usage, and hub traffic does not spend it. It is recognised by its gauge icon, so an app update that redraws the icon brings the banner back and changes nothing else; the account and usage pages, and the rate-limit prompt Codex may open on submit, are untouched. Save, then launch."
                     : "Off: with your ChatGPT account shown, Codex keeps its usage banner above the composer even though hub traffic does not spend that plan. Needs the power-slider watcher above; save, then launch.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button(model.codexProfileActive && model.codexRunning ? "Show Codex / ChatGPT" : "Launch Codex / ChatGPT") { Task { await model.launchCodex() } }
                        .buttonStyle(.borderedProminent).controlSize(.large)
                        .disabled(model.busy || model.codexAppPath == nil || model.settings.codex_model == nil)
                    Button("Restore previous setup") { Task { await model.restoreCodex() } }
                        .disabled(model.busy || !model.codexRecoveryNeeded || model.codexRunning)
                    Spacer()
                    Button("Save catalogue") { Task { await model.saveFromUI() } }
                        .disabled(model.busy || !model.changed)
                }
                if model.claudeRunning && model.profileActive {
                    Text("Claude is live on this gateway. Launching Codex shares it; changing the catalogue or default first briefly restarts the gateway, and Claude reconnects automatically.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if model.codexRunning && !model.codexProfileActive {
                    Text("The desktop app is open. Launching here will ask before restarting it.")
                        .font(.caption).foregroundStyle(.orange)
                }
                if model.codexModels.isEmpty {
                    Text("Configure a provider account or start your Ollama daemon, then refresh the catalogues.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if model.codexAppPath == nil {
                    Text("Install Codex / ChatGPT Desktop to enable launching here.").font(.caption).foregroundStyle(.secondary)
                }
            }
            Panel {
                Text("Your existing setup is retained").font(.headline)
                Text("Provider Hub switches the installed app’s model configuration and restores it after the app quits. Existing conversations, credentials, projects and unrelated settings are retained. It does not open a separate simultaneous desktop instance.")
                    .font(.callout).foregroundStyle(.secondary)
                Text("Grok uses xAI API billing; Fast requests premium Priority processing. Ollama uses your existing daemon and its configured local or cloud access.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    var customCatalogue: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("\(curatedRoutes.count) model\(curatedRoutes.count == 1 ? "" : "s") will appear in Codex’s picker.")
                .font(.caption).foregroundStyle(.secondary)
            ForEach(curatedRoutes, id: \.self) { route in
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(model.modelLabel(route)).font(.system(size: 12, weight: .medium)).lineLimit(1)
                        Text(model.modelFacts(route)).font(.system(size: 9)).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button { removeRoute(route) } label: { Image(systemName: "minus.circle").foregroundStyle(.secondary) }
                        .buttonStyle(.plain)
                        .disabled(curatedRoutes.count <= 1 || model.busy)
                        .help(curatedRoutes.count <= 1 ? "Keep at least one model in the catalogue" : "Remove from the Codex catalogue")
                }
                .padding(.horizontal, 10).padding(.vertical, 7)
                .background(Color.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 7))
            }
            HStack(spacing: 12) {
                addMenu
                Button("Add all") { model.settings.codex_catalogue = model.availableModels.filter { $0.tools ?? true }.map(\.id) }
                    .disabled(model.busy || model.availableModels.isEmpty)
                Button("Clear") { clearCatalogue() }
                    .disabled(model.busy || curatedRoutes.count <= 1)
            }
        }
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
                            Button(model.modelLabel(entry.id)) { model.settings.codex_catalogue?.append(entry.id) }
                                .help(entry.id)
                        }
                    }
                }
            }
        } label: { Label("Add model…", systemImage: "plus") }
            .disabled(model.busy)
    }
}
