import SwiftUI

// A live activity strip between the transcript and the composer: one line per
// speaker that is working right now, in the transcript's own words ("Running
// swift build -c release", "Editing main.swift", "Thinking…"). In a Team the
// members' tool rows interleave and scroll past quickly, so a whole page can
// go by while one member is still mid-command with no reply yet; the strip
// keeps every live member in view and a click jumps to their current row.
// Nothing here is invented status: each line reads the member's newest row.

private typealias Semantic = HubTheme.Semantic

extension ChatModel {
    /// Scroll the transcript to an entry's row (or the fold holding it).
    func jump(to entryID: String) {
        jumpRequest = ChatJumpRequest(id: entryID, token: (jumpRequest?.token ?? 0) + 1)
    }
}

/// What one speaker is doing right now, read from their newest row.
struct ChatActivity: Identifiable, Equatable {
    var id: String
    var name: String
    var route: String
    var verb: String
    var subject = ""
    var code = false
    /// The row to jump to; nil while the speaker has no row of its own yet.
    var entryID: String?
    /// Shimmers while something is actually running; a wait sits still.
    var live = true

    init(id: String, name: String, route: String, verb: String, subject: String = "", code: Bool = false,
         entryID: String? = nil, live: Bool = true) {
        self.id = id; self.name = name; self.route = route; self.verb = verb
        self.subject = subject; self.code = code; self.entryID = entryID; self.live = live
    }

    /// A working speaker described by its newest row: a running tool reads
    /// as that tool's live phrasing, a streaming reply as "Replying…", and
    /// anything else (a model request in flight) as "Thinking…".
    init(id: String, name: String, route: String, entry: ChatEntry?, streaming: Bool) {
        self.id = id; self.name = name; self.route = route
        self.entryID = entry?.id
        if let entry, entry.kind == "tool", entry.detail == "Running…" {
            let display = ChatToolDisplay.describe(tool: entry.tool, summary: entry.summary ?? entry.text, live: true)
            verb = display.verb; subject = display.subject; code = display.code
        } else if let entry, entry.kind == "assistant", streaming, !ChatReplySources.isBlank(entry.text) {
            verb = "Replying…"
        } else {
            verb = "Thinking…"
        }
    }

    /// The strip's rows for the current state, in Team roster order; a solo
    /// chat has one row for its model. Empty while nothing runs. While Stop
    /// or an update is in flight, `transition` is the turn's own status
    /// ("Stopping…", "Interrupting for your update…"): the newest rows still
    /// show the work being cancelled, so each line says that instead.
    static func current(entries: [ChatEntry], team: ChatTeamSnapshot?, busy: Bool, liveEntryIDs: Set<String>,
                        transition: String? = nil, label: (String) -> String) -> [ChatActivity] {
        guard busy else { return [] }
        let rows = speakers(entries: entries, team: team, liveEntryIDs: liveEntryIDs, label: label)
        guard let transition, !transition.isEmpty else { return rows }
        return rows.map { row in
            var row = row
            row.verb = transition; row.subject = ""; row.code = false; row.live = true
            return row
        }
    }

    private static func speakers(entries: [ChatEntry], team: ChatTeamSnapshot?, liveEntryIDs: Set<String>,
                                 label: (String) -> String) -> [ChatActivity] {
        if let team, team.enabled {
            return team.members.compactMap { member in
                let latest = entries.last { $0.memberID == member.id && $0.contributionID == member.contributionID }
                switch member.status {
                case "working":
                    if member.memberStoppedByUser == true {
                        return ChatActivity(id: member.id, name: member.name, route: member.route, verb: "Stopping…", entryID: latest?.id)
                    }
                    return ChatActivity(id: member.id, name: member.name, route: member.route, entry: latest,
                                        streaming: latest.map { liveEntryIDs.contains($0.id) } ?? false)
                case "continuing":
                    return ChatActivity(id: member.id, name: member.name, route: member.route, verb: "Continuing…", entryID: latest?.id)
                case "waiting":
                    let reason = member.waitReason?.isEmpty == false ? member.waitReason! : member.nextStep
                    return ChatActivity(id: member.id, name: member.name, route: member.route, verb: "Waiting",
                                        subject: reason, entryID: latest?.id, live: false)
                default:
                    return nil
                }
            }
        }
        let last = entries.last
        let route = last?.route ?? ""
        return [ChatActivity(id: "solo-" + route, name: label(route), route: route, entry: last,
                             streaming: last.map { liveEntryIDs.contains($0.id) } ?? false)]
    }
}

struct ChatActivityStrip: View {
    @ObservedObject var model: ChatModel
    @Environment(\.chatTextStyle) private var textStyle

    var body: some View {
        let rows = ChatActivity.current(entries: model.entries, team: model.team, busy: model.busy,
                                        liveEntryIDs: model.streamingEntryIDs(),
                                        transition: transition, label: { model.label(for: $0) })
        if !rows.isEmpty {
            VStack(alignment: .leading, spacing: textStyle.scaled(3)) {
                ForEach(rows) { row in
                    activityRow(row)
                }
            }
            // No panel or rule of its own: the lines float on the pane between
            // the transcript and the composer.
            .padding(.horizontal, 20).padding(.vertical, textStyle.scaled(6))
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Live activity")
        }
    }

    /// The turn's status while Stop or an update is in flight. A reconnect
    /// snapshot can carry a raw tool summary there, so anything else reads
    /// as "Stopping…".
    private var transition: String? {
        guard model.interrupting else { return nil }
        return ["Stopping…", "Interrupting for your update…"].contains(model.status) ? model.status : "Stopping…"
    }

    private func activityRow(_ row: ChatActivity) -> some View {
        Button {
            if let entryID = row.entryID { model.jump(to: entryID) }
        } label: {
            activityLabel(row)
        }
        .buttonStyle(.plain)
        .disabled(row.entryID == nil)
        .help(row.entryID == nil ? "" : "Show " + row.name + "’s current row")
        .accessibilityLabel(row.name + " " + row.verb + (row.subject.isEmpty ? "" : " " + row.subject))
    }

    private func activityLabel(_ row: ChatActivity) -> some View {
        HStack(spacing: textStyle.scaled(6)) {
            ChatProviderIcon(presentation: model.route(named: row.route)?.presentation, size: textStyle.scaled(13))
            Text(row.name).font(textStyle.system(11.5, weight: .semibold)).foregroundStyle(Semantic.ink)
                .lineLimit(1).fixedSize()
            ChatShimmerText(text: row.verb, active: row.live, accent: model.accent(for: row.route))
                .font(textStyle.system(11.5, weight: .medium)).fixedSize()
            if !row.subject.isEmpty {
                Text(row.subject)
                    .font(row.code ? textStyle.system(11, design: .monospaced) : textStyle.system(11.5))
                    .foregroundStyle(Semantic.secondaryInk).lineLimit(1).truncationMode(.middle)
            }
            Spacer(minLength: 0)
        }
        .contentShape(Rectangle())
    }
}
