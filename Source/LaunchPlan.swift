// LaunchPlan — the Hub's launch-decision policy, isolated so the Swift
// test harness can exercise the actual production code (no Python
// mirror, no parallel policy). The resolver is pure: it takes the
// state the orchestrator already knows and returns one of four
// actions the orchestrator then executes.
//
// The four actions:
//
//   open_directly       — gateway snapshot already matches the prepared
//                         plan. Activate the app's profile and open it;
//                         no save, no restart, no chat block. The
//                         "open directly" verb is the relaxed-launch win.
//
//   save_and_open       — settings need persisting but the gateway
//                         snapshot already matches. Save without
//                         disturbing the gateway and open.
//
//   restart_and_open    — gateway snapshot does not match. The
//                         orchestrator offers a restart alert (no
//                         in-flight work) or blocks with a named
//                         reason.
//
//   blocked             — launch cannot proceed; ``blockReason`` names
//                         what is in the way.
//
// Two named-block-message rules the resolver enforces:
//
//   * The gateway's ``active`` count is global, not per-harness. The
//     block reason names "the gateway", never "Codex / ChatGPT" or
//     "Claude", unless the orchestrator passes an attributed count.
//     Attribution is not measured by ``/_bridge/status``; the message
//     would be wrong during a brief restart transition when both
//     harnesses share the gateway.
//
//   * "Chat is working" is the only chat-attributed block. The
//     orchestrator passes ``chatWorking`` and ``chatHasActiveWork``
//     separately so the resolver can name which signal tripped.

import Foundation

/// The settings-change categories the Settings UI and the resolver
/// share. Top-level so LaunchPlan.swift (a pure module) can take it
/// as a parameter; the ``BridgeModel`` side computes the value and
/// passes it through. Raw values are stable for JSON / IPC and for
/// the Python ``launch_plan`` mirror's string constants, should a
/// worker-side entry point ever be added.
public enum ChangeKind: String, Equatable {
    case unchanged = "unchanged"
    case prefs = "prefs"
    case codexOnly = "codexOnly"
    case claudeRouting = "claudeRouting"
    case mixed = "mixed"
}

/// The four actions the orchestrator dispatches on. Kept as a
/// String-rawvalue enum so the values double as IPC payloads if the
/// Swift orchestrator ever asks the worker for a plan.
public enum LaunchAction: String, Equatable {
    case openDirectly = "open_directly"
    case saveAndOpen = "save_and_open"
    case restartAndOpen = "restart_and_open"
    case blocked = "blocked"
}

/// The Hub's decision for one launch attempt. ``otherHarness`` and
/// ``surface`` are display strings the orchestrator may surface in
/// UI copy; the policy itself lives in ``action`` and ``blockReason``.
public struct LaunchPlan: Equatable {
    public var action: LaunchAction
    public var blockReason: String?
    public var requiresUserConfirm: Bool = false
    public var mustRestart: Bool = false
    public var mustSave: Bool = false
    /// The other harness's display name, used in alert copy.
    public var otherHarness: String
    /// The surface being launched ("claude" or "codex").
    public var surface: String

    public init(action: LaunchAction,
                blockReason: String? = nil,
                requiresUserConfirm: Bool = false,
                mustRestart: Bool = false,
                mustSave: Bool = false,
                otherHarness: String,
                surface: String) {
        self.action = action
        self.blockReason = blockReason
        self.requiresUserConfirm = requiresUserConfirm
        self.mustRestart = mustRestart
        self.mustSave = mustSave
        self.otherHarness = otherHarness
        self.surface = surface
    }
}

/// Pure resolver; the orchestrator's only entry point.
///
/// - Parameters:
///   - surface: "claude" or "codex".
///   - gatewayRunning: whether the gateway process is currently up.
///   - gatewayFingerprint / gatewayDigest: the live gateway's
///     ``/_bridge/status`` snapshot values, or ``nil`` when the
///     gateway is not running.
///   - preparedFingerprint / preparedDigest: the values returned by
///     ``prepare-launch`` / ``codex-prepare``; ``nil`` means "do not
///     compare this dimension".
///   - chatWorking / chatHasActiveWork: the two ChatModel signals.
///     The orchestrator passes them separately so the block reason can
///     name whichever is hot.
///   - chatWindowOpen: true when the Chat window is on screen; the
///     may-stop-gateway predicate folds it in.
///   - activeRequests: the gateway's global active count.
///   - changeKind: the same enum the Settings UI surfaces
///     (.unchanged / .prefs / .codexOnly / .claudeRouting / .mixed).
///   - liveClaude: Claude is the live harness on this gateway
///     (running AND on the Hub-owned 3P profile).
///   - liveCodex: Codex / ChatGPT is the live harness (running AND
///     recovery-pending — the marker that the worker activated Codex's
///     profile for this gateway).
public func resolveLaunchPlan(
    surface: String,
    gatewayRunning: Bool,
    gatewayFingerprint: String?,
    gatewayDigest: String?,
    preparedFingerprint: String?,
    preparedDigest: String?,
    chatWorking: Bool,
    chatHasActiveWork: Bool,
    chatWindowOpen: Bool,
    activeRequests: Int,
    changeKind: ChangeKind,
    liveClaude: Bool,
    liveCodex: Bool
) -> LaunchPlan {
    let otherHarness = surface == "claude" ? "Codex / ChatGPT" : "Claude"
    let surfaceLabel = surface == "claude" ? "Claude" : "Codex"
    let chatBusy = chatWorking || chatHasActiveWork

    // 1. Live-harness-vs-change refusal. Hard block: a live app's
    // profile is never rewritten from under it (AGENTS.md: "never
    // rewrite a live app's profile under it"). The names in the block
    // reasons quote the harness directly, which is safe because the
    // change-vs-live test is per-harness.
    if changeKind != .unchanged {
        switch changeKind {
        case .codexOnly where liveCodex:
            return LaunchPlan(action: .blocked,
                             blockReason: "Quit Codex / ChatGPT before changing its model catalogue or default.",
                             otherHarness: otherHarness, surface: surface)
        case .claudeRouting where liveClaude:
            return LaunchPlan(action: .blocked,
                             blockReason: "Quit Claude before changing its provider settings.",
                             otherHarness: otherHarness, surface: surface)
        case .mixed where liveClaude || liveCodex:
            return LaunchPlan(action: .blocked,
                             blockReason: "Quit the desktop sessions using this gateway before changing provider settings.",
                             otherHarness: otherHarness, surface: surface)
        default:
            break
        }
    }

    // 2. Snapshot comparison. If the gateway already carries what this
    //    launch needs, no save is needed unless settings changed, and
    //    no restart is ever needed.
    let mustSave = changeKind != .unchanged
    let matches = snapshotsMatchFn(
        surface: surface,
        gatewayRunning: gatewayRunning,
        gatewayFingerprint: gatewayFingerprint,
        gatewayDigest: gatewayDigest,
        preparedFingerprint: preparedFingerprint,
        preparedDigest: preparedDigest)
    if matches {
        return LaunchPlan(action: mustSave ? .saveAndOpen : .openDirectly,
                         mustRestart: false, mustSave: mustSave,
                         otherHarness: otherHarness, surface: surface)
    }

    // 3. Snapshot mismatch — a gateway restart is needed. Name the
    //    holders rather than refusing generically. The gateway's
    //    ``active`` count is GLOBAL — a request in flight could be
    //    either harness's during a brief restart transition — so the
    //    block reason names "the gateway", not a specific harness.
    if activeRequests > 0 {
        let noun = activeRequests == 1 ? "request" : "requests"
        return LaunchPlan(
            action: .blocked,
            blockReason: "The gateway has \(activeRequests) \(noun) in flight. Wait for them to finish, then launch again so the gateway can load \(surfaceLabel)'s selection.",
            mustRestart: true, mustSave: mustSave,
            otherHarness: otherHarness, surface: surface)
    }
    if chatBusy {
        return LaunchPlan(
            action: .blocked,
            blockReason: "Chat is working. Stop the Chat turn, then launch again so the gateway can load \(surfaceLabel)'s selection.",
            mustRestart: true, mustSave: mustSave,
            otherHarness: otherHarness, surface: surface)
    }

    // 4. No in-flight work; surface the existing restart alert.
    return LaunchPlan(action: .restartAndOpen,
                      requiresUserConfirm: true,
                      mustRestart: true, mustSave: mustSave,
                      otherHarness: otherHarness, surface: surface)
}

/// Static accessors so call sites outside this file can use the
/// resolver without depending on module-level functions. ``BridgeModel``
/// forwards its state through ``resolve(...)`` and ``mayStop(...)``;
/// Swift test harnesses import the same module and call the same
/// static functions, so the policy under test is the production
/// policy byte-for-byte.
public enum LaunchPlanResolver {
    /// See ``resolveLaunchPlan(_:gatewayRunning:gatewayFingerprint:
    /// gatewayDigest:preparedFingerprint:preparedDigest:chatWorking:
    /// chatHasActiveWork:chatWindowOpen:activeRequests:changeKind:
    /// liveClaude:liveCodex:)`` for parameter documentation.
    public static func resolve(
        surface: String,
        gatewayRunning: Bool,
        gatewayFingerprint: String?,
        gatewayDigest: String?,
        preparedFingerprint: String?,
        preparedDigest: String?,
        chatWorking: Bool,
        chatHasActiveWork: Bool,
        chatWindowOpen: Bool,
        activeRequests: Int,
        changeKind: ChangeKind,
        liveClaude: Bool,
        liveCodex: Bool
    ) -> LaunchPlan {
        return resolveLaunchPlan(
            surface: surface,
            gatewayRunning: gatewayRunning,
            gatewayFingerprint: gatewayFingerprint,
            gatewayDigest: gatewayDigest,
            preparedFingerprint: preparedFingerprint,
            preparedDigest: preparedDigest,
            chatWorking: chatWorking,
            chatHasActiveWork: chatHasActiveWork,
            chatWindowOpen: chatWindowOpen,
            activeRequests: activeRequests,
            changeKind: changeKind,
            liveClaude: liveClaude,
            liveCodex: liveCodex)
    }

    /// See ``mayStopGateway(liveClaude:liveCodex:chatWorking:
    /// chatHasActiveWork:chatWindowOpen:activeRequests:)`` for parameter
    /// documentation.
    public static func mayStop(
        liveClaude: Bool,
        liveCodex: Bool,
        chatWorking: Bool,
        chatHasActiveWork: Bool,
        chatWindowOpen: Bool,
        activeRequests: Int
    ) -> Bool {
        return mayStopGateway(
            liveClaude: liveClaude,
            liveCodex: liveCodex,
            chatWorking: chatWorking,
            chatHasActiveWork: chatHasActiveWork,
            chatWindowOpen: chatWindowOpen,
            activeRequests: activeRequests)
    }

    /// True when the live gateway already has the snapshot the launch
    /// needs. ``nil`` on either side means "do not compare this
    /// dimension". Exposed so test harnesses can exercise the
    /// snapshot comparison directly.
    public static func snapshotsMatch(
        surface: String,
        gatewayRunning: Bool,
        gatewayFingerprint: String?,
        gatewayDigest: String?,
        preparedFingerprint: String?,
        preparedDigest: String?
    ) -> Bool {
        return snapshotsMatchFn(
            surface: surface,
            gatewayRunning: gatewayRunning,
            gatewayFingerprint: gatewayFingerprint,
            gatewayDigest: gatewayDigest,
            preparedFingerprint: preparedFingerprint,
            preparedDigest: preparedDigest)
    }
}

/// True when the live gateway already has the snapshot the launch needs.
/// ``nil`` on either side means "do not compare this dimension".
public func snapshotsMatchFn(
    surface: String,
    gatewayRunning: Bool,
    gatewayFingerprint: String?,
    gatewayDigest: String?,
    preparedFingerprint: String?,
    preparedDigest: String?
) -> Bool {
    guard gatewayRunning else { return false }
    if surface == "claude" {
        // Claude-side launch compares catalogue_fingerprint; codex-side
        // digest is irrelevant here.
        guard let expected = preparedFingerprint else { return true }
        return gatewayFingerprint == expected
    }
    // surface == "codex"
    guard let expected = preparedDigest else { return true }
    return gatewayDigest == expected
}

/// True when no surface (desktop harness or chat) is keeping the
/// gateway alive and no requests are in flight. The restore / auto-stop
/// paths share this predicate so the policy lives in one place.
public func mayStopGateway(
    liveClaude: Bool,
    liveCodex: Bool,
    chatWorking: Bool,
    chatHasActiveWork: Bool,
    chatWindowOpen: Bool,
    activeRequests: Int
) -> Bool {
    let chatBusy = chatWorking || chatHasActiveWork
    let anyOwned = liveClaude || liveCodex || chatWindowOpen || chatBusy
    return !anyOwned && activeRequests == 0
}
