import AppKit
import SwiftUI

@MainActor
final class ChatWorkspaceController: ObservableObject {
    @Published var layout: ChatWorkspaceLayout {
        didSet { if let data = try? layout.save() { UserDefaults.standard.set(data, forKey: Self.defaultsKey) } }
    }
    @Published var focusedOnly = false
    /// An explicit keyboard command; ordinary focus and streamed updates never move the caret.
    @Published var keyboardFocusRequest: (pane: UUID, token: UUID)?
    @Published var notice = ""
    var area = CGSize.zero
    private var restored = false
    private var hasSavedLayout = false
    private var restoringChat: String?
    private static let defaultsKey = "chatWorkspaceLayoutV1"
    init() {
        let saved = UserDefaults.standard.data(forKey: Self.defaultsKey).flatMap { try? ChatWorkspaceLayout.load(from: $0) }
        layout = saved ?? .initial()
        hasSavedLayout = saved != nil
    }

    var chatIDs: Set<String> { Set(layout.panes.compactMap { if case .chat(let id) = $0.assignment { return id }; return nil }) }
    func browserPane(for chat: String) -> UUID? {
        layout.panes.first { if case .browser(let id) = $0.assignment { return id == chat }; return false }?.id
    }
    func focus(_ pane: UUID, model: ChatModel) {
        layout.focus(paneID: pane)
        let assignment = layout.panes.first { $0.id == pane }?.assignment
        let chat: String?
        switch assignment { case .chat(let id), .browser(let id): chat = id; case nil: chat = nil }
        if case .chat(let id) = assignment { model.setPaneFocus(id) } else { model.setPaneFocus(nil) }
        if let chat, model.selectedID != chat { model.select(chat) }
    }
    func selected(_ chat: String?, model: ChatModel) {
        guard restored else { return }
        if let restoringChat {
            guard chat == restoringChat else { return }
            self.restoringChat = nil
        }
        guard let chat else { return }
        if let existing = layout.panes.first(where: { $0.assignment == .chat(id: chat) }) {
            if browserPane(for: chat) != layout.focusedPaneID { layout.focus(paneID: existing.id) }
        } else if browserPane(for: chat) != layout.focusedPaneID {
            layout.assignChat(paneID: layout.focusedPaneID, chatID: chat)
        }
        model.observe(chats: chatIDs)
        let current = layout.panes.first { $0.id == layout.focusedPaneID }?.assignment
        if case .chat(let id) = current { model.setPaneFocus(id) } else { model.setPaneFocus(nil) }
    }
    func reconcile(_ model: ChatModel) {
        let ids = Set(model.chats.map(\.id))
        func trim(_ node: ChatWorkspaceNode) -> ChatWorkspaceNode {
            switch node {
            case .leaf(var pane):
                switch pane.assignment {
                case .chat(let id), .browser(let id): if !ids.contains(id) { pane.assignment = nil }
                case nil: break
                }
                return .leaf(pane)
            case .split(var split): split.first = trim(split.first); split.second = trim(split.second); return .split(split)
            }
        }
        layout.root = trim(layout.root)
        if !restored, model.connected {
            restored = true
            if hasSavedLayout {
                let saved = layout.panes.first { $0.id == layout.focusedPaneID }?.assignment
                switch saved {
                case .chat(let id), .browser(let id):
                    restoringChat = model.selectedID == id ? nil : id
                    focus(layout.focusedPaneID, model: model)
                case nil:
                    model.setPaneFocus(nil)
                }
            } else {
                selected(model.selectedID, model: model)
            }
        }
        model.observe(chats: chatIDs)
    }

    func paneSize(_ pane: UUID, node: ChatWorkspaceNode? = nil, size: CGSize? = nil) -> CGSize? {
        let size = size ?? area
        switch node ?? layout.root {
        case .leaf(let value): return value.id == pane ? size : nil
        case .split(let split):
            let ratio = CGFloat(ChatWorkspaceSplit.clampRatio(split.ratio))
            let available = max(0, (split.axis == .horizontal ? size.width : size.height) - 8)
            let first = split.axis == .horizontal ? CGSize(width: available * ratio, height: size.height) : CGSize(width: size.width, height: available * ratio)
            let second = split.axis == .horizontal ? CGSize(width: available * (1-ratio), height: size.height) : CGSize(width: size.width, height: available * (1-ratio))
            return paneSize(pane, node: split.first, size: first) ?? paneSize(pane, node: split.second, size: second)
        }
    }
    func canSplit(_ zone: ChatPaneDropZone, pane: UUID) -> Bool {
        guard !focusedOnly, let size = paneSize(pane) else { return false }
        switch zone {
        case .left, .right: return size.width >= 2 * 420 + 8
        case .top, .bottom: return size.height >= 2 * 300 + 8
        case .center: return true
        }
    }
    func split(_ zone: ChatPaneDropZone, pane: UUID, assignment: ChatPaneAssignment? = nil) {
        guard canSplit(zone, pane: pane) else { notice = "Enlarge the window or close a pane to make room."; return }
        notice = ""
        let edge: ChatWorkspaceEdge
        switch zone { case .left: edge = .left; case .top: edge = .top; case .bottom: edge = .bottom; case .right, .center: edge = .right }
        layout.split(pane, edge: edge, assignment: assignment)
    }
    /// The sidebar's Open in Split View: beside the focused pane, on the edge
    /// asked for when it has room and on the other axis otherwise. A chat
    /// already on screen is focused where it is, and an empty focused pane
    /// takes the chat rather than gaining an empty neighbour.
    func open(chat: String, edge: ChatWorkspaceEdge, model: ChatModel) {
        guard model.chats.contains(where: { $0.id == chat }) else { return }
        if let existing = layout.pane(showing: chat) { focusedOnly = false; focus(existing, model: model); return }
        if focusedOnly { focusedOnly = false }
        let pane = layout.focusedPaneID
        if case .some(nil) = layout.assignment(for: pane) {
            layout.assignChat(paneID: pane, chatID: chat)
            focus(pane, model: model); model.observe(chats: chatIDs); return
        }
        let asked: ChatPaneDropZone = edge == .bottom || edge == .top ? .bottom : .right
        guard let zone = [asked, asked == .right ? .bottom : .right].first(where: { canSplit($0, pane: pane) }) else {
            notice = "Enlarge the window or close a pane to make room."; return
        }
        split(zone, pane: pane, assignment: .chat(id: chat))
        focus(layout.focusedPaneID, model: model)
        model.observe(chats: chatIDs)
    }
    /// Move focus to the next or previous pane in reading order.
    func focusAdjacent(_ step: Int, model: ChatModel) {
        guard layout.panes.count > 1 else { return }
        let pane = layout.adjacentPane(step)
        focus(pane, model: model)
        keyboardFocusRequest = (pane, UUID())
    }
    func drop(chat: String, pane: UUID, zone: ChatPaneDropZone, model: ChatModel) {
        guard model.chats.contains(where: { $0.id == chat }) else { return }
        if let existing = layout.panes.first(where: { $0.assignment == .chat(id: chat) }) {
            focus(existing.id, model: model); return
        }
        if zone == .center { layout.assignChat(paneID: pane, chatID: chat); layout.focus(paneID: pane) }
        else { split(zone, pane: pane, assignment: .chat(id: chat)) }
        focus(layout.focusedPaneID, model: model)
        model.observe(chats: chatIDs)
    }
    func close(_ pane: UUID, model: ChatModel) {
        layout.close(paneID: pane); focusedOnly = false
        focus(layout.focusedPaneID, model: model); model.observe(chats: chatIDs)
    }
}

struct ChatWorkspaceContent: View {
    private typealias Semantic = HubTheme.Semantic
    @ObservedObject var model: ChatModel
    @ObservedObject var workspace: ChatWorkspaceController
    @ObservedObject var browser: ChatBrowserStore
    var railVisible: Bool
    var railAvailable: Bool
    var toggleRail: () -> Void

    var body: some View {
        GeometryReader { proxy in
            VStack(spacing: 0) {
                ChatSplitWorkspace(layout: $workspace.layout,
                    focusedPaneID: Binding(get: { workspace.layout.focusedPaneID }, set: { if let id = $0 { workspace.focus(id, model: model) } }),
                    leafContent: { id, assignment in leaf(id, assignment) },
                    onFocus: { workspace.focus($0, model: model) },
                    onDropChat: { pane, zone, chat in workspace.drop(chat: chat, pane: pane, zone: zone, model: model) },
                    focusedOnly: workspace.focusedOnly, keyboardFocusRequest: workspace.keyboardFocusRequest)
                if !workspace.notice.isEmpty {
                    Text(workspace.notice).font(.caption).foregroundStyle(.secondary).padding(5)
                }
            }
            .onAppear { workspace.area = proxy.size; registerCommands() }
            .onChange(of: proxy.size) { _, size in workspace.area = size }
        }
        .onChange(of: workspace.chatIDs) { _, ids in model.observe(chats: ids) }
        .onChange(of: model.selectedID) { _, id in workspace.selected(id, model: model) }
        .onChange(of: model.chats.map(\.id)) { _, _ in workspace.reconcile(model) }
        .onChange(of: model.connected) { _, connected in if connected { workspace.reconcile(model) } }
    }

    @ViewBuilder private func leaf(_ id: UUID, _ assignment: ChatPaneAssignment?) -> some View {
        VStack(spacing: 0) {
            if workspace.layout.panes.count > 1 || workspace.focusedOnly { paneHeader(id, assignment) }
            switch assignment {
            case .chat(let chat):
                GeometryReader { size in
                    ChatPane(model: model.paneModel(for: chat), railVisible: railVisible, railAvailable: railAvailable,
                             compactHeader: size.size.width < 860, toggleRail: toggleRail)
                }
            case .browser(let chat): ChatBrowserPane(chatID: chat, store: browser)
            case nil:
                if model.chats.isEmpty && workspace.layout.panes.count == 1 {
                    ChatPane(model: model, railVisible: railVisible, railAvailable: railAvailable,
                             compactHeader: true, toggleRail: toggleRail)
                } else { picker(id) }
            }
        }.frame(maxWidth: .infinity, maxHeight: .infinity)
    }
    /// Each pane names what it shows, with a menu of what can be done to the
    /// pane itself; the focused pane's name is in full ink, the others recede.
    private func paneHeader(_ id: UUID, _ assignment: ChatPaneAssignment?) -> some View {
        let focused = id == workspace.layout.focusedPaneID
        let chat: ChatSummary? = { if case .chat(let chat) = assignment { return model.chats.first { $0.id == chat } }; return nil }()
        let route = chat.flatMap { chat in model.models.first { $0.route == chat.route && $0.account == chat.account } }
        return HStack(spacing: 6) {
            Menu {
                Button("Split Right") { workspace.focus(id, model: model); workspace.split(.right, pane: id); model.setPaneFocus(nil) }
                    .disabled(!workspace.canSplit(.right, pane: id))
                Button("Split Below") { workspace.focus(id, model: model); workspace.split(.bottom, pane: id); model.setPaneFocus(nil) }
                    .disabled(!workspace.canSplit(.bottom, pane: id))
                Divider()
                Button(workspace.focusedOnly ? "Show All Panes" : "Maximize Pane") {
                    workspace.focus(id, model: model); workspace.focusedOnly.toggle()
                }
                Button("Close Pane") { workspace.close(id, model: model) }
            } label: {
                HStack(spacing: 6) {
                    if assignment != nil {
                        if case .browser = assignment { Image(systemName: "globe").font(.system(size: 11)) }
                        else { ChatProviderIcon(presentation: route?.presentation, size: 13) }
                    }
                    Text(paneTitle(assignment)).font(.system(size: 12, weight: .semibold)).lineLimit(1).truncationMode(.tail)
                    Image(systemName: "chevron.down").font(.system(size: 8, weight: .semibold)).foregroundStyle(Semantic.secondaryInk)
                }
                .foregroundStyle(focused ? Semantic.ink : Semantic.secondaryInk)
            }
            // A plain button-style menu keeps the label as drawn, so the
            // unfocused pane's name can recede; a borderless one restyles it.
            .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden).fixedSize(horizontal: false, vertical: true)
            .accessibilityLabel("Pane: " + paneTitle(assignment))
            .help("Pane actions")
            if let chat, model.isActive(chat.id) {
                ProgressView().controlSize(.mini).tint(route?.accent ?? Semantic.secondaryInk).accessibilityLabel("Working")
            }
            Spacer(minLength: 4)
            Button { workspace.focus(id, model: model); workspace.focusedOnly.toggle() } label: {
                Image(systemName: workspace.focusedOnly ? "arrow.down.right.and.arrow.up.left" : "arrow.up.left.and.arrow.down.right")
                    .font(.system(size: 11)).frame(width: 22, height: 22).contentShape(Rectangle())
            }.buttonStyle(.plain).foregroundStyle(Semantic.secondaryInk)
                .help(workspace.focusedOnly ? "Show all panes (⌃⌥⌘F)" : "Maximize pane (⌃⌥⌘F)")
                .accessibilityLabel(workspace.focusedOnly ? "Show all panes" : "Maximize pane")
            Button { workspace.close(id, model: model) } label: {
                Image(systemName: "xmark").font(.system(size: 11)).frame(width: 22, height: 22).contentShape(Rectangle())
            }.buttonStyle(.plain).foregroundStyle(Semantic.secondaryInk)
                .help("Close pane (⌥⌘W)").accessibilityLabel("Close pane")
        }
        .padding(.leading, 12).padding(.trailing, 8).frame(height: 32).contentShape(Rectangle())
        .onTapGesture { workspace.focus(id, model: model) }
    }

    private func paneTitle(_ assignment: ChatPaneAssignment?) -> String {
        switch assignment {
        case .chat(let id): return model.chats.first { $0.id == id }?.title ?? "Chat"
        case .browser: return "Browser"
        case nil: return "New pane"
        }
    }
    private func picker(_ pane: UUID) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Open a chat").font(.headline)
            Text("Choose a chat or drag one from the sidebar.").font(.caption).foregroundStyle(.secondary)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 6) {
                    ForEach(model.chats) { chat in
                        Button { workspace.drop(chat: chat.id, pane: pane, zone: .center, model: model) } label: {
                            Text(chat.title).font(.system(size: 12)).lineLimit(1).frame(maxWidth: .infinity, alignment: .leading).padding(6)
                        }.buttonStyle(.plain)
                    }
                }
            }
            HStack {
                Button("New chat") { workspace.focus(pane, model: model); model.newChat() }
                if let chat = model.selectedID, workspace.browserPane(for: chat) == nil {
                    Button("Browser") {
                        workspace.layout.assignBrowser(paneID: pane, surface: chat)
                        workspace.focus(pane, model: model)
                    }
                }
            }
        }.padding(20).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
    private func registerCommands() {
        ChatPaneCommands.shared.perform = { command in
            switch command {
            case "right": workspace.split(.right, pane: workspace.layout.focusedPaneID); model.setPaneFocus(nil)
            case "below": workspace.split(.bottom, pane: workspace.layout.focusedPaneID); model.setPaneFocus(nil)
            case "close": workspace.close(workspace.layout.focusedPaneID, model: model)
            case "focus": workspace.focusedOnly.toggle()
            case "next": workspace.focusAdjacent(1, model: model)
            case "previous": workspace.focusAdjacent(-1, model: model)
            default: break
            }
        }
        ChatPaneCommands.shared.enabled = { command in
            switch command {
            case "right": return workspace.canSplit(.right, pane: workspace.layout.focusedPaneID)
            case "below": return workspace.canSplit(.bottom, pane: workspace.layout.focusedPaneID)
            case "close", "focus", "next", "previous": return workspace.layout.panes.count > 1
            default: return false
            }
        }
    }
}
