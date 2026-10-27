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

/// One member inside a collapsed activity count. The icon is chosen by route;
/// the name stays in the accessible label so identical provider marks do not
/// erase who is in the group.
struct ChatActivityParticipant: Equatable, Identifiable {
    var id: String
    var name: String
    var route: String
}

/// Live rows that share a state, in the order the rows were given (roster order).
struct ChatActivityBucket: Equatable, Identifiable {
    var title: String
    var participants: [ChatActivityParticipant]
    var live: Bool
    var id: String { title }
    var count: Int { participants.count }

    var accessibilityLabel: String {
        let names = participants.map(\.name).filter { !$0.isEmpty }.joined(separator: ", ")
        return names.isEmpty ? "\(count) \(title)" : "\(count) \(title), \(names)"
    }
}

/// Counts for the collapsed strip. A shell is "Running a command"; every other
/// tool keeps its own live verb, so an edit or a read is not a command.
enum ChatActivityBuckets {
    struct Row: Equatable {
        var id: String
        var name: String
        var route: String
        var verb: String
        var live: Bool
    }

    static func group(_ rows: [Row]) -> [ChatActivityBucket] {
        var titles: [String] = []
        var grouped: [String: (live: Bool, people: [ChatActivityParticipant])] = [:]
        for row in rows {
            let title = title(for: row.verb)
            var current = grouped[title] ?? (live: false, people: [])
            if grouped[title] == nil { titles.append(title) }
            current.live = current.live || row.live
            current.people.append(ChatActivityParticipant(id: row.id, name: row.name, route: row.route))
            grouped[title] = current
        }
        return titles.compactMap { title in
            grouped[title].map { ChatActivityBucket(title: title, participants: $0.people, live: $0.live) }
        }
    }

    /// "Running" is only the live shell phrasing from `ChatToolDisplay`.
    static func title(for verb: String) -> String {
        switch verb {
        case "Running", "Ran", "Running a command", "Ran a command":
            return "Running a command"
        case "Interrupting for your update…":
            return "Interrupting for your update"
        default:
            return verb.hasSuffix("…") ? String(verb.dropLast()) : verb
        }
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

/// The collapsed team row: a count per state, and one provider icon per member
/// in that state. Narrow widths scroll the chips instead of dropping a member.
struct ChatSharedActivitySummary<Mark: View>: View {
    var buckets: [ChatActivityBucket]
    var expanded: Bool
    var ink: Color
    var secondary: Color
    var scale: CGFloat = 1
    var accent: (ChatActivityParticipant) -> Color
    var onToggle: () -> Void
    @ViewBuilder var mark: (ChatActivityParticipant) -> Mark

    var body: some View {
        HStack(alignment: .center, spacing: 4 * scale) {
            Button(action: onToggle) {
                Image(systemName: expanded ? "chevron.down" : "chevron.right")
                    .font(.system(size: 9 * scale, weight: .bold))
                    .foregroundStyle(secondary)
                    .frame(width: 28 * scale, height: 28 * scale)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(expanded ? "Hide individual activity" : "Show individual activity")
            ViewThatFits(in: .horizontal) {
                chips
                ScrollView(.horizontal, showsIndicators: false) { chips }
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var chips: some View {
        HStack(spacing: 10 * scale) {
            ForEach(buckets) { bucket in
                Button(action: onToggle) {
                    HStack(spacing: 4 * scale) {
                        HStack(spacing: 2 * scale) {
                            ForEach(bucket.participants) { person in
                                mark(person).accessibilityHidden(true)
                            }
                        }
                        ChatSharedShimmerText(text: "\(bucket.count) \(bucket.title)", active: bucket.live,
                            accent: bucket.participants.first.map(accent) ?? ink, base: secondary)
                            .font(.system(size: 11.5 * scale, weight: .medium))
                            .lineLimit(1)
                            .fixedSize(horizontal: true, vertical: false)
                    }
                }
                .buttonStyle(.plain)
                .accessibilityLabel(bucket.accessibilityLabel)
            }
        }
    }
}
