// The Hub's launch orchestration for the shared gateway: which pending
// settings may be written while peers are served, when the gateway must be
// refreshed, and when an idle gateway may stop. Claude Desktop, Codex /
// ChatGPT Desktop and Chat share one gateway, so a launch must not refuse
// an adjacent app merely because a peer is busy, must never stop the gateway
// under in-flight work, and must never rewrite a live
// app's profile.
//
// This file holds the production code paths themselves. BridgeModel conforms
// to LaunchHost and supplies the AppKit/worker primitives; test_launch_plan.py
// compiles this file against a scripted host and drives the same methods.

import Foundation

/// The category of a pending settings change, which decides who must be idle
/// before it is written.
enum ChangeKind: String, Equatable {
    case unchanged
    /// Read live or at the next activation; never needs a gateway restart.
    case prefs
    /// Only Codex / ChatGPT's selection or desktop preferences.
    case codexOnly
    /// Claude's model selection (mappings, catalogue, branding).
    case claudeRouting
    /// Both selections.
    case mixed
    /// The port or a provider connection. The running gateway listens on the
    /// saved port; a launch defers a port edit while the listener is running.
    /// Provider connection changes can be loaded by a routing refresh.
    case shared
}

func classifyChange(_ settings: RouteSettings, from saved: RouteSettings) -> ChangeKind {
    if settings == saved { return .unchanged }
    // The gateway re-reads the active CLI account on every turn, so adding,
    // renaming or switching accounts is a preference, not a connection.
    var connections = settings.providers
    for id in connections.keys {
        connections[id]?.cli_accounts = saved.providers[id]?.cli_accounts
        connections[id]?.cli_account = saved.providers[id]?.cli_account
    }
    if settings.port != saved.port || connections != saved.providers { return .shared }
    var mine = settings
    mine.codex_model = saved.codex_model
    mine.codex_catalogue = saved.codex_catalogue
    mine.codex_chatgpt_account = saved.codex_chatgpt_account
    mine.codex_apply_patch_all = saved.codex_apply_patch_all
    mine.codex_apply_patch = saved.codex_apply_patch
    mine.codex_apply_patch_exclude = saved.codex_apply_patch_exclude
    mine.codex_accent_slider = saved.codex_accent_slider
    mine.codex_quick_composer = saved.codex_quick_composer
    mine.codex_quick_composer_window = saved.codex_quick_composer_window
    mine.codex_hide_usage_banner = saved.codex_hide_usage_banner
    mine.codex_unlock_composer = saved.codex_unlock_composer
    mine.codex_goal_budget = saved.codex_goal_budget
    mine.codex_subagent_rank = saved.codex_subagent_rank
    mine.codex_subagent_route = saved.codex_subagent_route
    let codexChanged = mine != settings
    var prefsOnly = mine
    prefsOnly.providers = saved.providers  // only CLI accounts can differ here
    prefsOnly.auto_stop = saved.auto_stop
    prefsOnly.auto_mode = saved.auto_mode
    prefsOnly.claude_features = saved.claude_features
    prefsOnly.claude_code_settings = saved.claude_code_settings
    prefsOnly.claude_workflows = saved.claude_workflows
    if prefsOnly == saved { return codexChanged ? .codexOnly : .prefs }
    return codexChanged ? .mixed : .claudeRouting
}

enum LaunchSurface {
    case claude, codex

    var label: String { self == .claude ? "Claude" : "Codex / ChatGPT" }
    var otherLabel: String { self == .claude ? "Codex / ChatGPT" : "Claude" }
    /// The prepare command's result key for the snapshot this surface needs,
    /// and the gateway's own value for it.
    var preparedKey: String { self == .claude ? "catalogue_fingerprint" : "catalogue_digest" }
    func serving(_ status: GatewayStatus) -> String? { self == .claude ? status.fingerprint : status.digest }
}

/// The parts of `/_bridge/status` the launch flow reads. `active` is the
/// gateway's global in-flight count; it is not attributed to any one app.
struct GatewayStatus {
    var active: Int?
    var fingerprint: String?
    var digest: String?
}

struct LaunchRefusal: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

@MainActor
protocol LaunchHost: AnyObject {
    var busy: Bool { get set }
    var running: Bool { get }
    var activeRequests: Int { get set }
    var settings: RouteSettings { get set }
    var savedSettings: RouteSettings { get set }
    var chatWindowOpen: Bool { get }
    var chatWorking: Bool { get }
    var chatHasActiveWork: Bool { get }
    /// Running on the profile the Hub activated for this gateway.
    var claudeLive: Bool { get }
    var codexLive: Bool { get }
    func refreshDesktopState()
    func waitForCatalogueRefresh() async
    /// Persist `settings` and make it the saved state.
    func writeSettings() async throws
    /// nil when the gateway answered but not with a readable status.
    func gatewayStatus() async throws -> GatewayStatus?
    func startGateway() async throws
    func stopGateway() async
    func refreshGateway(_ surface: LaunchSurface, expected: String) async throws -> GatewayStatus?
}

extension LaunchHost {
    var changeKind: ChangeKind { classifyChange(settings, from: savedSettings) }
    var chatBusy: Bool { chatWorking || chatHasActiveWork }

    /// Starting a service must not inherit Save's activity restrictions. While
    /// sessions are active, serve the saved endpoint and leave edits pending.
    func startGatewayForUse(save: () async throws -> Void) async throws {
        guard !running else { return }
        refreshDesktopState()
        if !(chatBusy || activeRequests > 0 || claudeLive || codexLive) { try await save() }
        try await startGateway()
    }

    /// No surface is keeping the gateway alive and nothing is in flight.
    func mayStopGateway() -> Bool {
        !(claudeLive || codexLive || chatWindowOpen || chatBusy) && activeRequests == 0
    }

    /// A live app's profile is never rewritten from under it.
    func liveProfileRefusal(_ kind: ChangeKind) -> String? {
        switch kind {
        case .codexOnly where codexLive:
            return "Quit Codex / ChatGPT before changing its model catalogue or default."
        case .claudeRouting where claudeLive:
            return "Quit Claude before changing its model or provider settings."
        case .mixed where claudeLive || codexLive:
            return "Quit the desktop sessions using this gateway before changing both apps' model settings."
        default:
            return nil
        }
    }

    /// Save pending settings, prepare `surface`, and leave the gateway serving
    /// the prepared snapshot. Live refresh preserves each in-flight request.
    /// A pending port change stays pending while the existing listener runs.
    /// When the launch fails
    /// before the gateway reloads, the saved settings are rolled back and the
    /// edits stay pending, so a refused or cancelled launch changes nothing.
    func prepareGatewayForLaunch(_ surface: LaunchSurface,
                                 prepare: () async throws -> [String: Any]) async throws -> [String: Any] {
        await waitForCatalogueRefresh()
        refreshDesktopState()
        let previous = savedSettings, draft = settings
        let deferredPort = running && settings.port != savedSettings.port ? settings.port : nil
        if deferredPort != nil { settings.port = savedSettings.port }
        defer { if let deferredPort { settings.port = deferredPort } }
        let kind = changeKind
        if let refusal = liveProfileRefusal(kind) { throw LaunchRefusal(message: refusal) }
        var writing = false, wrote = false, reloaded = false
        do {
            // Selection changes are safe to write while peers are served: the
            // running requests keep the snapshot they started with, and
            // the live app (refused above) is never the one being changed.
            if kind != .unchanged { writing = true; try await writeSettings(); wrote = true }
            let prepared = try await prepare()
            if !running { try await startGateway(); reloaded = true }
            try await ensureGatewaySnapshot(surface, expected: prepared[surface.preparedKey] as? String, reloaded: &reloaded)
            return prepared
        } catch {
            guard !reloaded else { throw error }
            // Put back what was saved before this launch. A write that threw
            // may still have reached the disk, so roll back after any attempt.
            var reverted = true, keptNew = false
            if writing {
                let pending = settings
                settings = previous
                do { try await writeSettings(); settings = draft }
                catch _ { settings = pending; reverted = false; keptNew = wrote }
            }
            var message = error.localizedDescription
            if keptNew {
                message += " The new settings stay saved but are not loaded yet."
            } else if !reverted {
                message += " Provider Hub could not confirm which settings are saved."
            } else if writing {
                message += " Your settings changes were not saved; they are still pending."
            }
            if message == error.localizedDescription { throw error }
            throw LaunchRefusal(message: message)
        }
    }

    /// Refresh only when preparation changed the serving snapshot. Activity
    /// does not block launches: the gateway pins routing per request.
    func ensureGatewaySnapshot(_ surface: LaunchSurface, expected: String?, reloaded: inout Bool) async throws {
        guard running, let expected else { return }
        let status = try await gatewayStatus()
        if let active = status?.active { activeRequests = active }
        if let status, surface.serving(status) == expected { return }
        let refreshed = try await refreshGateway(surface, expected: expected)
        reloaded = true
        guard let refreshed, surface.serving(refreshed) == expected else {
            throw LaunchRefusal(message: "The gateway did not load the prepared selection. Try launching again.")
        }
        if let active = refreshed.active { activeRequests = active }
    }

    /// Stop the gateway when nothing uses it, checking the live in-flight
    /// count first. A gateway whose status cannot be read is left running.
    func stopGatewayIfUnused() async {
        // The poll's cached count may be stale either way, so read it live.
        guard !(claudeLive || codexLive || chatWindowOpen || chatBusy) else { return }
        if running {
            guard let active = (try? await gatewayStatus())?.active else { return }
            activeRequests = active
        }
        guard mayStopGateway() else { return }
        await stopGateway()
    }

    /// Restore a desktop app that quit on its own. Holds `busy` so a launch
    /// cannot start while the restore and auto-stop run, and does nothing
    /// while another Hub operation holds it.
    @discardableResult
    func finishClosedSession(autoStop: Bool, restore: () async throws -> Void) async throws -> Bool {
        guard !busy else { return false }
        busy = true; defer { busy = false }
        try await restore()
        if autoStop { await stopGatewayIfUnused() }
        return true
    }
}
