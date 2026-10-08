import AppKit
import SwiftUI
import UniformTypeIdentifiers

// Provider Hub's Chat window. One quiet glass surface over ChatModel: a
// collapsible rail of saved chats, a header that names the route, reasoning
// effort and working folder, a transcript that is the only main scroll, an
// approval strip above the composer, and a composer that sends on Return.
//
// Colour comes from the Python branding records through ChatRoute.presentation
// and ChatModel.accent(for:); nothing here keeps a provider colour table. A
// known provider tints the assistant edge, the model dot and the status
// spinner; an unknown provider keeps the neutral fallback those APIs return.

private typealias Semantic = HubTheme.Semantic

struct ChatWindow: View {
    @ObservedObject var model: ChatModel
    @AppStorage("chatRailVisible") private var railPreferred = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var dropTargeted = false

    private static let railWidth: CGFloat = 190
    /// Below this width the rail hides on its own; the toggle explains why.
    private static let narrowWidth: CGFloat = 860

    var body: some View {
        GeometryReader { proxy in
            let narrow = proxy.size.width < Self.narrowWidth
            HStack(spacing: 0) {
                if railPreferred && !narrow {
                    ChatRail(model: model).frame(width: Self.railWidth)
                    Rectangle().fill(Semantic.hairline).frame(width: HubTheme.Separator.width)
                }
                ChatPane(model: model, railVisible: railPreferred && !narrow, railAvailable: !narrow) { railPreferred.toggle() }
            }
            .animation(reduceMotion ? nil : .easeInOut(duration: 0.18), value: railPreferred)
        }
        .frame(minWidth: 720, minHeight: 500)
        .background(VibrancyBackground().ignoresSafeArea())
        .dropDestination(for: URL.self, action: { urls, _ in acceptFolder(urls) }, isTargeted: { dropTargeted = $0 })
        .overlay { if dropTargeted { dropHint } }
    }

    private var dropHint: some View {
        RoundedRectangle(cornerRadius: HubTheme.Radius.panel)
            .stroke(model.activeAccent.opacity(0.7), style: StrokeStyle(lineWidth: 2, dash: [6, 4]))
            .padding(10)
            .overlay {
                Text("Drop a folder to use it as the working folder")
                    .font(HubTheme.Typography.rowTitle).foregroundStyle(Semantic.ink)
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .background(RoundedRectangle(cornerRadius: HubTheme.Radius.row).fill(Semantic.raisedSurface))
            }
            .allowsHitTesting(false)
    }

    private func acceptFolder(_ urls: [URL]) -> Bool {
        guard !model.busy, model.selectedID != nil else { return false }
        var isDirectory: ObjCBool = false
        for url in urls where FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory) {
            let folder = isDirectory.boolValue ? url.path : url.deletingLastPathComponent().path
            model.setFolder(folder)
            return true
        }
        return false
    }
}

// MARK: - Helpers over the parent's model

fileprivate extension ChatModel {
    func route(named route: String) -> ChatRoute? { models.first { $0.route == route } }
    /// Only a provider with branding metadata may colour the transcript.
    func isKnownRoute(_ route: String) -> Bool { self.route(named: route)?.presentation != nil }
    func label(for route: String) -> String { self.route(named: route)?.label ?? route }
    func providerTitle(_ route: ChatRoute) -> String { route.presentation?.displayProvider ?? route.provider }
    /// Routes grouped by provider in first-seen order, for the model menu.
    var providerGroups: [(provider: String, title: String, routes: [ChatRoute])] {
        var order: [String] = []
        var groups: [String: [ChatRoute]] = [:]
        for route in models {
            if groups[route.provider] == nil { order.append(route.provider) }
            groups[route.provider, default: []].append(route)
        }
        return order.map { provider in
            let routes = groups[provider] ?? []
            return (provider, routes.first.map(providerTitle) ?? provider, routes)
        }
    }
    func hasSeveralAccounts(_ provider: String) -> Bool {
        Set(models.filter { $0.provider == provider }.map(\.account)).count > 1
    }
}

private func shortPath(_ path: String) -> String { (path as NSString).abbreviatingWithTildeInPath }
private func folderName(_ path: String) -> String {
    let name = (path as NSString).lastPathComponent
    return name.isEmpty ? shortPath(path) : name
}
private func effortTitle(_ effort: String) -> String {
    switch effort.lowercased() {
    case "xhigh": return "Extra high"
    case "": return "Default"
    default: return effort.prefix(1).uppercased() + effort.dropFirst()
    }
}
private func tokenCount(_ value: Int) -> String {
    if value >= 1_000_000 { return String(format: "%.1fM", Double(value) / 1_000_000) }
    if value >= 1_000 { return String(format: "%.1fK", Double(value) / 1_000) }
    return String(value)
}
/// Saved chats carry whatever timestamp the runtime wrote; an ISO-8601 value
/// reads as a relative time and anything else is shown as written.
private func whenLabel(_ raw: String) -> String {
    let iso = ISO8601DateFormatter()
    let date = iso.date(from: raw) ?? { iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return iso.date(from: raw) }()
    guard let date else { return raw }
    let formatter = RelativeDateTimeFormatter(); formatter.unitsStyle = .short
    return formatter.localizedString(for: date, relativeTo: Date())
}
private func reveal(_ path: String, in workspace: String?) {
    let full = path.hasPrefix("/") ? path : ((workspace ?? NSHomeDirectory()) as NSString).appendingPathComponent(path)
    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: full)])
}

private struct ModelDot: View {
    var color: Color
    var size: CGFloat = 7
    var body: some View {
        Circle().fill(color).overlay(Circle().stroke(Semantic.hairline, lineWidth: 0.5)).frame(width: size, height: size)
    }
}

// MARK: - Rail

private struct ChatRail: View {
    @ObservedObject var model: ChatModel
    @State private var renaming: String?
    @State private var renameText = ""
    @State private var pendingDelete: ChatSummary?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Chats").font(.system(size: 11, weight: .semibold)).foregroundStyle(Semantic.secondaryInk)
                .padding(.horizontal, 14).padding(.top, 12).padding(.bottom, 6)
            ScrollView {
                LazyVStack(spacing: 2) {
                    if model.chats.isEmpty {
                        Text(model.connected ? "No saved chats yet." : "Connecting…")
                            .font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
                            .frame(maxWidth: .infinity).padding(.vertical, 16)
                    }
                    ForEach(model.chats) { chat in row(chat) }
                }
                .padding(.horizontal, 6).padding(.bottom, 8)
            }
        }
        .confirmationDialog(deleteTitle, isPresented: deleteShown, presenting: pendingDelete) { chat in
            Button("Delete", role: .destructive) { model.delete(chat.id) }
        } message: { _ in
            Text("The saved transcript is removed from this Mac.")
        }
    }

    private var deleteTitle: String { "Delete “\(pendingDelete?.title ?? "")”?" }
    private var deleteShown: Binding<Bool> {
        Binding(get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } })
    }

    @ViewBuilder private func row(_ chat: ChatSummary) -> some View {
        let selected = chat.id == model.selectedID
        let route = model.route(named: chat.route)
        Button {
            guard renaming != chat.id else { return }
            model.select(chat.id)
        } label: {
            HStack(alignment: .top, spacing: 8) {
                ModelDot(color: route?.accent ?? .secondary, size: 6).padding(.top, 5)
                VStack(alignment: .leading, spacing: 2) {
                    if renaming == chat.id { renameField(chat) }
                    else { Text(chat.title).font(.system(size: 12.5)).foregroundStyle(Semantic.ink).lineLimit(1) }
                    Text(subtitle(chat, route: route)).font(.system(size: 10.5)).foregroundStyle(Semantic.secondaryInk).lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 8).padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: HubTheme.Radius.row).fill(selected ? Semantic.selection : Color.clear))
            .contentShape(RoundedRectangle(cornerRadius: HubTheme.Radius.row))
        }
        .buttonStyle(.plain)
        .disabled(model.busy && !selected)
        .accessibilityLabel(chat.title)
        .accessibilityValue(subtitle(chat, route: route))
        .accessibilityAddTraits(selected ? .isSelected : [])
        .contextMenu {
            Button("Rename") { renameText = chat.title; renaming = chat.id }
            Button("Delete…") { pendingDelete = chat }
        }
    }

    private func renameField(_ chat: ChatSummary) -> some View {
        TextField("Title", text: $renameText)
            .textFieldStyle(.plain).font(.system(size: 12.5)).foregroundStyle(Semantic.ink)
            .onSubmit {
                let title = renameText.trimmingCharacters(in: .whitespacesAndNewlines)
                if !title.isEmpty, title != chat.title { model.rename(chat.id, title: title) }
                renaming = nil
            }
            .onExitCommand { renaming = nil }
    }

    private func subtitle(_ chat: ChatSummary, route: ChatRoute?) -> String {
        let label = route?.label ?? chat.route
        let when = whenLabel(chat.updated)
        return when.isEmpty ? label : label + " · " + when
    }
}

// MARK: - Pane

private struct ChatPane: View {
    @ObservedObject var model: ChatModel
    var railVisible: Bool
    var railAvailable: Bool
    var toggleRail: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            ChatHeader(model: model, railVisible: railVisible, railAvailable: railAvailable, toggleRail: toggleRail)
            Rectangle().fill(Semantic.hairline).frame(height: HubTheme.Separator.width)
            if model.selectedID == nil { ChatWelcome(model: model) }
            else { ChatTranscript(model: model) }
            if let approval = model.approval { ApprovalStrip(model: model, approval: approval) }
            ChatComposer(model: model)
        }
    }
}

// MARK: - Header

private struct ChatHeader: View {
    @ObservedObject var model: ChatModel
    var railVisible: Bool
    var railAvailable: Bool
    var toggleRail: () -> Void

    var body: some View {
        HStack(spacing: HubTheme.Spacing.sm) {
            railButton
            newChatButton
            if model.selectedID != nil {
                modelMenu
                effortMenu
                folderMenu
            }
            Spacer(minLength: HubTheme.Spacing.sm)
            usage
            connection
        }
        .padding(.horizontal, HubTheme.Spacing.md).padding(.vertical, 7)
    }

    private var railButton: some View {
        Button(action: toggleRail) {
            Image(systemName: "sidebar.left").font(.system(size: 13)).foregroundStyle(Semantic.secondaryInk)
                .frame(width: 24, height: 22).contentShape(Rectangle())
        }
        .buttonStyle(.plain).disabled(!railAvailable)
        .keyboardShortcut("s", modifiers: [.command, .control])
        .accessibilityLabel(railVisible ? "Hide saved chats" : "Show saved chats")
        .help(railAvailable ? (railVisible ? "Hide saved chats (⌃⌘S)" : "Show saved chats (⌃⌘S)") : "Widen the window to show saved chats")
    }

    private var newChatButton: some View {
        Button { model.newChat() } label: {
            Image(systemName: "square.and.pencil").font(.system(size: 13)).foregroundStyle(Semantic.secondaryInk)
                .frame(width: 24, height: 22).contentShape(Rectangle())
        }
        .buttonStyle(.plain).disabled(model.busy || !model.connected)
        .keyboardShortcut("n", modifiers: .command)
        .accessibilityLabel("New chat").help("New chat (⌘N)")
    }

    private var modelMenu: some View {
        Menu {
            ForEach(model.providerGroups, id: \.provider) { group in
                Section(group.title) {
                    ForEach(group.routes) { route in
                        Button { model.setRoute(route.id) } label: {
                            if route.id == model.selectedRoute?.id { Label(routeTitle(route), systemImage: "checkmark") }
                            else { Text(routeTitle(route)) }
                        }
                    }
                }
            }
            if model.models.isEmpty { Text("No models yet. Configure a provider in Provider Hub and refresh.") }
        } label: {
            HStack(spacing: 6) {
                ModelDot(color: model.activeAccent)
                Text(model.selectedRoute?.label ?? "Choose model").font(HubTheme.Typography.rowTitle).foregroundStyle(Semantic.ink)
                if let route = model.selectedRoute, model.hasSeveralAccounts(route.provider), !route.accountLabel.isEmpty {
                    Text(route.accountLabel).font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
                }
            }
            .lineLimit(1)
        }
        .menuStyle(.button).buttonStyle(.borderless).fixedSize()
        .disabled(model.busy)
        .accessibilityLabel("Model")
        .help(model.selectedRoute.map { "\(model.providerTitle($0)) · \($0.accountLabel)" } ?? "Choose a model")
    }

    private func routeTitle(_ route: ChatRoute) -> String {
        guard model.hasSeveralAccounts(route.provider), !route.accountLabel.isEmpty else { return route.label }
        return route.label + " — " + route.accountLabel
    }

    @ViewBuilder private var effortMenu: some View {
        if let route = model.selectedRoute, !route.efforts.isEmpty {
            let current = model.selected?.effort ?? ""
            Menu {
                ForEach(route.efforts, id: \.self) { effort in
                    Button { model.setEffort(effort) } label: {
                        if effort == current { Label(effortTitle(effort), systemImage: "checkmark") }
                        else { Text(effortTitle(effort)) }
                    }
                }
            } label: {
                Label(effortTitle(current), systemImage: "dial.medium")
                    .font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk).lineLimit(1)
            }
            .menuStyle(.button).buttonStyle(.borderless).fixedSize()
            .disabled(model.busy)
            .accessibilityLabel("Reasoning effort").help("Reasoning effort")
        }
    }

    private var folderMenu: some View {
        let path = model.selected?.workspace ?? ""
        return Menu {
            Button("Choose Folder…") { model.chooseFolder() }
            if !model.recentFolders.isEmpty {
                Divider()
                ForEach(model.recentFolders, id: \.self) { folder in
                    Button { model.setFolder(folder) } label: {
                        if folder == path { Label(shortPath(folder), systemImage: "checkmark") } else { Text(shortPath(folder)) }
                    }
                }
            }
        } label: {
            Label(path.isEmpty ? "Choose folder" : folderName(path), systemImage: "folder")
                .font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk).lineLimit(1)
        }
        .menuStyle(.button).buttonStyle(.borderless).fixedSize()
        .disabled(model.busy)
        .accessibilityLabel("Working folder")
        .help(path.isEmpty ? "Choose the folder tools work in" : shortPath(path) + " — drop a folder here to change it")
    }

    @ViewBuilder private var usage: some View {
        if let used = model.tokenUsage {
            Text(model.contextLimit.map { tokenCount(used) + " / " + tokenCount($0) } ?? tokenCount(used) + " tokens")
                .font(.system(size: 10.5, design: .monospaced)).foregroundStyle(Semantic.secondaryInk)
                .help("Context used in this chat")
                .accessibilityLabel("Context used")
        }
    }

    @ViewBuilder private var connection: some View {
        if !model.connected {
            HStack(spacing: 5) {
                ProgressView().controlSize(.mini)
                Text("Connecting").font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
            }
        }
    }
}

// MARK: - Empty states

/// No chat is selected: say what a new chat would use and offer to start one.
private struct ChatWelcome: View {
    @ObservedObject var model: ChatModel

    var body: some View {
        VStack(spacing: HubTheme.Spacing.md) {
            Spacer()
            Text("Chat").font(HubTheme.Typography.paneTitle).foregroundStyle(Semantic.ink)
            Text(invitation).font(HubTheme.Typography.body).foregroundStyle(Semantic.secondaryInk)
                .multilineTextAlignment(.center).frame(maxWidth: 420)
            if let route = model.models.first {
                HStack(spacing: 14) {
                    HStack(spacing: 6) { ModelDot(color: route.accent); Text(route.label) }
                    Label(folderName(model.recentFolders.first ?? NSHomeDirectory()), systemImage: "folder")
                }
                .font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk).lineLimit(1)
            }
            Button("New chat") { model.newChat() }
                .buttonStyle(HubTheme.Control.prominentButton).controlSize(.regular)
                .disabled(model.busy || !model.connected || model.models.isEmpty)
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(HubTheme.Spacing.xl)
    }

    private var invitation: String {
        if !model.connected { return "Connecting to the chat runtime…" }
        if model.models.isEmpty { return "Configure a provider and refresh its models in Provider Hub, then come back here." }
        return "A new chat uses the model and folder below. Both can be changed from the header once it is open."
    }
}

/// A chat is open but has no turns yet.
private struct ChatInvitation: View {
    @ObservedObject var model: ChatModel

    var body: some View {
        VStack(spacing: 6) {
            Text(headline).font(HubTheme.Typography.body).foregroundStyle(Semantic.ink)
            Text("Tools run inside that folder and ask before changing files. Changing the model, account or folder starts a fresh context; the transcript stays.")
                .font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
        }
        .multilineTextAlignment(.center).frame(maxWidth: 460).frame(maxWidth: .infinity)
        .padding(.top, 60)
    }

    private var headline: String {
        let folder = model.selected.map { folderName($0.workspace) } ?? "a folder"
        guard let route = model.selectedRoute else { return "Choose a model to start." }
        return "Message \(route.label) about \(folder)."
    }
}

// MARK: - Transcript

private struct ContentBottomKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

private struct ChatTranscript: View {
    @ObservedObject var model: ChatModel
    @State private var expanded: Set<String> = []
    /// True while the end of the transcript is within reach of the viewport;
    /// new text follows only then, so reading earlier turns is never yanked.
    @State private var nearBottom = true
    @State private var viewportHeight: CGFloat = 0
    private let bottomID = "chat-transcript-bottom"

    var body: some View {
        GeometryReader { outer in
            ScrollViewReader { proxy in
                ScrollView {
                    content
                        .background(GeometryReader { inner in
                            Color.clear.preference(key: ContentBottomKey.self, value: inner.frame(in: .named("transcript")).maxY)
                        })
                }
                .coordinateSpace(name: "transcript")
                .onPreferenceChange(ContentBottomKey.self) { maxY in nearBottom = maxY - outer.size.height < 120 }
                .onChange(of: model.entries.count) { _, _ in
                    if model.entries.last?.kind == "user" || nearBottom { scrollToEnd(proxy) }
                }
                .onChange(of: model.entries.last?.text.count) { _, _ in if nearBottom { scrollToEnd(proxy) } }
                .onChange(of: model.selectedID) { _, _ in scrollToEnd(proxy) }
                .onAppear { scrollToEnd(proxy) }
            }
        }
    }

    private var content: some View {
        LazyVStack(alignment: .leading, spacing: 14) {
            if model.entries.isEmpty { ChatInvitation(model: model) }
            ForEach(model.entries) { entry in row(entry) }
            Color.clear.frame(height: 1).id(bottomID)
        }
        .frame(maxWidth: 760).frame(maxWidth: .infinity)
        .padding(.horizontal, 20).padding(.vertical, 16)
    }

    private func scrollToEnd(_ proxy: ScrollViewProxy) {
        proxy.scrollTo(bottomID, anchor: .bottom)
    }

    @ViewBuilder private func row(_ entry: ChatEntry) -> some View {
        let last = entry.id == model.entries.last?.id
        switch entry.kind {
        case "user":
            UserRow(entry: entry)
        case "assistant":
            AssistantRow(entry: entry, label: model.label(for: entry.route), accent: model.accent(for: entry.route),
                         known: model.isKnownRoute(entry.route), streaming: model.busy && last)
        case "tool":
            ToolRow(entry: entry, expanded: expandedBinding(entry.id), workspace: model.selected?.workspace)
        case "error":
            ErrorRow(entry: entry, canRetry: last && !model.busy && model.connected) { model.retry() }
        default:
            NoticeRow(entry: entry)
        }
    }

    private func expandedBinding(_ id: String) -> Binding<Bool> {
        Binding(get: { expanded.contains(id) }, set: { if $0 { expanded.insert(id) } else { expanded.remove(id) } })
    }
}

private struct UserRow: View {
    var entry: ChatEntry
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("You").font(.system(size: 10.5, weight: .semibold)).foregroundStyle(Semantic.secondaryInk)
            Text(entry.text).font(HubTheme.Typography.body).foregroundStyle(Semantic.ink).textSelection(.enabled)
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: HubTheme.Radius.providerTile).fill(Semantic.raisedSurface))
        .accessibilityElement(children: .combine)
    }
}

private struct AssistantRow: View {
    var entry: ChatEntry
    var label: String
    var accent: Color
    var known: Bool
    var streaming: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            RoundedRectangle(cornerRadius: 1.5)
                .fill(entry.isError ? Color(nsColor: .systemRed).opacity(0.7) : (known ? accent.opacity(0.75) : Semantic.hairline))
                .frame(width: 3).padding(.vertical, 2)
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    ModelDot(color: known ? accent : .secondary, size: 6)
                    Text(label).font(.system(size: 10.5, weight: .semibold)).foregroundStyle(Semantic.secondaryInk).lineLimit(1)
                    if streaming { ProgressView().controlSize(.mini).tint(known ? accent : Semantic.secondaryInk) }
                }
                if entry.text.isEmpty, streaming {
                    Text("Thinking…").font(HubTheme.Typography.body).foregroundStyle(Semantic.secondaryInk)
                } else {
                    Text(rendered).font(HubTheme.Typography.body).foregroundStyle(Semantic.ink).textSelection(.enabled)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    /// Inline Markdown only (emphasis, code, links); block syntax stays as
    /// typed so a half-streamed list or fence never flickers into a layout.
    private var rendered: AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: entry.text, options: options)) ?? AttributedString(entry.text)
    }
}

private struct ToolRow: View {
    var entry: ChatEntry
    @Binding var expanded: Bool
    var workspace: String?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            header
            if expanded { body_ }
        }
        .padding(.horizontal, 10).padding(.vertical, 6)
        .background(RoundedRectangle(cornerRadius: HubTheme.Radius.row).fill(Semantic.raisedSurface))
        .overlay(RoundedRectangle(cornerRadius: HubTheme.Radius.row).stroke(entry.isError ? Color(nsColor: .systemRed).opacity(0.35) : Semantic.hairline, lineWidth: 1))
    }

    private var header: some View {
        Button {
            if reduceMotion { expanded.toggle() } else { withAnimation(.easeInOut(duration: 0.15)) { expanded.toggle() } }
        } label: {
            HStack(spacing: 8) {
                Image(systemName: "chevron.right").font(.system(size: 9, weight: .semibold)).foregroundStyle(Semantic.secondaryInk)
                    .rotationEffect(.degrees(expanded ? 90 : 0)).frame(width: 10)
                Text(entry.tool ?? "tool").font(.system(size: 11.5, weight: .medium, design: .monospaced)).foregroundStyle(Semantic.ink)
                Text(entry.summary ?? entry.text).font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
                    .lineLimit(1).truncationMode(.middle)
                Spacer(minLength: 0)
                if !entry.changedFiles.isEmpty {
                    Text(entry.changedFiles.count == 1 ? "1 file" : "\(entry.changedFiles.count) files")
                        .font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
                }
                if entry.isError {
                    Image(systemName: "exclamationmark.triangle.fill").font(.system(size: 10)).foregroundStyle(Color(nsColor: .systemRed))
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Tool \(entry.tool ?? ""), \(entry.summary ?? entry.text)")
        .accessibilityValue(expanded ? "Expanded" : "Collapsed")
    }

    @ViewBuilder private var body_: some View {
        let detail = entry.detail?.isEmpty == false ? entry.detail! : entry.text
        if !detail.isEmpty {
            ScrollView([.vertical, .horizontal]) {
                PatchText(text: detail).padding(8)
            }
            .frame(maxHeight: 260)
            .background(RoundedRectangle(cornerRadius: 6).fill(Semantic.surface.opacity(0.6)))
        }
        if !entry.changedFiles.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                ForEach(entry.changedFiles, id: \.self) { path in
                    HStack(spacing: 6) {
                        Image(systemName: "doc").font(.system(size: 10)).foregroundStyle(Semantic.secondaryInk)
                        Text(path).font(.system(size: 11, design: .monospaced)).foregroundStyle(Semantic.ink)
                            .lineLimit(1).truncationMode(.middle).textSelection(.enabled)
                        Spacer(minLength: 0)
                        Button("Reveal") { reveal(path, in: workspace) }
                            .buttonStyle(.plain).font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
                            .accessibilityLabel("Reveal \(path) in Finder")
                    }
                }
            }
            .padding(.top, 2)
        }
    }
}

/// Monospaced output; when it reads as a patch, added and removed lines are
/// tinted and headers recede. One attributed Text keeps selection and copy plain.
private struct PatchText: View {
    var text: String
    private static let lineLimit = 600

    var body: some View {
        Text(attributed).font(.system(size: 11.5, design: .monospaced)).textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var attributed: AttributedString {
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
        let isPatch = lines.contains { $0.hasPrefix("@@") || $0.hasPrefix("+++ ") || $0.hasPrefix("--- ") || $0.hasPrefix("*** ") }
        var result = AttributedString()
        for (index, line) in lines.prefix(Self.lineLimit).enumerated() {
            var piece = AttributedString(String(line))
            piece.foregroundColor = isPatch ? colour(line) : Semantic.ink
            result.append(piece)
            if index < lines.count - 1 { result.append(AttributedString("\n")) }
        }
        if lines.count > Self.lineLimit {
            var more = AttributedString("… \(lines.count - Self.lineLimit) more lines")
            more.foregroundColor = Semantic.secondaryInk
            result.append(more)
        }
        return result
    }

    private func colour(_ line: Substring) -> Color {
        if line.hasPrefix("+++") || line.hasPrefix("---") || line.hasPrefix("@@") || line.hasPrefix("*** ") { return Semantic.secondaryInk }
        if line.hasPrefix("+") { return Color(nsColor: .systemGreen) }
        if line.hasPrefix("-") { return Color(nsColor: .systemRed) }
        return Semantic.ink
    }
}

private struct ErrorRow: View {
    var entry: ChatEntry
    var canRetry: Bool
    var retry: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill").font(.system(size: 11)).foregroundStyle(Color(nsColor: .systemRed)).padding(.top, 2)
            Text(entry.text).font(HubTheme.Typography.body).foregroundStyle(Semantic.ink).textSelection(.enabled)
            Spacer(minLength: 0)
            if canRetry {
                Button("Retry", action: retry).controlSize(.small)
                    .help("Resume from the last recorded result")
            }
        }
        .padding(.horizontal, 10).padding(.vertical, 8)
        .background(RoundedRectangle(cornerRadius: HubTheme.Radius.row).fill(Color(nsColor: .systemRed).opacity(0.08)))
        .overlay(RoundedRectangle(cornerRadius: HubTheme.Radius.row).stroke(Color(nsColor: .systemRed).opacity(0.3), lineWidth: 1))
        .accessibilityElement(children: .contain)
    }
}

private struct NoticeRow: View {
    var entry: ChatEntry
    var body: some View {
        Text(entry.text).font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
            .multilineTextAlignment(.center).frame(maxWidth: .infinity).padding(.vertical, 2)
    }
}

// MARK: - Approval

private struct ApprovalStrip: View {
    @ObservedObject var model: ChatModel
    var approval: ChatApproval
    @State private var showDetail = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "hand.raised.fill").font(.system(size: 11)).foregroundStyle(model.activeAccent)
                Text("Approval needed").font(HubTheme.Typography.rowTitle).foregroundStyle(Semantic.ink)
                Spacer(minLength: 0)
                Text(shortPath(approval.workspace)).font(.system(size: 10.5, design: .monospaced)).foregroundStyle(Semantic.secondaryInk)
                    .lineLimit(1).truncationMode(.middle)
            }
            ScrollView(.vertical) {
                Text(approval.summary).font(.system(size: 11.5, design: .monospaced)).foregroundStyle(Semantic.ink)
                    .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
            }.frame(height: min(140, max(34, CGFloat(approval.summary.split(separator: "\n", omittingEmptySubsequences: false).count) * 17)))
            if let detail = approval.detail, !detail.isEmpty { detailDisclosure(detail) }
            HStack {
                Spacer()
                Button("Deny") { model.decideApproval(allow: false) }
                    .controlSize(.small).keyboardShortcut("d", modifiers: .command).help("Deny (⌘D)")
                Button("Allow once") { model.decideApproval(allow: true) }
                    .buttonStyle(HubTheme.Control.prominentButton).controlSize(.small)
                    .keyboardShortcut("y", modifiers: .command).help("Allow this once (⌘Y)")
            }
        }
        .padding(HubTheme.Spacing.md)
        .background(RoundedRectangle(cornerRadius: HubTheme.Radius.providerTile).fill(Semantic.raisedSurface))
        .overlay(RoundedRectangle(cornerRadius: HubTheme.Radius.providerTile).stroke(model.activeAccent.opacity(0.45), lineWidth: 1))
        .padding(.horizontal, 16).padding(.top, 6)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Approval needed: \(approval.summary)")
    }

    private func detailDisclosure(_ detail: String) -> some View {
        DisclosureGroup(isExpanded: $showDetail) {
            ScrollView([.vertical, .horizontal]) { PatchText(text: detail).padding(8) }
                .frame(maxHeight: 220)
                .background(RoundedRectangle(cornerRadius: 6).fill(Semantic.surface.opacity(0.6)))
        } label: {
            Text("Proposed change").font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
        }
    }
}

// MARK: - Composer

private struct ChatComposer: View {
    @ObservedObject var model: ChatModel
    @State private var height: CGFloat = 22

    var body: some View {
        VStack(spacing: 5) {
            HStack(alignment: .bottom, spacing: 8) {
                ComposerTextView(text: $model.draft, height: $height, placeholder: placeholder, enabled: editable) { model.send() }
                    .frame(height: height)
                    .accessibilityLabel("Message")
                if model.busy { stopButton } else { sendButton }
            }
            .padding(.leading, 14).padding(.trailing, 7).padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: HubTheme.Radius.panel).fill(Semantic.raisedSurface))
            .overlay(RoundedRectangle(cornerRadius: HubTheme.Radius.panel).stroke(Semantic.hairline, lineWidth: 1))
            footer
        }
        .padding(.horizontal, 16).padding(.top, 8).padding(.bottom, 10)
    }

    private var editable: Bool { model.connected && model.selectedID != nil }

    private var placeholder: String {
        guard model.selectedID != nil else { return "Start a new chat to begin" }
        guard model.connected else { return "Connecting…" }
        guard let route = model.selectedRoute else { return "Choose a model above" }
        return "Message " + route.label + "…"
    }

    private var sendButton: some View {
        Button { model.send() } label: {
            Image(systemName: "arrow.up").font(.system(size: 13, weight: .bold)).foregroundStyle(Semantic.inkOnAccent)
                .frame(width: 28, height: 28).background(Circle().fill(Semantic.accentOnSurface))
        }
        .buttonStyle(.plain).disabled(!model.canSend).opacity(model.canSend ? 1 : 0.35)
        .accessibilityLabel("Send").help("Send (Return)")
    }

    private var stopButton: some View {
        Button { model.stop() } label: {
            Image(systemName: "stop.fill").font(.system(size: 11, weight: .bold)).foregroundStyle(Semantic.inkOnAccent)
                .frame(width: 28, height: 28).background(Circle().fill(model.activeAccent))
        }
        .buttonStyle(.plain).keyboardShortcut(".", modifiers: .command)
        .accessibilityLabel("Stop").help("Stop and keep what has arrived (⌘.)")
    }

    private var footer: some View {
        HStack(alignment: .top) {
            if model.notice.isEmpty {
                Text(model.status).font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
            } else {
                Text(model.notice).font(HubTheme.Typography.detail).foregroundStyle(Semantic.accentOnSurface).textSelection(.enabled)
            }
            Spacer(minLength: 8)
            Text("Return sends · Shift-Return for a new line").font(.system(size: 10.5)).foregroundStyle(Semantic.secondaryInk.opacity(0.7))
                .accessibilityHidden(true)
        }
        .lineLimit(2).padding(.horizontal, 6).frame(minHeight: 16)
    }
}

/// A plain NSTextView: Return and ⌘Return send, Shift-Return and Option-Return
/// insert a line, and the view grows with its text up to a few lines.
private struct ComposerTextView: NSViewRepresentable {
    @Binding var text: String
    @Binding var height: CGFloat
    var placeholder: String
    var enabled: Bool
    var onSend: () -> Void

    private static let minHeight: CGFloat = 22
    private static let maxHeight: CGFloat = 160

    final class SendTextView: NSTextView {
        var onSend: (() -> Void)?
        var placeholder = "" { didSet { if placeholder != oldValue { needsDisplay = true } } }

        override func keyDown(with event: NSEvent) {
            let command = event.modifierFlags.intersection(.deviceIndependentFlagsMask).contains(.command)
            if event.keyCode == 36, command, isEditable { onSend?(); return }
            super.keyDown(with: event)
        }

        override func draw(_ dirtyRect: NSRect) {
            super.draw(dirtyRect)
            guard string.isEmpty, !placeholder.isEmpty else { return }
            let attributes: [NSAttributedString.Key: Any] = [.font: font ?? NSFont.systemFont(ofSize: 14), .foregroundColor: NSColor.placeholderTextColor]
            let origin = NSPoint(x: textContainerInset.width + (textContainer?.lineFragmentPadding ?? 0), y: textContainerInset.height)
            (placeholder as NSString).draw(at: origin, withAttributes: attributes)
        }
    }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: ComposerTextView
        init(_ parent: ComposerTextView) { self.parent = parent }

        func textDidChange(_ notification: Notification) {
            guard let view = notification.object as? NSTextView else { return }
            parent.text = view.string
            parent.measure(view)
        }

        func textView(_ textView: NSTextView, doCommandBy selector: Selector) -> Bool {
            guard selector == #selector(NSResponder.insertNewline(_:)) else { return false }
            let flags = NSApp.currentEvent?.modifierFlags.intersection(.deviceIndependentFlagsMask) ?? []
            if flags.contains(.shift) || flags.contains(.option) { return false }
            parent.onSend()
            return true
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.drawsBackground = false; scroll.borderType = .noBorder
        scroll.hasVerticalScroller = true; scroll.autohidesScrollers = true
        let view = SendTextView()
        view.delegate = context.coordinator
        view.drawsBackground = false
        view.font = .systemFont(ofSize: 14); view.textColor = .labelColor
        view.isRichText = false; view.allowsUndo = true; view.usesFindBar = false
        view.isAutomaticQuoteSubstitutionEnabled = false; view.isAutomaticDashSubstitutionEnabled = false
        view.textContainerInset = NSSize(width: 0, height: 3)
        view.isVerticallyResizable = true; view.isHorizontallyResizable = false
        view.autoresizingMask = [.width]
        view.minSize = .zero
        view.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        view.textContainer?.widthTracksTextView = true
        view.textContainer?.containerSize = NSSize(width: 0, height: CGFloat.greatestFiniteMagnitude)
        view.setAccessibilityLabel("Message")
        scroll.documentView = view
        DispatchQueue.main.async { view.window?.makeFirstResponder(view) }
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.parent = self
        guard let view = scroll.documentView as? SendTextView else { return }
        view.onSend = onSend
        view.placeholder = placeholder
        view.isEditable = enabled
        if view.string != text { view.string = text }
        measure(view)
    }

    /// Publish the height the text needs, clamped to a few lines; the SwiftUI
    /// frame follows and the scroll view takes over past the ceiling.
    func measure(_ view: NSTextView) {
        guard let container = view.textContainer, let layout = view.layoutManager else { return }
        layout.ensureLayout(for: container)
        let used = layout.usedRect(for: container).height + view.textContainerInset.height * 2
        let next = min(max(used.rounded(.up), Self.minHeight), Self.maxHeight)
        guard abs(next - height) > 0.5 else { return }
        DispatchQueue.main.async { height = next }
    }
}
