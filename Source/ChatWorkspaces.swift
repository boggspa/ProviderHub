import AppKit
import SwiftUI

/// Paths are identities; labels are presentation only. Resolve aliases using
/// the same filesystem rule as the worker, without creating any directory.
struct ChatWorkspaceGroup: Identifiable {
    var id: String
    var chats: [ChatSummary]
    var secondaries: [String] = []

    static func identity(_ path: String) -> String {
        URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
            .standardizedFileURL.resolvingSymlinksInPath().path
    }

    static func rail(chats: [ChatSummary], folders: [String], projects: [String: [String]] = [:]) -> ChatRailGroups {
        var order: [String] = []
        var grouped: [String: [ChatSummary]] = [:]
        for path in folders + chats.map(\.workspace) {
            let key = identity(path)
            if grouped[key] == nil { order.append(key); grouped[key] = [] }
        }
        for chat in chats { grouped[identity(chat.workspace), default: []].append(chat) }
        let attached = Dictionary(uniqueKeysWithValues: projects.map { (identity($0.key), $0.value.map(identity)) })
        let all = order.map { path -> ChatWorkspaceGroup in
            ChatWorkspaceGroup(id: path, chats: (grouped[path] ?? []).sorted {
                $0.updated == $1.updated ? $0.id < $1.id : $0.updated > $1.updated
            }, secondaries: attached[path] ?? [])
        }
        // Promotion is purely a function of attachment: any workspace with at
        // least one connected folder is a project; the last disconnect demotes it.
        let recents = chats.sorted { $0.updated == $1.updated ? $0.id < $1.id : $0.updated > $1.updated }.prefix(10)
        return ChatRailGroups(projects: all.filter { !$0.secondaries.isEmpty },
                              workspaces: all.filter { $0.secondaries.isEmpty },
                              recents: Array(recents))
    }

    static func groups(chats: [ChatSummary], folders: [String], projects: [String: [String]] = [:]) -> [ChatWorkspaceGroup] {
        let sections = rail(chats: chats, folders: folders, projects: projects)
        return sections.projects + sections.workspaces
    }
}

struct ChatRailGroups {
    var projects: [ChatWorkspaceGroup]
    var workspaces: [ChatWorkspaceGroup]
    var recents: [ChatSummary]
}

/// What the Chat window's panes offer the sidebar, as Claude Desktop's does:
/// which chats are on screen and a way to open one beside the focused pane.
/// Outside the Chat window it offers nothing and the sidebar shows no pane items.
struct ChatPaneOpener {
    var openChats: Set<String> = []
    var split = false
    /// Opens a chat beside the focused pane: below it when the flag is set.
    var open: ((_ chat: String, _ below: Bool) -> Void)?
    func beside(_ chat: String) { open?(chat, false) }
    func below(_ chat: String) { open?(chat, true) }
}
private struct ChatPaneOpenerKey: EnvironmentKey { static let defaultValue = ChatPaneOpener() }
private struct ChatSearchRequestKey: EnvironmentKey { static let defaultValue: UUID? = nil }
extension EnvironmentValues {
    var chatPaneOpener: ChatPaneOpener {
        get { self[ChatPaneOpenerKey.self] }
        set { self[ChatPaneOpenerKey.self] = newValue }
    }
    var chatSearchRequest: UUID? {
        get { self[ChatSearchRequestKey.self] }
        set { self[ChatSearchRequestKey.self] = newValue }
    }
}

struct ChatWorkspaceRail: View {
    @ObservedObject var model: ChatModel
    @Environment(\.chatSearchRequest) private var searchRequest
    @State private var searching = false
    @State private var searchText = ""
    @FocusState private var searchFocused: Bool
    @State private var collapsed: Set<String> = []
    @State private var renaming: String?
    @State private var renameText = ""
    @State private var pendingDelete: ChatSummary?
    @Environment(\.chatPaneOpener) private var paneOpener
    private typealias Semantic = HubTheme.Semantic

    private static let recentsKey = "chat-rail-recents"

    private var rail: ChatRailGroups {
        ChatWorkspaceGroup.rail(chats: model.chats, folders: model.recentFolders, projects: model.workspaceProjects)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if searching {
                HStack {
                    Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                    TextField("Search all threads", text: $searchText).textFieldStyle(.plain).focused($searchFocused)
                    Button { searching = false; searchText = ""; searchFocused = false } label: { Image(systemName: "xmark") }
                        .buttonStyle(.plain).help("Close search")
                }.padding(10).onExitCommand { searching = false; searchText = ""; searchFocused = false }
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    if searching && !searchText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        if model.searchPending { ProgressView().controlSize(.small).padding(10) }
                        else if model.searchResults.isEmpty { Text("No matches").font(.system(size: 11)).foregroundStyle(.secondary).padding(10) }
                        ForEach(model.searchResults) { result in
                            Button { model.openSearchResult(result) } label: {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(result.title).font(.system(size: 12, weight: .medium)).lineLimit(1)
                                    Text(result.excerpt).font(.system(size: 10.5)).foregroundStyle(.secondary).lineLimit(3)
                                }.frame(maxWidth: .infinity, alignment: .leading).padding(7).contentShape(Rectangle())
                            }.buttonStyle(.plain)
                        }
                        if model.searchLimited { Text("First 200 matches shown").font(.system(size: 10)).foregroundStyle(.secondary).padding(8) }
                    } else {
                    // A chat appears in both its workspace and Recents. Give
                    // each placement its own lazy-stack identity; commands
                    // continue to use the original ChatSummary.id.
                    if !rail.projects.isEmpty {
                        sectionHeader("Projects")
                        ForEach(rail.projects) { group in
                            workspaceHeader(group)
                            if !collapsed.contains(group.id) {
                                ForEach(group.secondaries, id: \.self) { folder in
                                    secondaryRow(folder, primary: group.id)
                                }
                                ForEach(group.chats) { chat in chatRow(chat).id("project:\(group.id):\(chat.id)") }
                                if group.chats.isEmpty {
                                    Text("No chats yet").font(.system(size: 11))
                                        .foregroundStyle(Semantic.secondaryInk).padding(.leading, 24).padding(.bottom, 8)
                                }
                            }
                        }
                    }
                    if !rail.recents.isEmpty {
                        recentsHeader
                        if !collapsed.contains(Self.recentsKey) {
                            ForEach(rail.recents) { chat in chatRow(chat, showWorkspace: true).id("recent:\(chat.id)") }
                        }
                    }
                    sectionHeader("Workspaces")
                    ForEach(rail.workspaces) { group in
                        workspaceHeader(group)
                        if !collapsed.contains(group.id) {
                            ForEach(group.chats) { chat in chatRow(chat).id("workspace:\(group.id):\(chat.id)") }
                            if group.chats.isEmpty {
                                Text("No chats yet").font(.system(size: 11))
                                    .foregroundStyle(Semantic.secondaryInk).padding(.leading, 24).padding(.bottom, 8)
                            }
                        }
                    }
                    if rail.projects.isEmpty && rail.workspaces.isEmpty && rail.recents.isEmpty {
                        Text(model.connected ? "Choose a folder to begin." : "Connecting…")
                            .font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk).padding(8)
                    }
                    }
                }.padding(.horizontal, 6).padding(.bottom, 8)
            }
            HStack { ChatSettingsMenu(model: model); Spacer() }.padding(.horizontal, 14).padding(.vertical, 12)
        }
        .onAppear { if searchRequest != nil { searching = true; searchFocused = true } }
        .onChange(of: searchRequest) { _, request in if request != nil { searching = true; searchFocused = true } }
        .task(id: searchText) {
            try? await Task.sleep(for: .milliseconds(250))
            guard !Task.isCancelled else { return }
            model.searchTranscripts(searchText)
        }
        .onChange(of: model.selectedID) { _, _ in
            if let path = model.selected?.workspace { collapsed.remove(ChatWorkspaceGroup.identity(path)) }
        }
        .confirmationDialog("Delete “\(pendingDelete?.title ?? "")”?", isPresented: Binding(
            get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } }
        ), presenting: pendingDelete) { chat in
            Button("Delete", role: .destructive) { model.delete(chat.id) }
        } message: { _ in Text("The saved transcript is removed from this Mac.") }
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title).font(.system(size: 11, weight: .semibold))
            .foregroundStyle(Semantic.secondaryInk)
            .padding(.horizontal, 14).padding(.top, 12).padding(.bottom, 6)
    }

    private var recentsHeader: some View {
        Button {
            if !collapsed.insert(Self.recentsKey).inserted { collapsed.remove(Self.recentsKey) }
        } label: {
            HStack(spacing: 5) {
                Image(systemName: collapsed.contains(Self.recentsKey) ? "chevron.right" : "chevron.down")
                    .font(.system(size: 9)).frame(width: 10)
                Text("Recents").font(.system(size: 11, weight: .semibold))
                Spacer(minLength: 0)
            }.foregroundStyle(Semantic.secondaryInk).contentShape(Rectangle())
        }.buttonStyle(.plain)
        .padding(.horizontal, 14).padding(.top, 12).padding(.bottom, 6)
        .accessibilityLabel("Recents").accessibilityValue(collapsed.contains(Self.recentsKey) ? "Collapsed" : "Expanded")
    }

    private func workspaceHeader(_ group: ChatWorkspaceGroup) -> some View {
        let name = (group.id as NSString).lastPathComponent
        let label = group.id == NSHomeDirectory() ? "Home" : name.isEmpty ? "/" : name
        let allGroups = rail.projects + rail.workspaces
        let duplicate = allGroups.filter { ($0.id as NSString).lastPathComponent == name }.count > 1
        return HStack(spacing: 5) {
            Button {
                if !collapsed.insert(group.id).inserted { collapsed.remove(group.id) }
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: collapsed.contains(group.id) ? "chevron.right" : "chevron.down")
                        .font(.system(size: 9)).frame(width: 10)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(label).font(.system(size: 11.5, weight: .semibold)).lineLimit(1)
                        if duplicate { Text(group.id).font(.system(size: 9.5)).foregroundStyle(Semantic.secondaryInk).lineLimit(1).truncationMode(.middle) }
                    }
                    Spacer(minLength: 0)
                }.contentShape(Rectangle())
            }.buttonStyle(.plain).help(group.id)
                .accessibilityLabel(label).accessibilityValue(collapsed.contains(group.id) ? "Collapsed" : "Expanded")
            Button { model.newChat(in: group.id) } label: {
                Image(systemName: "plus").font(.system(size: 11)).frame(width: 20, height: 24)
            }.buttonStyle(.plain).disabled(!model.connected)
                .help("New chat here").accessibilityLabel("New chat in \(label)")
        }.foregroundStyle(Semantic.ink).padding(.horizontal, 6).padding(.top, 8).padding(.bottom, 3)
        .contextMenu {
            Button("Attach folder…") { model.attachFolder(to: group.id) }
        }
    }

    private func secondaryRow(_ folder: String, primary: String) -> some View {
        let name = (folder as NSString).lastPathComponent
        let label = folder == NSHomeDirectory() ? "Home" : name.isEmpty ? "/" : name
        return HStack(spacing: 5) {
            Image(systemName: "folder").font(.system(size: 9)).foregroundStyle(Semantic.secondaryInk).frame(width: 10)
            Text(label).font(.system(size: 11.5)).foregroundStyle(Semantic.secondaryInk).lineLimit(1)
            Spacer(minLength: 0)
        }
        .padding(.leading, 24).padding(.trailing, 8).padding(.vertical, 3)
        .contentShape(Rectangle())
        .help(folder)
        .accessibilityLabel("Attached folder \(label)")
        .contextMenu {
            Button("Disconnect") { model.detachFolder(from: primary, folder) }
        }
    }

    private func chatRow(_ chat: ChatSummary, showWorkspace: Bool = false) -> some View {
        let selected = chat.id == model.selectedID
        let route = model.models.first { $0.route == chat.route && $0.account == chat.account }
        return HStack(spacing: 7) {
            if let presentation = route?.presentation { ProviderMark(presentation: presentation, size: 13) }
            else { Image(systemName: "bubble.left").font(.system(size: 12)).foregroundStyle(Semantic.secondaryInk) }
            if renaming == chat.id {
                TextField("Title", text: $renameText).textFieldStyle(.plain).font(.system(size: 12.5))
                    .onSubmit {
                        let title = renameText.trimmingCharacters(in: .whitespacesAndNewlines)
                        if !title.isEmpty, title != chat.title { model.rename(chat.id, title: title) }
                        renaming = nil
                    }.onExitCommand { renaming = nil }
            } else {
                Button { model.select(chat.id) } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(chat.title).font(.system(size: 12.5)).foregroundStyle(Semantic.ink).lineLimit(1)
                        Text(subtitle(chat, route: route, showWorkspace: showWorkspace)).font(.system(size: 10.5)).foregroundStyle(Semantic.secondaryInk).lineLimit(1)
                    }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                }.buttonStyle(.plain).disabled(!model.connected)
                if model.needsApproval(chat.id) {
                    Image(systemName: "hand.raised.fill").font(.system(size: 11))
                        .foregroundStyle(Semantic.secondaryInk).accessibilityLabel("Needs approval")
                } else if model.isActive(chat.id) {
                    ProgressView().controlSize(.mini).tint(route?.accent ?? Semantic.secondaryInk).accessibilityLabel("Working")
                } else if paneOpener.split, paneOpener.openChats.contains(chat.id) {
                    // On screen in one of the window's panes, as Claude Desktop marks it.
                    Circle().fill(Semantic.secondaryInk).frame(width: 5, height: 5).padding(.trailing, 2)
                        .help("Open in a pane").accessibilityLabel("Open in a pane")
                }
            }
        }
        .padding(.leading, 18).padding(.trailing, 8).padding(.vertical, 6)
        .background(RoundedRectangle(cornerRadius: HubTheme.Radius.row).fill(selected ? Semantic.selection : Color.clear))
        // The selected row carries its model's accent as a 2pt leading bar,
        // the rail's one touch of colour; an unbranded route keeps it neutral.
        .overlay(alignment: .leading) {
            if selected {
                RoundedRectangle(cornerRadius: 1).fill(route?.accent ?? Semantic.secondaryInk)
                    .frame(width: 2).padding(.vertical, 7).padding(.leading, 7).accessibilityHidden(true)
            }
        }
        .accessibilityAddTraits(selected ? .isSelected : [])
        .onDrag {
            let provider = NSItemProvider()
            guard renaming != chat.id else { return provider }
            provider.registerDataRepresentation(forTypeIdentifier: "com.mistralbridge.providerhub.chat-id", visibility: .ownProcess) { completion in
                completion(Data(chat.id.utf8), nil)
                return nil
            }
            return provider
        }
        .contextMenu {
            if paneOpener.open != nil {
                Menu("Open in") {
                    Button("Split View") { paneOpener.beside(chat.id) }
                    Button("Split View Below") { paneOpener.below(chat.id) }
                }.disabled(!model.connected)
                Divider()
            }
            Button("Rename") { renameText = chat.title; renaming = chat.id }.disabled(model.isActive(chat.id))
            Button("Delete…") { pendingDelete = chat }.disabled(model.isActive(chat.id))
        }
    }

    private func subtitle(_ chat: ChatSummary, route: ChatRoute?, showWorkspace: Bool = false) -> String {
        var label = route?.label ?? chat.route
        if showWorkspace { label = workspaceLabel(chat.workspace) + " · " + label }
        let iso = ISO8601DateFormatter()
        let date = iso.date(from: chat.updated) ?? {
            iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            return iso.date(from: chat.updated)
        }()
        guard let date else { return label }
        // A working chat saves every few seconds; a ticking "13 sec ago", or
        // "in 0 sec" when the save lands a moment ahead, only jitters.
        if date.timeIntervalSinceNow > -60 { return label + " · now" }
        let formatter = RelativeDateTimeFormatter(); formatter.unitsStyle = .short
        return label + " · " + formatter.localizedString(for: date, relativeTo: Date())
    }

    private func workspaceLabel(_ path: String) -> String {
        let resolved = ChatWorkspaceGroup.identity(path)
        let name = (resolved as NSString).lastPathComponent
        return resolved == NSHomeDirectory() ? "Home" : name.isEmpty ? "/" : name
    }
}
