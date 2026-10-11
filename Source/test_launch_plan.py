"""Drive the Hub's production launch orchestration on macOS.

Source/LaunchPlan.swift holds the code BridgeModel runs for launchClaude,
launchCodex, restore and the poll-driven auto-restore: pending-settings
writes, the gateway snapshot check and restart, rollback of a refused launch
and the auto-stop. It is compiled here unchanged with HubModels.swift and a
scripted LaunchHost standing in for the worker, the gateway's /_bridge/status
and the restart alert. The scripted gateway serves the snapshot of the
settings it last started from, listens on the port it started with, and
records any stop that would sever in-flight requests.
"""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parent

CASES = r'''
import Foundation

func check(_ yes: Bool, _ text: String) {
    if !yes { FileHandle.standardError.write(Data("FAIL: \(text)\n".utf8)); exit(1) }
}

@MainActor final class ScriptedHost: LaunchHost {
    var busy = false
    var activeRequests = 0
    var settings = RouteSettings()
    var savedSettings = RouteSettings()
    var chatWindowOpen = false
    var chatWorking = false
    var chatHasActiveWork = false
    var claudeLive = false
    var codexLive = false
    // settings.json, and the gateway process.
    var disk = RouteSettings()
    var gatewayUp = true
    var listeningPort = 0
    var servingFingerprint = ""
    var servingDigest = ""
    var inFlight = 0
    var confirmAnswer = true
    var onConfirm: (() -> Void)?
    var onPrepare: (() async -> Void)?
    var prepareError: String?
    // One entry per upcoming write: "fail" throws before the disk changes,
    // "lost" changes the disk and then throws, as when the worker's reply is
    // lost. Writes past the script succeed. startFailures fails that many starts.
    var writeScript: [String] = []
    var startFailures = 0
    var events: [String] = []
    var running: Bool { gatewayUp }

    init() { load() }
    // Stand-ins for catalogue_fingerprint and catalogue_digest: each reads
    // only the settings its real counterpart reads. Codex's desktop helper
    // preferences change neither.
    static func fingerprint(_ s: RouteSettings) -> String { "fp:" + (s.mappings["claude-opus-5"] ?? "") }
    static func digest(_ s: RouteSettings) -> String { "dg:" + (s.codex_model ?? "") + ":\(s.codex_goal_budget)" }
    func load() {
        listeningPort = disk.port
        servingFingerprint = Self.fingerprint(disk)
        servingDigest = Self.digest(disk)
    }

    func refreshDesktopState() {}
    func waitForCatalogueRefresh() async {}
    func writeSettings() async throws {
        events.append("write")
        let step = writeScript.isEmpty ? "ok" : writeScript.removeFirst()
        if step == "fail" { throw LaunchRefusal(message: "Could not save the settings.") }
        disk = settings
        if step == "lost" { throw LaunchRefusal(message: "Could not save the settings.") }
        savedSettings = settings
    }
    func gatewayStatus() async throws -> GatewayStatus? {
        events.append("status")
        // The Hub's endpoint is the saved port.
        guard gatewayUp, savedSettings.port == listeningPort else { throw LaunchRefusal(message: "Could not connect to the server.") }
        return GatewayStatus(active: inFlight, fingerprint: servingFingerprint, digest: servingDigest)
    }
    func startGateway() async throws {
        if gatewayUp { return }
        events.append("start")
        if startFailures > 0 { startFailures -= 1; throw LaunchRefusal(message: "The gateway could not start.") }
        gatewayUp = true; load()
    }
    func stopGateway() async {
        guard gatewayUp else { return }
        events.append(inFlight > 0 ? "stop-SEVERED" : "stop")
        gatewayUp = false; activeRequests = 0
    }
    func confirmGatewayRestart(peersConnected: Bool, otherHarness: String) -> Bool {
        events.append("confirm"); onConfirm?(); return confirmAnswer
    }

    /// What launchClaude / launchCodex pass to prepareGatewayForLaunch: the
    /// worker prepares from the saved settings on disk.
    func launch(_ surface: LaunchSurface) async -> String? {
        do {
            _ = try await prepareGatewayForLaunch(surface) {
                events.append("prepare")
                await onPrepare?()
                if let prepareError { throw LaunchRefusal(message: prepareError) }
                return surface == .claude ? ["catalogue_fingerprint": Self.fingerprint(disk)]
                                          : ["catalogue_digest": Self.digest(disk)]
            }
            return nil
        } catch { return error.localizedDescription }
    }
    var stopped: Bool { events.contains { $0.hasPrefix("stop") } }
    var severed: Bool { events.contains("stop-SEVERED") }
}

@MainActor func host(_ configure: (ScriptedHost) -> Void = { _ in }) -> ScriptedHost {
    let host = ScriptedHost()
    configure(host)
    return host
}

@main struct Cases {
    @MainActor static func main() async throws {
        // Unchanged adjacent launches open while every peer is busy.
        for surface in [LaunchSurface.claude, .codex] {
            let h = host { $0.claudeLive = surface == .codex; $0.codexLive = surface == .claude
                           $0.chatWorking = true; $0.chatWindowOpen = true; $0.inFlight = 2; $0.activeRequests = 2 }
            let error = await h.launch(surface)
            check(error == nil, "adjacent \(surface) launch refused: \(error ?? "")")
            check(!h.stopped && !h.events.contains("write") && !h.events.contains("confirm"), "adjacent launch disturbed the gateway: \(h.events)")
        }

        // Dirty config for the idle target while a peer is busy: the new
        // selection needs a reload the busy gateway cannot take, so the launch
        // is refused with the gateway's count and nothing stays saved.
        do {
            let h = host { $0.claudeLive = true; $0.inFlight = 1 }
            let previous = h.savedSettings
            h.settings.codex_model = "mistral/new-model"
            let error = await h.launch(.codex) ?? ""
            check(error.hasPrefix("The gateway has 1 request in flight."), "busy-peer refusal: \(error)")
            check(error.contains("were not saved"), "refusal does not say the edit is pending: \(error)")
            check(h.disk == previous && h.savedSettings == previous, "refused launch left settings saved")
            check(h.settings.codex_model == "mistral/new-model", "refused launch dropped the draft")
            check(!h.stopped, "busy gateway was stopped: \(h.events)")
        }
        // A Codex desktop preference leaves the digest alone, so it is saved
        // and the launch opens on the running gateway.
        do {
            let h = host { $0.claudeLive = true; $0.inFlight = 1 }
            h.settings.codex_accent_slider = true
            let error = await h.launch(.codex)
            check(error == nil, "snapshot-neutral Codex preference refused: \(error ?? "")")
            check(h.disk.codex_accent_slider && !h.stopped, "preference not saved or gateway disturbed: \(h.events)")
        }
        // A live app's own profile is never rewritten, and nothing is written.
        do {
            let h = host { $0.codexLive = true }
            h.settings.codex_model = "mistral/new-model"
            let error = await h.launch(.codex) ?? ""
            check(error.hasPrefix("Quit Codex / ChatGPT"), "live Codex edit: \(error)")
            check(h.events.isEmpty, "live Codex edit touched the gateway or disk: \(h.events)")
            let c = host { $0.claudeLive = true }
            c.settings.mappings["claude-opus-5"] = "kimi/k3"
            let claudeError = await c.launch(.codex) ?? ""
            check(claudeError.hasPrefix("Quit Claude"), "live Claude edit: \(claudeError)")
            check(c.events.isEmpty, "live Claude edit was written: \(c.events)")
        }

        // Mixed settings while Chat is working and no desktop app is live: the
        // refusal names what is actually busy, never a desktop session.
        for (inFlight, expected) in [(1, "The gateway has 1 request in flight."), (0, "Chat is working.")] {
            let h = host { $0.chatWorking = true; $0.chatWindowOpen = true; $0.inFlight = inFlight; $0.activeRequests = inFlight }
            let previous = h.savedSettings
            h.settings.mappings["claude-opus-5"] = "kimi/k3"
            h.settings.codex_model = "mistral/new-model"
            let draft = h.settings
            let error = await h.launch(.claude) ?? ""
            check(error.hasPrefix(expected), "mixed + Chat refusal: \(error)")
            check(!error.contains("desktop session"), "mixed + Chat blamed a desktop session: \(error)")
            check(h.disk == previous && h.settings == draft && !h.stopped, "mixed + Chat left a partial change: \(h.events)")
        }

        // Port change with Chat idle: the old server stops before the new
        // port is saved, and the restarted gateway is reachable at it.
        do {
            let h = host { $0.chatWindowOpen = true }
            let old = h.disk.port
            h.settings.port = old + 1
            let error = await h.launch(.claude)
            check(error == nil, "port change refused: \(error ?? "")")
            check(h.events == ["status", "stop", "write", "prepare", "start", "status"], "port change order: \(h.events)")
            check(h.listeningPort == old + 1 && h.savedSettings.port == old + 1, "gateway not on the saved port")
            check(((try? await h.gatewayStatus()) ?? nil) != nil, "saved endpoint unreachable after port change")
        }
        // ...is refused before anything is written while a request is in
        // flight, even if the poll has not seen it yet.
        do {
            let h = host { $0.inFlight = 1; $0.activeRequests = 0 }
            let old = h.disk.port
            h.settings.port = old + 1
            let error = await h.launch(.codex) ?? ""
            check(error.hasPrefix("The gateway has 1 request in flight."), "busy port change: \(error)")
            check(!h.stopped && !h.events.contains("write") && h.disk.port == old && h.settings.port == old + 1, "busy port change: \(h.events)")
        }
        // ...is refused outright while a desktop app is live on the gateway.
        do {
            let h = host { $0.codexLive = true }
            h.settings.port += 1
            let error = await h.launch(.claude) ?? ""
            check(error.hasPrefix("Quit the desktop sessions"), "live port change: \(error)")
            check(h.events.isEmpty, "live port change touched the gateway: \(h.events)")
        }
        // ...rolls back, and serves the old port again, if preparation fails.
        do {
            let h = host { $0.chatWindowOpen = true; $0.prepareError = "No selected model is available for Claude right now." }
            let old = h.disk.port
            h.settings.port = old + 1
            let error = await h.launch(.claude) ?? ""
            check(error.hasPrefix("No selected model") && error.contains("were not saved"), "failed port launch: \(error)")
            check(h.disk.port == old && h.savedSettings.port == old && h.settings.port == old + 1, "failed port launch kept the port")
            check(h.gatewayUp && h.listeningPort == old, "failed port launch left the gateway down or moved")
        }
        // ...and the same after the old gateway has stopped, if the disk write
        // throws, whether or not it reached the disk.
        for step in ["fail", "lost"] {
            let h = host { $0.chatWindowOpen = true; $0.writeScript = [step] }
            let old = h.disk.port
            h.settings.port = old + 1
            let draft = h.settings
            let error = await h.launch(.claude) ?? ""
            check(error.hasPrefix("Could not save the settings.") && error.contains("were not saved"), "\(step) shared write: \(error)")
            check(h.events == ["status", "stop", "write", "write", "start"], "\(step) shared write order: \(h.events)")
            check(h.disk.port == old && h.savedSettings.port == old && h.settings == draft, "\(step) shared write kept a partial change")
            check(h.gatewayUp && h.listeningPort == old, "\(step) shared write left the old gateway down")
        }
        // ...if the new gateway cannot start, the old settings are saved and
        // served again.
        do {
            let h = host { $0.chatWindowOpen = true; $0.startFailures = 1 }
            let old = h.disk.port
            h.settings.port = old + 1
            let error = await h.launch(.claude) ?? ""
            check(error.hasPrefix("The gateway could not start.") && error.contains("were not saved"), "failed shared start: \(error)")
            check(h.events == ["status", "stop", "write", "prepare", "start", "write", "start"], "failed shared start order: \(h.events)")
            check(h.gatewayUp && h.listeningPort == old && h.savedSettings.port == old && h.settings.port == old + 1, "failed shared start not restored")
        }
        // ...if the rollback write fails too, the new settings that did land
        // are served, so the gateway matches the Hub's saved endpoint.
        do {
            let h = host { $0.chatWindowOpen = true; $0.startFailures = 1; $0.writeScript = ["ok", "fail"] }
            let old = h.disk.port
            h.settings.port = old + 1
            let error = await h.launch(.claude) ?? ""
            check(error.contains("stay saved, and the gateway has loaded them."), "kept shared change: \(error)")
            check(h.gatewayUp && h.listeningPort == old + 1 && h.savedSettings.port == old + 1 && h.settings == h.savedSettings, "kept shared change not served")
        }
        // ...and when neither write is confirmed the disk may disagree with the
        // saved endpoint, so the gateway stays stopped and the launch says so.
        do {
            let h = host { $0.chatWindowOpen = true; $0.writeScript = ["fail", "fail"] }
            let old = h.disk.port
            h.settings.port = old + 1
            let draft = h.settings
            let error = await h.launch(.claude) ?? ""
            check(error.contains("could not confirm which settings are saved") && error.contains("choose Start gateway"), "unconfirmed shared write: \(error)")
            check(!h.gatewayUp && h.savedSettings.port == old && h.settings == draft, "unconfirmed shared write: \(h.events)")
        }

        // A selection that needs a reload asks first; cancelling saves nothing.
        do {
            let h = host { $0.chatWindowOpen = true; $0.confirmAnswer = false }
            let previous = h.savedSettings
            h.settings.mappings["claude-opus-5"] = "kimi/k3"
            let error = await h.launch(.claude) ?? ""
            check(error.hasPrefix("Launch cancelled."), "cancelled restart: \(error)")
            check(h.disk == previous && h.settings.mappings["claude-opus-5"] == "kimi/k3" && !h.stopped, "cancelled restart: \(h.events)")
        }
        // Work that starts while the alert is up is checked again before the
        // restart, so it is never severed.
        for (label, arrive) in [("request", { (h: ScriptedHost) in h.inFlight = 1 }), ("chat", { (h: ScriptedHost) in h.chatWorking = true })] {
            let h = host()
            h.onConfirm = { arrive(h) }
            let previous = h.savedSettings
            h.settings.mappings["claude-opus-5"] = "kimi/k3"
            let error = await h.launch(.claude) ?? ""
            let expected = label == "request" ? "The gateway has 1 request in flight." : "Chat is working."
            check(error.hasPrefix(expected), "\(label) during confirmation: \(error)")
            check(!h.stopped && h.disk == previous, "\(label) during confirmation: \(h.events)")
        }
        // A confirmed restart reloads the gateway and keeps the settings.
        do {
            let h = host()
            h.settings.mappings["claude-opus-5"] = "kimi/k3"
            let error = await h.launch(.claude)
            check(error == nil, "confirmed restart: \(error ?? "")")
            check(h.events == ["write", "prepare", "status", "confirm", "status", "stop", "start", "status"], "confirmed restart order: \(h.events)")
            check(h.servingFingerprint == "fp:kimi/k3" && h.disk.mappings["claude-opus-5"] == "kimi/k3", "restart did not load the selection")
        }

        // An app that quits while a sibling launch holds busy is not restored
        // over it, and the gateway the launch is using stays up.
        do {
            let h = host()
            var resume: CheckedContinuation<Void, Never>?
            h.onPrepare = { await withCheckedContinuation { resume = $0 } }
            h.busy = true  // launchClaude / launchCodex hold busy for the whole launch
            let launch = Task { await h.launch(.codex) }
            while resume == nil { await Task.yield() }
            let finished = try await h.finishClosedSession(autoStop: true) { h.events.append("restore") }
            check(!finished && !h.events.contains("restore") && h.gatewayUp, "auto-restore ran during a launch: \(h.events)")
            resume?.resume()
            let error = await launch.value
            h.busy = false
            check(error == nil && h.gatewayUp, "launch failed after deferred restore: \(error ?? "")")
            var heldBusy = false
            let restored = try await h.finishClosedSession(autoStop: false) { heldBusy = h.busy }
            check(restored && heldBusy && !h.busy, "auto-restore did not hold busy for its duration")
        }
        // Auto-stop re-reads the in-flight count and keeps a used gateway.
        do {
            let h = host { $0.inFlight = 1; $0.activeRequests = 0 }
            _ = try await h.finishClosedSession(autoStop: true) {}
            check(h.gatewayUp && !h.severed, "auto-stop severed a request the poll had not seen: \(h.events)")
            h.inFlight = 0
            _ = try await h.finishClosedSession(autoStop: true) {}
            check(!h.gatewayUp, "idle unused gateway was not stopped: \(h.events)")
            let chat = host { $0.chatWindowOpen = true }
            await chat.stopGatewayIfUnused()
            check(chat.gatewayUp, "auto-stop stopped the gateway under an open Chat")
        }
        print("ok")
    }
}
'''


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "Swift launch orchestration test needs macOS")
class LaunchOrchestrationTests(unittest.TestCase):
    def test_production_launch_orchestration(self):
        bridge = (SOURCE / "MistralBridge.swift").read_text()
        slots = bridge[bridge.index("let slots:"):bridge.index("/// Family tier")]
        with tempfile.TemporaryDirectory(prefix="hub-launch-plan-") as tmp:
            root = Path(tmp)
            (root / "Slots.swift").write_text("import Foundation\n" + slots)
            (root / "Cases.swift").write_text(CASES)
            executable = root / "cases"
            build = subprocess.run([
                "xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-module-cache-path", str(root / "ModuleCache"),
                str(SOURCE / "CatalogueSelection.swift"), str(SOURCE / "HubModels.swift"),
                str(SOURCE / "LaunchPlan.swift"), str(root / "Slots.swift"), str(root / "Cases.swift"),
                "-o", str(executable),
            ], capture_output=True, text=True, timeout=240)
            self.assertEqual(build.returncode, 0, build.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
