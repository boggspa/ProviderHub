import SwiftUI

/// Each lane's to-do list in the Team inspector pane: the chat's own list when
/// solo, every member's and the shared list in a Team. The worker owns the
/// lists (chat_todos.py) and models write them with todo_write; this section
/// only draws the latest snapshot, which arrives with the chat and after
/// every change.
struct ChatTodosSection: View {
    @ObservedObject var model: ChatModel
    private typealias Semantic = HubTheme.Semantic
    var body: some View {
        let todos = model.todos
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text("To-dos").font(.system(size: 13, weight: .semibold))
                // A solo chat's one list carries its own count.
                if let todos, todos.team, !todos.isEmpty {
                    let lanes = todos.all
                    Text("\(lanes.reduce(0) { $0 + $1.done })/\(lanes.reduce(0) { $0 + $1.total })")
                        .font(.system(size: 10.5)).foregroundStyle(.secondary).help("Completed to-dos across every list")
                }
                Spacer(minLength: 0)
            }
            Text(todos?.team == true
                 ? "Each member keeps its own list, and any member can claim items on the shared list. A member's replacement model resumes from its list."
                 : "The model's plan for this chat. A model you switch to resumes from it.")
                .font(.system(size: 10.5)).foregroundStyle(.secondary)
            if let todos, !todos.isEmpty {
                ForEach(todos.team ? todos.lanes.filter { !$0.items.isEmpty } : todos.lanes) { lane in
                    let route = route(lane)
                    ChatSharedTodoLane(title: todos.team ? lane.name : route?.label ?? "This chat", items: lane.checklist,
                                       done: lane.done, total: lane.total, accent: route?.accent ?? model.activeAccent,
                                       ink: Semantic.ink, secondary: Semantic.secondaryInk, fill: Semantic.cardFill) {
                        ChatProviderIcon(presentation: route?.presentation, size: 12)
                    }
                }
                if let shared = todos.shared, !shared.items.isEmpty {
                    ChatSharedTodoLane(title: shared.name, items: shared.checklist, done: shared.done, total: shared.total,
                                       accent: Semantic.accentOnSurface, ink: Semantic.ink, secondary: Semantic.secondaryInk,
                                       fill: Semantic.cardFill) {
                        Image(systemName: "person.2").font(.system(size: 9.5)).foregroundStyle(Semantic.secondaryInk)
                    }
                }
            } else if model.todosNotice.isEmpty {
                Text("No to-dos yet. Models keep one here for multi-step work.").font(.system(size: 11.5)).foregroundStyle(.secondary)
            }
            if !model.todosNotice.isEmpty {
                Text(model.todosNotice).font(.system(size: 11.5)).foregroundStyle(.secondary).textSelection(.enabled)
            }
        }
        .padding(14)
    }

    private func route(_ lane: ChatTodoSnapshot.Lane) -> ChatRoute? {
        model.models.first { $0.id == lane.choice } ?? model.models.first { $0.route == lane.route }
    }
}

extension ChatTodoSnapshot.Lane {
    var checklist: [ChatTodoItem] {
        items.map { ChatTodoItem(id: $0.id, content: $0.content, status: ChatTodoItem.Status(rawValue: $0.status) ?? .pending,
                                 owner: $0.ownerName ?? "") }
    }
}
