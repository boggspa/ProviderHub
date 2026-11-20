import AppKit
import Foundation

// The Vibe harness: launch Mistral's Vibe desktop app with Provider Hub's
// catalogue in Vibe's own configuration file, and put the user's own
// configuration back when Vibe quits.
//
// Vibe keeps its model list in `$VIBE_HOME/config.toml`, so the Hub writes a
// provider entry pointing at the local gateway and one model row per route.
// The credential never reaches that file: Vibe reads the gateway token from
// an environment variable, and the launcher puts it in the app's
// environment. The wire is Anthropic Messages, the same one the Claude
// surface speaks, so the gateway serves it without a second protocol.

extension BridgeModel {
    /// The routes Vibe's picker offers. A nil selection serves every
    /// compatible route, the way the Codex catalogue does.
    var vibeRoutes: [String] {
        if let curated = settings.vibe_catalogue { return curated }
        var routes = availableModels.filter { $0.tools ?? true }.map(\.id)
        if let starting = settings.vibe_model, !routes.contains(starting) { routes.append(starting) }
        return routes
    }

    func vibeMaterialise() {
        if settings.vibe_catalogue == nil && !vibeRoutes.isEmpty { settings.vibe_catalogue = vibeRoutes }
    }

    func vibeSetListed(_ route: String, _ listed: Bool) {
        if listed {
            guard !vibeRoutes.contains(route) else { return }
            settings.vibe_catalogue = vibeRoutes + [route]
            if settings.vibe_model == nil { settings.vibe_model = route }
        } else {
            guard vibeRoutes.count > 1 else { return }
            let list = vibeRoutes.filter { $0 != route }
            settings.vibe_catalogue = list
            if settings.vibe_model == route { settings.vibe_model = list.first }
        }
    }

    /// "all" lists every compatible model, "custom" the curated list.
    var vibeMode: String { settings.vibe_catalogue == nil ? "all" : "custom" }
    func setVibeMode(_ mode: String) {
        if mode == "all" { settings.vibe_catalogue = nil } else { vibeMaterialise() }
    }

    var vibeLive: Bool { vibeRunning && vibeProfileActive }

    func updateVibeRunning() {
        vibeRunning = NSWorkspace.shared.runningApplications.contains {
            $0.bundleIdentifier == "ai.mistral.lechat.desktop" && !$0.isTerminated
        }
    }

    func readVibeState(_ object: [String: Any]) {
        if let path = object["vibe_app_path"] as? String { vibeAppPath = path }
        if let value = object["vibe_running"] as? Bool { vibeRunning = value }
        if let value = object["vibe_profile_active"] as? Bool { vibeProfileActive = value }
        if let value = object["vibe_recovery_needed"] as? Bool { vibeRecoveryNeeded = value }
    }

    /// The gateway token Vibe reads from the environment. It is the same
    /// local credential the Hub uses for its own requests, and it never
    /// reaches a file.
    private func gatewayToken() throws -> String {
        let token = try String(contentsOf: root.appendingPathComponent("gateway-token"), encoding: .utf8)
        return token.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func launchVibe() async {
        guard !busy else { return }
        busy = true; defer { busy = false }
        updateVibeRunning()
        if vibeLive {
            NSWorkspace.shared.runningApplications.first { $0.bundleIdentifier == "ai.mistral.lechat.desktop" }?
                .activate(options: [.activateAllWindows])
            return
        }
        if vibeRunning {
            tell("Vibe is already open. Quit it, then launch it here.", error: true)
            return
        }
        guard let appPath = vibeAppPath else {
            tell("Install the Vibe desktop app first, then try again.", error: true)
            return
        }
        guard settings.vibe_model != nil else {
            tell("Choose a model for Vibe first.", error: true)
            return
        }
        do {
            if vibeRecoveryNeeded {
                _ = try await command("vibe-restore")
                vibeRecoveryNeeded = false; vibeProfileActive = false
            }
            tell("Preparing the selected models for Vibe…")
            let prepared = try await prepareGatewayForLaunch(.vibe) {
                let prepared = try await command("vibe-prepare")
                readCatalogue(prepared, modelsKey: "models")
                readVibeState(prepared)
                return prepared
            }
            _ = try await command("vibe-activate")
            vibeProfileActive = true; vibeRecoveryNeeded = true
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = true
            configuration.environment = ["PROVIDER_HUB_VIBE_TOKEN": try gatewayToken()]
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: appPath), configuration: configuration) { _, error in
                    if let error { continuation.resume(throwing: error) }
                    else { continuation.resume() }
                }
            }
            vibeLaunchTime = Date(); observedOwnedVibe = false
            let base = "Vibe is opening with your provider catalogue. Its previous configuration will be restored after it quits."
            if let omitted = omissionSummary(prepared) { tell(base + " " + omitted, warning: true) }
            else { tell(base) }
        } catch {
            updateVibeRunning()
            if vibeRecoveryNeeded && !vibeRunning {
                if (try? await command("vibe-restore")) != nil {
                    vibeRecoveryNeeded = false; vibeProfileActive = false
                }
            }
            tell(error.localizedDescription, error: true)
        }
    }

    func restoreVibe() async {
        guard !busy else { return }
        busy = true; defer { busy = false }
        do {
            let result = try await command("vibe-restore")
            vibeRecoveryNeeded = false; vibeProfileActive = false; observedOwnedVibe = false
            await stopGatewayIfUnused()
            let kept = result["preserved_external_changes"] as? Int ?? 0
            tell(kept > 0 ? "Disconnected. Changes made in Vibe were preserved." : "Restored Vibe’s previous configuration. Your conversations remain saved.")
        } catch { tell(error.localizedDescription, error: true) }
    }

    /// Put Vibe's own configuration back once the app has quit.
    func pollVibeRecovery() async {
        if vibeProfileActive && vibeRunning { observedOwnedVibe = true }
        let failedLaunch = vibeProfileActive && !vibeRunning && !observedOwnedVibe && Date().timeIntervalSince(vibeLaunchTime) > 20
        guard vibeRecoveryNeeded && !vibeRunning && (observedOwnedVibe || failedLaunch) && !busy && !finishingSession else { return }
        finishingSession = true; defer { finishingSession = false }
        do {
            let finished = try await finishClosedSession(autoStop: savedSettings.auto_stop) {
                _ = try await command("vibe-restore")
                vibeRecoveryNeeded = false; vibeProfileActive = false; observedOwnedVibe = false
            }
            if finished { tell("Vibe closed. Its previous configuration has been restored.") }
        } catch { tell(error.localizedDescription, error: true) }
    }
}
