"""Drive the production Swift launch flow through busy peers and live refresh."""
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
    var disk = RouteSettings()
    var chatWindowOpen = false
    var chatWorking = false
    var chatHasActiveWork = false
    var claudeLive = false
    var codexLive = false
    var running = true
    var inFlight = 0
    var port = 0
    var fingerprint = ""
    var digest = ""
    var events: [String] = []
    var writes: [String] = []
    var prepareError: String?
    var refreshError: String?
    var wrongRefresh = false
    var onPrepare: (() async -> Void)?
    init() { load() }
    static func fp(_ s: RouteSettings) -> String { "fp:" + (s.mappings["claude-opus-5"] ?? "") }
    static func dg(_ s: RouteSettings) -> String { "dg:" + (s.codex_model ?? "") + ":\(s.codex_goal_budget)" }
    func load() { port = disk.port; fingerprint = Self.fp(disk); digest = Self.dg(disk) }
    func refreshDesktopState() {}
    func waitForCatalogueRefresh() async {}
    func writeSettings() async throws {
        events.append("write")
        let step = writes.isEmpty ? "ok" : writes.removeFirst()
        if step == "fail" { throw LaunchRefusal(message: "Save failed") }
        disk = settings
        if step == "lost" { throw LaunchRefusal(message: "Save failed") }
        savedSettings = settings
    }
    func gatewayStatus() async throws -> GatewayStatus? {
        events.append("status")
        guard running, savedSettings.port == port else { throw LaunchRefusal(message: "Unreachable") }
        return GatewayStatus(active: inFlight, fingerprint: fingerprint, digest: digest)
    }
    func refreshGateway(_ surface: LaunchSurface, expected: String) async throws -> GatewayStatus? {
        events.append("refresh")
        if let refreshError { throw LaunchRefusal(message: refreshError) }
        if !wrongRefresh { load() }
        return GatewayStatus(active: inFlight, fingerprint: fingerprint, digest: digest)
    }
    func startGateway() async throws { events.append("start"); running = true; load() }
    func stopGateway() async { events.append(inFlight > 0 ? "SEVERED" : "stop"); running = false }
    func launch(_ surface: LaunchSurface) async -> String? {
        do {
            _ = try await prepareGatewayForLaunch(surface) {
                events.append("prepare"); await onPrepare?()
                if let prepareError { throw LaunchRefusal(message: prepareError) }
                return surface == .claude ? ["catalogue_fingerprint": Self.fp(disk)] : ["catalogue_digest": Self.dg(disk)]
            }
            return nil
        } catch { return error.localizedDescription }
    }
}
@main struct Cases {
    @MainActor static func main() async throws {
        // A changed selection launches under every kind of current activity,
        // regardless of a stale active count in the UI's last poll.
        for surface in [LaunchSurface.claude, .codex] {
            for count in [0, 1, 8] {
                let h = ScriptedHost()
                h.chatWorking = true; h.chatHasActiveWork = true; h.chatWindowOpen = true
                h.inFlight = count; h.activeRequests = 0
                h.claudeLive = surface == .codex; h.codexLive = surface == .claude
                if surface == .claude { h.settings.mappings["claude-opus-5"] = "kimi/k3" }
                else { h.settings.codex_model = "mistral/new-model" }
                let error = await h.launch(surface)
                check(error == nil, "busy launch: \(error ?? "")")
                check(h.events == ["write", "prepare", "status", "refresh"], "busy launch order: \(h.events)")
                check(h.running && h.inFlight == count && h.activeRequests == count, "activity disturbed")
                check(h.disk == h.savedSettings, "selection not saved")
            }
        }
        // No refresh when the prepared selection is already served.
        let unchanged = ScriptedHost()
        unchanged.inFlight = 3; unchanged.chatWorking = true
        let unchangedError = await unchanged.launch(.codex)
        check(unchangedError == nil && unchanged.events == ["prepare", "status"], "unchanged launch")
        let preference = ScriptedHost()
        preference.settings.codex_accent_slider = true
        let preferenceError = await preference.launch(.codex)
        check(preferenceError == nil && !preference.events.contains("refresh"), "neutral preference refreshed")

        // Chat activity does not prevent mixed desktop catalogue preparation.
        let mixed = ScriptedHost()
        mixed.chatWorking = true; mixed.inFlight = 2
        mixed.settings.mappings["claude-opus-5"] = "kimi/k3"; mixed.settings.codex_model = "mistral/new"
        let mixedError = await mixed.launch(.claude)
        check(mixedError == nil && mixed.fingerprint == ScriptedHost.fp(mixed.disk), "mixed launch failed")

        // Live app profile edits are still guarded independently of activity.
        for surface in [LaunchSurface.claude, .codex] {
            let h = ScriptedHost()
            if surface == .claude { h.claudeLive = true; h.settings.mappings["claude-opus-5"] = "kimi/k3" }
            else { h.codexLive = true; h.settings.codex_model = "mistral/new" }
            let error = await h.launch(surface)
            check(error?.hasPrefix("Quit") == true && h.events.isEmpty, "live profile overwritten")
        }

        // A pending port change never moves the endpoint under active peers
        // or blocks a launch. It remains a pending UI edit after launch.
        for surface in [LaunchSurface.claude, .codex] {
            let h = ScriptedHost(); let old = h.port
            h.inFlight = 2; h.chatWorking = true
            h.claudeLive = surface == .codex; h.codexLive = surface == .claude
            h.settings.port = old + 1
            if surface == .codex { h.settings.codex_model = "mistral/new" }
            let error = await h.launch(surface)
            check(error == nil, "pending port blocked launch: \(error ?? "")")
            check(h.port == old && h.disk.port == old && h.savedSettings.port == old && h.settings.port == old + 1, "port moved")
            check(!h.events.contains("stop") && !h.events.contains("SEVERED"), "gateway stopped for port")
        }
        // When no gateway runs the new port can be used directly.
        let fresh = ScriptedHost(); fresh.running = false; fresh.settings.port += 1
        let freshError = await fresh.launch(.claude)
        check(freshError == nil && fresh.port == fresh.settings.port && fresh.events.contains("start"), "fresh gateway")

        // Preparation, write and refresh failures preserve the running gateway
        // and roll back disk state without dropping the user's draft.
        for failure in ["prepare", "refresh", "write", "lost"] {
            let h = ScriptedHost(); let previous = h.savedSettings
            h.inFlight = 1; h.settings.codex_model = "mistral/new"
            let draft = h.settings
            if failure == "prepare" { h.prepareError = "Prepare failed" }
            if failure == "refresh" { h.refreshError = "Refresh failed" }
            if failure == "write" { h.writes = ["fail"] }
            if failure == "lost" { h.writes = ["lost"] }
            let error = await h.launch(.codex)
            check(error != nil && h.disk == previous && h.savedSettings == previous && h.settings == draft, "rollback \(failure)")
            check(h.running && h.inFlight == 1 && !h.events.contains("SEVERED"), "failure stopped gateway")
        }
        let wrong = ScriptedHost(); wrong.settings.codex_model = "mistral/new"; wrong.wrongRefresh = true
        let wrongError = await wrong.launch(.codex)
        check(wrongError?.contains("did not load") == true && wrong.running, "wrong refresh activated")

        // Auto-stop still consults live request counts and holds operation busy.
        let idle = ScriptedHost(); idle.inFlight = 1
        _ = try await idle.finishClosedSession(autoStop: true) { check(idle.busy, "restore did not hold busy") }
        check(idle.running, "auto-stop severed request")
        idle.inFlight = 0
        _ = try await idle.finishClosedSession(autoStop: true) {}
        check(!idle.running, "unused gateway not stopped")
        let chat = ScriptedHost(); chat.chatWindowOpen = true
        await chat.stopGatewayIfUnused()
        check(chat.running, "open Chat lost gateway")
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
                "xcrun", "swiftc", "-swift-version", "5", "-parse-as-library", "-module-cache-path", str(root / "ModuleCache"),
                str(SOURCE / "CatalogueSelection.swift"), str(SOURCE / "HubModels.swift"), str(SOURCE / "LaunchPlan.swift"),
                str(root / "Slots.swift"), str(root / "Cases.swift"), "-o", str(executable),
            ], capture_output=True, text=True, timeout=240)
            self.assertEqual(build.returncode, 0, build.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
