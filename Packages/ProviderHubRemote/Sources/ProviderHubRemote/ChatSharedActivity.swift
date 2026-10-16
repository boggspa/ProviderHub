import SwiftUI

/// The desktop's live wording, driven only by the newest row in the current
/// contribution. Historical tool calls never keep a member looking active.
struct ChatActivityDescription: Equatable {
    var verb: String
    var subject = ""
    var code = false
    var live = true

    static func describe(status: String, kind: String = "", tool: String? = nil, summary: String? = nil,
                         runningTool: Bool = false, streaming: Bool = false, reason: String = "",
                         stopping: Bool = false, transition: String? = nil) -> Self {
        if let transition, !transition.isEmpty { return Self(verb: transition) }
        if stopping { return Self(verb: "Stopping…") }
        if status == "waiting" { return Self(verb: "Waiting", subject: reason, live: false) }
        if status == "continuing" { return Self(verb: "Continuing…") }
        if kind == "tool", runningTool {
            let display = ChatToolDisplay.describe(tool: tool, summary: summary, live: true)
            return Self(verb: display.verb, subject: display.subject, code: display.code)
        }
        if kind == "assistant", streaming { return Self(verb: "Replying…") }
        return Self(verb: "Thinking…")
    }
}

/// Shared with the Mac's shimmer: it updates just its own label, respects
/// Reduce Motion, and does not make the transcript redraw on every frame.
struct ChatSharedShimmerText: View {
    var text: String
    var active: Bool
    var accent: Color
    var base: Color = .secondary
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var body: some View {
        if active, !reduceMotion {
            TimelineView(.animation(minimumInterval: 1 / 30)) { context in
                let phase = context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 1.6) / 1.6
                Text(text).foregroundStyle(base)
                    .overlay {
                        GeometryReader { geometry in
                            let width = geometry.size.width
                            LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: accent, location: 0.5), .init(color: .clear, location: 1)],
                                           startPoint: .leading, endPoint: .trailing)
                                .frame(width: width * 0.7).offset(x: -width * 0.7 + width * 1.7 * phase)
                        }
                    }.mask(Text(text)).accessibilityLabel(text)
            }
        } else { Text(text).foregroundStyle(active ? accent : base) }
    }
}

struct ChatSharedActivityLabel<Mark: View>: View {
    var name: String
    var verb: String
    var subject: String
    var code: Bool
    var live: Bool
    var accent: Color
    var ink: Color
    var secondary: Color
    var scale: CGFloat = 1
    @ViewBuilder var mark: () -> Mark
    var body: some View {
        HStack(spacing: 6 * scale) {
            mark()
            Text(name).font(.system(size: 11.5 * scale, weight: .semibold)).foregroundStyle(ink).lineLimit(1).fixedSize()
            ChatSharedShimmerText(text: verb, active: live, accent: accent, base: secondary)
                .font(.system(size: 11.5 * scale, weight: .medium)).fixedSize()
            if !subject.isEmpty {
                Text(subject).font(.system(size: (code ? 11 : 11.5) * scale, design: code ? .monospaced : .default))
                    .foregroundStyle(secondary).lineLimit(1).truncationMode(.middle)
            }
            Spacer(minLength: 0)
        }.contentShape(Rectangle())
    }
}
