import AppKit
import SwiftUI

// State-driven accents for the Chat harness: the approval mode's colour, a
// context ring, a provider-hued working shimmer and patch line counts. Every
// colour is a dynamic HubTheme.Semantic token or the route accent ChatModel
// already resolves, so nothing here keeps a palette of its own.

/// Approval mode is state the user should read at a glance: Manual asks first
/// (blue), Accept Edits sits in between (neutral), YOLO runs tools without
/// prompts (red). Unknown modes read as Manual, the runtime's default.
enum ChatApprovalAccent {
    static func color(_ mode: String) -> Color {
        switch mode {
        case "yolo": return HubTheme.Semantic.approvalYolo
        case "accept_edits": return HubTheme.Semantic.approvalAcceptEdits
        default: return HubTheme.Semantic.approvalManual
        }
    }
}

/// Folders whose YOLO choice has been confirmed once. Keyed by the resolved
/// path so aliases of one folder share the answer; stored with the other Chat
/// preferences and never sent to the worker.
enum ChatYoloAcknowledgement {
    static let defaultsKey = "chatYoloAcknowledgedWorkspaces"
    static func contains(_ workspace: String) -> Bool {
        (UserDefaults.standard.stringArray(forKey: defaultsKey) ?? []).contains(ChatWorkspaceGroup.identity(workspace))
    }
    static func record(_ workspace: String) {
        var list = UserDefaults.standard.stringArray(forKey: defaultsKey) ?? []
        let key = ChatWorkspaceGroup.identity(workspace)
        guard !list.contains(key) else { return }
        list.append(key); UserDefaults.standard.set(list, forKey: defaultsKey)
    }
}

/// Context used, as a 12pt ring with a 2pt stroke beside the token readout.
/// It wears the provider accent until pressure builds, then amber from 80%
/// and red from 95%, the thresholds TaskWraith's context wheel uses. Callers
/// hide it when the limit is unknown; a ring with no ceiling is decoration.
struct ChatContextRing: View {
    var used: Int
    var limit: Int
    var accent: Color
    var size: CGFloat = 12

    static func fraction(used: Int, limit: Int) -> Double {
        guard limit > 0 else { return 0 }
        return min(1, max(0, Double(used) / Double(limit)))
    }
    static func tint(fraction: Double, accent: Color) -> Color {
        fraction >= 0.95 ? HubTheme.Semantic.contextCritical : fraction >= 0.8 ? HubTheme.Semantic.contextWarn : accent
    }

    var body: some View {
        let fraction = Self.fraction(used: used, limit: limit)
        let tint = Self.tint(fraction: fraction, accent: accent)
        ZStack {
            Circle().stroke(tint.opacity(0.22), lineWidth: 2)
            Circle().trim(from: 0, to: fraction)
                .stroke(tint, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                .rotationEffect(.degrees(-90))
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

/// A status word that sweeps the provider hue through its glyphs while work
/// is active, the way Codex Desktop and TaskWraith mark "Working". Reduce
/// Motion gets the static accent instead, and idle text keeps the caller's
/// base colour. The sweep is one small TimelineView scoped to this text; the
/// transcript and model loop never redraw for it.
struct ChatShimmerText: View {
    var text: String
    var active: Bool
    var accent: Color
    var base: Color = HubTheme.Semantic.secondaryInk
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private static let period: TimeInterval = 1.6

    var body: some View {
        if active, !reduceMotion {
            TimelineView(.animation(minimumInterval: 1 / 30)) { context in
                sweep(phase: context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: Self.period) / Self.period)
            }
        } else {
            Text(text).foregroundStyle(active ? accent : base)
        }
    }

    private func sweep(phase: Double) -> some View {
        Text(text).foregroundStyle(base)
            .overlay {
                GeometryReader { geometry in
                    let width = geometry.size.width
                    LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: accent, location: 0.5), .init(color: .clear, location: 1)],
                                   startPoint: .leading, endPoint: .trailing)
                        .frame(width: width * 0.7)
                        .offset(x: -width * 0.7 + width * 1.7 * phase)
                }
            }
            .mask(Text(text))
            .accessibilityLabel(text)
    }
}

/// Added and removed line counts read from a recorded patch. Only text that
/// reads as a patch counts, and its headers never do; anything else is nil
/// so a row shows no numbers rather than wrong ones.
struct ChatPatchStats: Equatable {
    var added = 0
    var removed = 0

    static func count(_ text: String) -> ChatPatchStats? {
        var stats = ChatPatchStats()
        var isPatch = false
        for line in text.split(separator: "\n", omittingEmptySubsequences: false) {
            if line.hasPrefix("@@") || line.hasPrefix("*** ") || line.hasPrefix("+++ ") || line.hasPrefix("--- ") { isPatch = true; continue }
            if line.hasPrefix("+") { stats.added += 1 } else if line.hasPrefix("-") { stats.removed += 1 }
        }
        return isPatch && stats.added + stats.removed > 0 ? stats : nil
    }

    static func + (lhs: ChatPatchStats, rhs: ChatPatchStats) -> ChatPatchStats {
        ChatPatchStats(added: lhs.added + rhs.added, removed: lhs.removed + rhs.removed)
    }
}

/// `+12 −3` in the Git indicator's colours and monospace, so a patch row and
/// the header count the same way.
struct ChatPatchStatsLabel: View {
    var stats: ChatPatchStats
    var size: CGFloat = 11
    var body: some View {
        HStack(spacing: 4) {
            Text("+\(stats.added)").foregroundStyle(Color(nsColor: .systemGreen))
            Text("−\(stats.removed)").foregroundStyle(Color(nsColor: .systemRed))
        }
        .font(.system(size: size, weight: .medium, design: .monospaced)).fixedSize()
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(stats.added) lines added, \(stats.removed) removed")
    }
}
