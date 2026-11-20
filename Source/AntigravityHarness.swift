import AppKit
import Foundation

// The Antigravity harness: launch Google's Antigravity desktop app with
// Provider Hub's catalogue using Antigravity's gateway environment.
//
// The gateway credential and catalogue are passed only in the process
// environment. Antigravity's normal Google account settings are not modified.

extension BridgeModel {
    /// The routes Antigravity's picker offers. A nil selection serves every
    /// compatible route, the way the Codex catalogue does.
    var antigravityRoutes: [String] {
        if let curated = settings.antigravity_catalogue { return curated }
        var routes = availableModels.filter { $0.tools ?? true }.map(\.id)
        if let starting = settings.antigravity_model, !routes.contains(starting) { routes.append(starting) }
        return routes
    }

    func antigravityMaterialise() {
        if settings.antigravity_catalogue == nil && !antigravityRoutes.isEmpty { settings.antigravity_catalogue = antigravityRoutes }
    }

    func antigravitySetListed(_ route: String, _ listed: Bool) {
        if listed {
            guard !antigravityRoutes.contains(route) else { return }
            settings.antigravity_catalogue = antigravityRoutes + [route]
            if settings.antigravity_model == nil { settings.antigravity_model = route }
        } else {
            guard antigravityRoutes.count > 1 else { return }
            let list = antigravityRoutes.filter { $0 != route }
            settings.antigravity_catalogue = list
            if settings.antigravity_model == route { settings.antigravity_model = list.first }
        }
    }

    /// "all" lists every compatible model, "custom" the curated list.
    var antigravityMode: String { settings.antigravity_catalogue == nil ? "all" : "custom" }
    func setAntigravityMode(_ mode: String) {
        if mode == "all" { settings.antigravity_catalogue = nil } else { antigravityMaterialise() }
    }

    var antigravityLive: Bool { antigravityRunning && antigravityProfileActive }

    func updateAntigravityRunning() {
        antigravityRunning = NSWorkspace.shared.runningApplications.contains {
            $0.bundleIdentifier == "com.google.antigravity" && !$0.isTerminated
        }
    }

    func readAntigravityState(_ object: [String: Any]) {
        if let path = object["antigravity_app_path"] as? String { antigravityAppPath = path }
        if let value = object["antigravity_running"] as? Bool { antigravityRunning = value }
        if let value = object["antigravity_profile_active"] as? Bool { antigravityProfileActive = value }
        if let value = object["antigravity_recovery_needed"] as? Bool { antigravityRecoveryNeeded = value }
    }

    /// The gateway token Antigravity's client reads from the environment. It
    /// is the same local credential the Hub uses for its own requests, and it
    /// never reaches a file.
    private func gatewayToken() throws -> String {
        let token = try String(contentsOf: root.appendingPathComponent("gateway-token"), encoding: .utf8)
        return token.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func launchAntigravity() async {
        guard !busy else { return }
        busy = true; defer { busy = false }
        updateAntigravityRunning()
        if antigravityLive {
            NSWorkspace.shared.runningApplications.first { $0.bundleIdentifier == "com.google.antigravity" }?
                .activate(options: [.activateAllWindows])
            return
        }
        if antigravityRunning {
            tell("Antigravity is already open. Quit it, then launch it here.", error: true)
            return
        }
        guard let appPath = antigravityAppPath else {
            tell("Install the Antigravity desktop app first, then try again.", error: true)
            return
        }
        guard settings.antigravity_model != nil else {
            tell("Choose a model for Antigravity first.", error: true)
            return
        }
        do {
            if antigravityRecoveryNeeded {
                _ = try await command("antigravity-restore")
                antigravityRecoveryNeeded = false; antigravityProfileActive = false
            }
            tell("Preparing the selected models for Antigravity…")
            let prepared = try await prepareGatewayForLaunch(.antigravity) {
                let prepared = try await command("antigravity-prepare")
                readCatalogue(prepared, modelsKey: "models")
                readAntigravityState(prepared)
                return prepared
            }
            let activation = try await command("antigravity-activate")
            antigravityProfileActive = true; antigravityRecoveryNeeded = true
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = true
            // The endpoint and the key travel in the app's environment:
            // Antigravity's Gemini client reads both from there, and neither
            // ever reaches a file.
            let baseURL = activation["antigravity_base_url"] as? String
                ?? "http://127.0.0.1:\(settings.port)"
            guard let models = activation["antigravity_models"] as? [String], !models.isEmpty else {
                throw WorkerError(message: "Antigravity has no available provider models.")
            }
            // The language server discovers labels and token limits from the
            // gateway's model-list endpoint. An environment allowlist would
            // take precedence and discard that metadata.
            configuration.environment = ["AGY_LLM_GATEWAY_URL": baseURL,
                                        "AGY_LLM_GATEWAY_API_KEY": try gatewayToken(),
                                        "AGY_LLM_GATEWAY_WIRE_PROTOCOL": "genai"]
            var launchedPID: pid_t = 0
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: appPath), configuration: configuration) { application, error in
                    if let error { continuation.resume(throwing: error) }
                    else {
                        launchedPID = application?.processIdentifier ?? 0
                        continuation.resume()
                    }
                }
            }
            // The gateway environment lives in the launched process, so the
            // Hub records which one it launched to tell its own session apart
            // from an Antigravity the user started themselves.
            if launchedPID > 0,
               let data = try? JSONSerialization.data(withJSONObject: ["pid": Int(launchedPID)]) {
                _ = try? await command("antigravity-register", input: data)
            }
            antigravityLaunchTime = Date(); observedOwnedAntigravity = false
            let base = "Antigravity is opening with your provider catalogue."
            if let omitted = omissionSummary(prepared) { tell(base + " " + omitted, warning: true) }
            else { tell(base) }
        } catch {
            updateAntigravityRunning()
            if antigravityRecoveryNeeded && !antigravityRunning {
                if (try? await command("antigravity-restore")) != nil {
                    antigravityRecoveryNeeded = false; antigravityProfileActive = false
                }
            }
            tell(error.localizedDescription, error: true)
        }
    }

    func restoreAntigravity() async {
        guard !busy else { return }
        updateAntigravityRunning()
        if antigravityRunning {
            tell("Quit Antigravity before disconnecting its gateway.", error: true)
            return
        }
        busy = true; defer { busy = false }
        do {
            let result = try await command("antigravity-restore")
            antigravityRecoveryNeeded = false; antigravityProfileActive = false; observedOwnedAntigravity = false
            await stopGatewayIfUnused()
            let kept = result["preserved_external_changes"] as? Int ?? 0
            tell(kept > 0 ? "Disconnected. Changes made in Antigravity were preserved." : "Restored Antigravity’s previous configuration. Your conversations remain saved.")
        } catch { tell(error.localizedDescription, error: true) }
    }

    /// Put Antigravity's own configuration back once the app has quit.
    func pollAntigravityRecovery() async {
        if antigravityProfileActive && antigravityRunning { observedOwnedAntigravity = true }
        let failedLaunch = antigravityProfileActive && !antigravityRunning && !observedOwnedAntigravity && Date().timeIntervalSince(antigravityLaunchTime) > 20
        guard antigravityRecoveryNeeded && !antigravityRunning && (observedOwnedAntigravity || failedLaunch) && !busy && !finishingSession else { return }
        finishingSession = true; defer { finishingSession = false }
        do {
            let finished = try await finishClosedSession(autoStop: savedSettings.auto_stop) {
                _ = try await command("antigravity-restore")
                antigravityRecoveryNeeded = false; antigravityProfileActive = false; observedOwnedAntigravity = false
            }
            if finished { tell("Antigravity closed. Its previous configuration has been restored.") }
        } catch { tell(error.localizedDescription, error: true) }
    }
}
