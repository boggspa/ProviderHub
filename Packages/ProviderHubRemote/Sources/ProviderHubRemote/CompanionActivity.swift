import SwiftUI

struct CompanionActivity: Identifiable, Equatable {
    var id: String
    var name: String
    var route: String
    var entryID: String
    var description: ChatActivityDescription

    static func current(entries: [RemoteEntry], members: [RemoteValue], teamEnabled: Bool,
                        busy: Bool, transition: String? = nil, authoritative: RemoteValue = .null,
                        soloName: String = "Assistant", soloRoute: String = "") -> [Self] {
        guard busy else { return [] }
        if case .array(let rows) = authoritative["rows"] {
            return rows.map { row in
                Self(id: row["id"].text, name: row["name"].text, route: row["route"].text, entryID: row["entryID"].text,
                     description: .describe(status: row["status"].text, kind: row["kind"].text,
                        tool: row["tool"].text, summary: row["summary"].text, runningTool: row["runningTool"].flag,
                        streaming: row["streaming"].flag, reason: row["reason"].text, stopping: row["stopping"].flag,
                        transition: transition))
            }
        }
        // Older hosts already send enough of the newest tool row for useful
        // fallback activity. New hosts supply rows even outside the tail.
        if teamEnabled {
            return members.compactMap { member in
                let status = member["status"].text
                guard ["working", "continuing", "waiting"].contains(status) else { return nil }
                let contribution = member["contributionID"].text
                let entry = entries.last {
                    $0.memberID == member["id"].text && (contribution.isEmpty || $0.contributionID == contribution) &&
                    !($0.tool == "react_to_message" && !$0.isError)
                }
                let reason = member["waitReason"].text.isEmpty ? member["nextStep"].text : member["waitReason"].text
                return Self(id: member["id"].text, name: member["name"].text, route: member["route"].text,
                    entryID: entry?.id ?? "", description: .describe(status: status, kind: entry?.role ?? "",
                        tool: entry?.tool, summary: entry?.summary.isEmpty == false ? entry?.summary : entry?.text,
                        runningTool: entry?.detail == "Running…", streaming: entry?.isLive == true && entry?.text.isEmpty == false,
                        reason: reason, stopping: member["memberStoppedByUser"].flag, transition: transition))
            }
        }
        let entry = entries.last { !($0.tool == "react_to_message" && !$0.isError) }
        return [Self(id: "solo-" + soloRoute, name: soloName, route: soloRoute, entryID: entry?.id ?? "",
            description: .describe(status: "working", kind: entry?.role ?? "", tool: entry?.tool,
                summary: entry?.summary.isEmpty == false ? entry?.summary : entry?.text,
                runningTool: entry?.detail == "Running…", streaming: entry?.isLive == true && entry?.text.isEmpty == false,
                transition: transition))]
    }
}

extension CompanionModel {
    /// The chat's own run state once the Mac has bound it to the phone;
    /// until then (just after a tap) the recent-list summary for that chat.
    public var runIsOpen: Bool {
        selectionBound ? store.selectedBusy ?? selectedChat?.busy ?? false : selectedChat?.busy ?? false
    }
    /// Whether the Mac's per-chat state (configuration, status, run) is for
    /// the chat selected on the phone yet.
    var selectionBound: Bool { selectedChatID != nil && store.selectedID == selectedChatID }
    var liveActivities: [CompanionActivity] {
        let transition = store.selectedInterrupting
            ? (["Stopping…", "Interrupting for your update…"].contains(store.selectedStatus) ? store.selectedStatus : "Stopping…") : nil
        return CompanionActivity.current(entries: store.visibleEntries, members: members, teamEnabled: team["enabled"].flag,
            busy: runIsOpen, transition: transition, authoritative: store.controls["activity"] ?? .null,
            soloName: selectedChat?.displayModel ?? "Assistant", soloRoute: selectedChat?.route ?? "")
    }
    func jumpToActivity(_ id: String) { activityJumpID = id; activityJumpToken += 1 }
}

struct CompanionActivityStrip: View {
    @ObservedObject var model: CompanionModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .body) private var scale: CGFloat = 1
    @State private var expanded = false
    var body: some View {
        let rows = model.liveActivities
        if !rows.isEmpty {
            VStack(alignment: .leading, spacing: 3 * scale) {
                if rows.count > 1 {
                    ChatSharedActivitySummary(
                        buckets: ChatActivityBuckets.group(rows.map {
                            ChatActivityBuckets.Row(id: $0.id, name: $0.name, route: $0.route, verb: $0.description.verb, live: $0.description.live)
                        }),
                        expanded: expanded, ink: CompanionTheme.ink, secondary: CompanionTheme.secondary, scale: scale,
                        accent: { CompanionBranding.presentation(route: $0.route, label: $0.name, models: model.models).accent },
                        onToggle: toggleExpanded
                    ) { person in
                        CompanionProviderMark(presentation: CompanionBranding.presentation(route: person.route, label: person.name, models: model.models), size: 13 * scale)
                    }
                }
                if rows.count == 1 || expanded {
                    ForEach(rows) { row in
                        let brand = CompanionBranding.presentation(route: row.route, label: row.name, models: model.models)
                        Button { model.jumpToActivity(row.entryID) } label: {
                            ChatSharedActivityLabel(name: row.name, verb: row.description.verb, subject: row.description.subject,
                                code: row.description.code, live: row.description.live, accent: brand.accent,
                                ink: CompanionTheme.ink, secondary: CompanionTheme.secondary, scale: scale) {
                                    CompanionProviderMark(presentation: brand, size: 13 * scale)
                                }
                        }.buttonStyle(.plain).disabled(row.entryID.isEmpty)
                            .accessibilityLabel(row.name + " " + row.description.verb + (row.description.subject.isEmpty ? "" : " " + row.description.subject))
                    }
                }
            }.padding(.horizontal, 20).padding(.vertical, 6 * scale)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityElement(children: .contain).accessibilityLabel("Live activity")
        }
    }

    private func toggleExpanded() {
        if reduceMotion { expanded.toggle() } else { withAnimation(.easeInOut(duration: 0.16)) { expanded.toggle() } }
    }
}
