import SwiftUI

/// The Mac's to-do snapshot (its "todos" control panel) as the lanes the
/// shared views draw: the chat's own list when solo, every member's and the
/// shared list in a Team. A host without to-dos never sends the panel, so the
/// section stays hidden there and no protocol version is needed.
struct CompanionTodoLists: Equatable {
    struct Lane: Equatable, Identifiable {
        var id: String
        var name: String
        var route: String
        var items: [ChatTodoItem]
        var done: Int
        var total: Int
    }
    var team: Bool
    var lanes: [Lane]
    var shared: Lane?
    var all: [Lane] { lanes + (shared.map { [$0] } ?? []) }
    var isEmpty: Bool { all.allSatisfy { $0.items.isEmpty } }

    init?(_ value: RemoteValue?) {
        guard let value, case .object = value else { return nil }
        team = value["team"].flag
        lanes = value["lanes"].rows.map(Self.lane)
        shared = value["shared"].object.isEmpty ? nil : Self.lane(value["shared"])
    }

    static func lane(_ row: RemoteValue) -> Lane {
        let items = row["items"].rows.compactMap { item -> ChatTodoItem? in
            guard let status = ChatTodoItem.Status(rawValue: item["status"].text), !item["content"].text.isEmpty else { return nil }
            return ChatTodoItem(id: item["id"].text, content: item["content"].text, status: status, owner: item["ownerName"].text)
        }
        // The host's counts cover the whole list; a long one may arrive shortened.
        let counted = ChatTodoChecklist.progress(items)
        return Lane(id: row["id"].text.isEmpty ? "shared" : row["id"].text, name: row["name"].text, route: row["route"].text,
                    items: items, done: row["done"].optionalInt ?? counted.done, total: max(row["total"].optionalInt ?? 0, counted.total))
    }
}

struct CompanionTodos: View {
    @ObservedObject var model: CompanionModel
    var lists: CompanionTodoLists
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider()
            HStack(spacing: 5) {
                Text("To-dos").font(.system(size: 12, weight: .semibold))
                if lists.team, !lists.isEmpty {
                    Text("\(lists.all.reduce(0) { $0 + $1.done })/\(lists.all.reduce(0) { $0 + $1.total })")
                        .font(.system(size: 10.5)).foregroundStyle(CompanionTheme.secondary)
                }
                Spacer()
            }
            Text(lists.team ? "Each member keeps its own list, and any member can claim items on the shared list."
                            : "The model's plan for this chat. A model you switch to resumes from it.")
                .font(.system(size: 10.5)).foregroundStyle(CompanionTheme.secondary)
            if lists.isEmpty {
                Text("No to-dos yet.").font(.system(size: 11.5)).foregroundStyle(CompanionTheme.secondary)
            } else {
                ForEach(lists.team ? lists.lanes.filter { !$0.items.isEmpty } : lists.lanes) { lane in
                    let brand = CompanionBranding.presentation(route: lane.route, models: model.models)
                    ChatSharedTodoLane(title: lists.team ? lane.name : brand.label, items: lane.items, done: lane.done, total: lane.total,
                                       accent: brand.accent, ink: CompanionTheme.ink, secondary: CompanionTheme.secondary,
                                       fill: CompanionTheme.cardFill) {
                        CompanionProviderMark(presentation: brand, size: 12)
                    }
                }
                if let shared = lists.shared, !shared.items.isEmpty {
                    ChatSharedTodoLane(title: shared.name.isEmpty ? "Shared" : shared.name, items: shared.items, done: shared.done,
                                       total: shared.total, accent: CompanionTheme.brand, ink: CompanionTheme.ink,
                                       secondary: CompanionTheme.secondary, fill: CompanionTheme.cardFill) {
                        Image(systemName: "person.2").font(.system(size: 9.5)).foregroundStyle(CompanionTheme.secondary)
                    }
                }
            }
        }
    }
}
