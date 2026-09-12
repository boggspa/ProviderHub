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
            if anyOwnedHarnessRunning {
                var withoutSelection = settings
                withoutSelection.codex_model = savedSettings.codex_model
                guard withoutSelection == savedSettings else {
                    throw WorkerError(message: "Close the desktop sessions using this gateway before changing provider settings.")
                }
                if changed {
                    let result = try await command("save", input: JSONEncoder().encode(settings))
                    readCatalogue(result, modelsKey: "models")
                    savedSettings = settings
                }
            } else { try await save() }
            let prepared = try await command("codex-prepare")
            readCatalogue(prepared, modelsKey: "models")
            try await startGateway()
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
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = true
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: appPath), configuration: configuration) { _, error in
                    if let error { continuation.resume(throwing: error) } else { continuation.resume() }
                }
            }
            tell("Codex / ChatGPT is opening with your provider catalogue. Its previous configuration will be restored after it quits.")
        } catch {
            updateCodexRunning()
            if codexRecoveryNeeded && !codexRunning {
                if (try? await command("codex-restore")) != nil { codexRecoveryNeeded = false; codexProfileActive = false }
            }
            tell(error.localizedDescription, error: true)
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
    var selected: CodexModelOption? { model.codexModels.first { $0.id == model.settings.codex_model } }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Panel {
                Text("Codex / ChatGPT Desktop").font(.title2.bold())
                Text("Choose a provider model directly. Your Claude mappings stay independent.").foregroundStyle(.secondary)
                HStack {
                    Text("Default model").font(.system(size: 13, weight: .medium))
                    Menu {
                        ForEach(model.providerDefinitions) { provider in
                            let options = model.codexModels.filter { $0.id.hasPrefix(provider.id + "/") }
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
                Text("Codex’s picker will show every compatible model from your configured accounts. This selection sets the starting model.")
                    .font(.caption).foregroundStyle(.secondary)
                Text("Context limits follow each model’s metadata; unreported limits remain unknown.")
                    .font(.caption).foregroundStyle(.secondary)
                Text("File editing and terminal tools are available. Web search is off in this setup.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button(model.codexProfileActive && model.codexRunning ? "Show Codex / ChatGPT" : "Launch Codex / ChatGPT") { Task { await model.launchCodex() } }
                        .buttonStyle(.borderedProminent).controlSize(.large)
                        .disabled(model.busy || model.codexAppPath == nil || model.settings.codex_model == nil)
                    Button("Restore previous setup") { Task { await model.restoreCodex() } }
                        .disabled(model.busy || !model.codexRecoveryNeeded || model.codexRunning)
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
}
