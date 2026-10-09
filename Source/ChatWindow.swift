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
// known provider tints the model dot and the status
// spinner; an unknown provider keeps the neutral fallback those APIs return.

private typealias Semantic = HubTheme.Semantic

struct ChatWindow: View {
    @ObservedObject var model: ChatModel
    @AppStorage("chatRailVisible") private var railPreferred = true
    @AppStorage(HubTheme.WindowStyle.defaultsKey) private var windowStyle = HubTheme.WindowStyle.Mode.glass
    @AppStorage("chatMonospacedText") private var monospacedText = false
    @AppStorage("chatFontChoice") private var fontChoice = ""
    @AppStorage("chatCustomFontName") private var customFontName = ""
    @AppStorage("chatTextSize") private var textSize = 13.0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var dropTargeted = false

    private static let railWidth: CGFloat = 190
    /// Below this width the rail hides on its own; the toggle explains why.
    private static let narrowWidth: CGFloat = 860

    var body: some View {
        GeometryReader { proxy in
            let inspectorWidth = min(360, max(290, proxy.size.width * 0.35))
            let remaining = proxy.size.width - (model.inspectorVisible ? inspectorWidth : 0)
            let narrow = remaining < Self.narrowWidth
            HStack(spacing: 0) {
                if railPreferred && !narrow {
                    ChatWorkspaceRail(model: model).frame(width: Self.railWidth)
                    Rectangle().fill(Semantic.hairline).frame(width: HubTheme.Separator.width)
                }
                ChatPane(model: model, railVisible: railPreferred && !narrow, railAvailable: !narrow,
                         compactHeader: remaining - (railPreferred && !narrow ? Self.railWidth : 0) < 860) { railPreferred.toggle() }
                if model.inspectorVisible {
                    Rectangle().fill(Semantic.hairline).frame(width: HubTheme.Separator.width)
                    ChatInspector(model: model).frame(width: inspectorWidth)
                }
            }
            .animation(reduceMotion ? nil : .easeInOut(duration: 0.18), value: railPreferred)
            .animation(reduceMotion ? nil : .easeInOut(duration: 0.18), value: model.inspectorVisible)
        }
        .frame(minWidth: 720, minHeight: 500)
        .environment(\.chatTextStyle, ChatTextStyle(size: textSize,
            selection: ChatFonts.selection(fontChoice, legacyMonospaced: monospacedText), customName: customFontName))
        .background {
            if windowStyle == .glass { VibrancyBackground().ignoresSafeArea() }
            else { Color(nsColor: .windowBackgroundColor).ignoresSafeArea() }
        }
        .dropDestination(for: URL.self, action: { urls, _ in acceptFolder(urls) }, isTargeted: { dropTargeted = $0 })
        .overlay { if dropTargeted { dropHint } }
    }

    private var dropHint: some View {
        RoundedRectangle(cornerRadius: HubTheme.Radius.panel)
            .stroke(model.activeAccent.opacity(0.7), style: StrokeStyle(lineWidth: 2, dash: [6, 4]))
            .padding(10)
            .overlay {
                Text(model.busy || model.branchBusy ? "Drop files to attach" : "Drop files to attach, or a folder to change workspace")
                    .font(HubTheme.Typography.rowTitle).foregroundStyle(Semantic.ink)
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .background(RoundedRectangle(cornerRadius: HubTheme.Radius.row).fill(Semantic.raisedSurface))
            }
            .allowsHitTesting(false)
    }

    /// A folder changes the workspace, which waits for the turn to end; files
    /// attach to the draft at any time, as the + button does, so a dropped
    /// screenshot can ride along with an update mid-turn.
    private func acceptFolder(_ urls: [URL]) -> Bool {
        guard model.connected else { return false }
        var isDirectory: ObjCBool = false
        for url in urls where FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory) {
            if isDirectory.boolValue {
                guard !model.busy, !model.branchBusy else { model.notice = "Finish or stop the turn before changing folder."; return false }
                model.setFolder(url.path); return true
            }
        }
        guard model.selectedID != nil else { return false }
        model.addAttachments(urls)
        return !urls.isEmpty
    }
}

// MARK: - Helpers over the parent's model

fileprivate extension ChatModel {
    func route(named route: String) -> ChatRoute? { models.first { $0.route == route } }
    /// Only a provider with branding metadata may colour the transcript.
    func isKnownRoute(_ route: String) -> Bool { self.route(named: route)?.presentation != nil }
    func label(for route: String) -> String { self.route(named: route)?.label ?? route }
    func providerTitle(_ route: ChatRoute) -> String { route.presentation?.displayProvider ?? route.provider }
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
private func reveal(_ path: String, in workspace: String?) {
    let full = path.hasPrefix("/") ? path : ((workspace ?? NSHomeDirectory()) as NSString).appendingPathComponent(path)
    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: full)])
}

struct ChatProviderIcon: View {
    var presentation: ProviderPresentation?
    var size: CGFloat = 14
    @ViewBuilder var body: some View {
        if let presentation { ProviderMark(presentation: presentation, size: size) }
        else { Image(systemName: "sparkle").font(.system(size: size - 2)).foregroundStyle(.secondary).frame(width: size, height: size) }
    }
}

// MARK: - Pane

private struct ChatPane: View {
    @ObservedObject var model: ChatModel
    var railVisible: Bool
    var railAvailable: Bool
    var compactHeader: Bool
    var toggleRail: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            ChatHeader(model: model, railVisible: railVisible, railAvailable: railAvailable, compact: compactHeader, toggleRail: toggleRail)
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
    var compact: Bool
    var toggleRail: () -> Void
    @State private var showingModels = false

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: HubTheme.Spacing.sm) {
                railButton
                newChatButton
                if model.selectedID != nil {
                    modelMenu
                    if !compact { workspaceControls }
                }
                Spacer(minLength: 4)
                usage
                connection
                inspectorButton
            }
            if compact, model.selectedID != nil { workspaceControls.padding(.leading, 4) }
        }
        .padding(.horizontal, HubTheme.Spacing.md).padding(.vertical, 7)
    }

    private var workspaceControls: some View {
        HStack(spacing: 9) {
            folderMenu
            ChatBranchChip(model: model)
            Spacer(minLength: 0)
            ChatGitIndicator(model: model).contentShape(Rectangle())
                .onTapGesture { model.showInspector(.changes) }
                .accessibilityAction(named: Text("Inspect file changes")) { model.showInspector(.changes) }
        }
    }

    private var inspectorButton: some View {
        Button { model.inspectorVisible.toggle() } label: {
            Image(systemName: "sidebar.right").font(.system(size: 13))
                .foregroundStyle(model.inspectorVisible ? Semantic.ink : Semantic.secondaryInk)
                .frame(width: 24, height: 22).contentShape(Rectangle())
        }.buttonStyle(.plain).keyboardShortcut("i", modifiers: [.command, .control])
            .accessibilityLabel(model.inspectorVisible ? "Hide inspector" : "Show inspector")
            .help("Inspector (⌃⌘I)")
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
        .buttonStyle(.plain).disabled(model.busy || model.branchBusy || !model.connected)
        .keyboardShortcut("n", modifiers: .command)
        .accessibilityLabel("New chat").help("New chat (⌘N)")
    }

    private var modelMenu: some View {
        Button {
            showingModels = true; model.refresh()
        } label: {
            HStack(spacing: 6) {
                ChatProviderIcon(presentation: model.selectedRoute?.presentation)
                Text(model.selectedRoute?.label ?? "Choose model").font(HubTheme.Typography.rowTitle).foregroundStyle(Semantic.ink)
                if let route = model.selectedRoute, model.hasSeveralAccounts(route.provider), !route.accountLabel.isEmpty {
                    Text(route.accountLabel).font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
                }
                if let effort = model.selected?.effort, !effort.isEmpty {
                    Text("· " + effortTitle(effort)).font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
                }
                Image(systemName: "chevron.down").font(.system(size: 9)).foregroundStyle(Semantic.secondaryInk)
            }
            .lineLimit(1)
        }
        .buttonStyle(.plain)
        .popover(isPresented: $showingModels, arrowEdge: .bottom) {
            ChatModelPicker(model: model) { showingModels = false }
        }
        .disabled(model.busy || model.branchBusy)
        .accessibilityLabel("Model")
        .help(model.selectedRoute.map { "\(model.providerTitle($0)) · \($0.accountLabel)" } ?? "Choose a model")
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
        .menuStyle(.button).buttonStyle(.borderless).frame(maxWidth: 170, alignment: .leading)
        .disabled(model.busy || model.branchBusy)
        .accessibilityLabel("Working folder")
        .help(path.isEmpty ? "Choose the folder tools work in" : shortPath(path) + " — drop a folder here to change it")
    }

    @ViewBuilder private var usage: some View {
        if let used = model.tokenUsage {
            let readout = model.contextLimit.map { tokenCount(used) + " / " + tokenCount($0) } ?? tokenCount(used) + " tokens"
            HStack(spacing: 6) {
                if let limit = model.contextLimit, limit > 0 {
                    ChatContextRing(used: used, limit: limit, accent: model.activeAccent)
                }
                Text(readout).font(.system(size: 10.5, design: .monospaced)).foregroundStyle(Semantic.secondaryInk)
            }
            .help(model.contextLimit.map { "\(Int((ChatContextRing.fraction(used: used, limit: $0) * 100).rounded()))% of context used in this chat" }
                  ?? "Context used in this chat")
            .accessibilityElement(children: .combine).accessibilityLabel("Context used: " + readout)
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
                    HStack(spacing: 6) { ChatProviderIcon(presentation: route.presentation); Text(route.label) }
                    Label(folderName(model.recentFolders.first ?? NSHomeDirectory()), systemImage: "folder")
                }
                .font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk).lineLimit(1)
            }
            Button("New chat") { model.newChat() }
                .buttonStyle(HubTheme.Control.prominentButton).controlSize(.regular)
                .disabled(model.busy || !model.connected || model.models.isEmpty)
            Button("Choose Folder…") { model.chooseFolder() }
                .buttonStyle(.plain).font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
                .disabled(model.busy || model.branchBusy || !model.connected || model.models.isEmpty)
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
    /// Explicit open/closed choices per fold; a fold without one follows the
    /// turn (open while it is the live tail, closed afterwards).
    @State private var foldChoice: [String: Bool] = [:]
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
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
        let segments = ChatTranscriptSegment.segments(model.entries)
        return LazyVStack(alignment: .leading, spacing: 14) {
            if model.entries.isEmpty { ChatInvitation(model: model) }
            ForEach(segments) { segment in
                switch segment {
                case .entry(let entry): row(entry)
                case .fold(let entries): fold(entries, live: model.busy && segment.id == segments.last?.id)
                }
            }
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
                         presentation: model.route(named: entry.route)?.presentation, streaming: model.busy && last)
        case "tool":
            if let ids = entry.agentIDs, !ids.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ToolRow(entry: entry, expanded: expandedBinding(entry.id), workspace: entry.workspace ?? model.selected?.workspace, onInspect: {
                        model.inspectedAgentID = nil; model.showInspector(.agents)
                    }, running: model.agents.contains { ids.contains($0.id) && $0.busy })
                    ChatParallelLanes(model: model, agentIDs: ids).padding(.leading, 18)
                }
            } else if let agentID = entry.agentID {
                ToolRow(entry: entry, expanded: expandedBinding(entry.id), workspace: model.selected?.workspace, onInspect: {
                    model.inspectedAgentID = agentID; model.showInspector(.agents)
                }, running: model.agents.first { $0.id == agentID }?.busy == true)
            } else { ToolRow(entry: entry, expanded: expandedBinding(entry.id), workspace: model.selected?.workspace) }
        case "error":
            ErrorRow(entry: entry, canRetry: last && !model.busy && model.connected) { model.retry() }
        default:
            NoticeRow(entry: entry)
        }
    }

    private func expandedBinding(_ id: String) -> Binding<Bool> {
        Binding(get: { expanded.contains(id) }, set: { if $0 { expanded.insert(id) } else { expanded.remove(id) } })
    }

    /// A run of local tool rows behind one disclosure. The live tail run stays
    /// open so progress is visible; once the reply begins it folds, and a
    /// choice the user makes by hand sticks for that run. Collapsed and live,
    /// the header carries the latest step so nothing goes dark mid-turn.
    @ViewBuilder private func fold(_ entries: [ChatEntry], live: Bool) -> some View {
        let id = ChatTranscriptSegment.foldID(entries)
        let open = foldChoice[id] ?? live
        let files = Set(entries.flatMap(\.changedFiles)).count
        let counted = entries.compactMap { ChatPatchStats.count(patchText($0)) }
        let stats = counted.isEmpty ? nil : counted.reduce(ChatPatchStats(), +)
        VStack(alignment: .leading, spacing: 6) {
            Button {
                if reduceMotion { foldChoice[id] = !open } else { withAnimation(.easeInOut(duration: 0.15)) { foldChoice[id] = !open } }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "chevron.right").font(.system(size: 9, weight: .semibold)).foregroundStyle(Semantic.secondaryInk)
                        .rotationEffect(.degrees(open ? 90 : 0)).frame(width: 10)
                    ChatShimmerText(text: live ? "Working" : "Worked", active: live, accent: model.activeAccent, base: Semantic.ink)
                        .font(.system(size: 11.5, weight: .medium))
                    Text("· \(entries.count) steps" + (files > 0 ? " · \(files) \(files == 1 ? "file" : "files")" : ""))
                        .font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
                    if let stats { ChatPatchStatsLabel(stats: stats) }
                    if entries.contains(where: \.isError) {
                        Image(systemName: "exclamationmark.triangle.fill").font(.system(size: 10)).foregroundStyle(Color(nsColor: .systemRed))
                    }
                    if !open, live, let latest = entries.last {
                        Text(latest.summary ?? latest.text).font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
                            .lineLimit(1).truncationMode(.middle)
                    }
                    Spacer(minLength: 0)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel((live ? "Working, " : "Worked, ") + "\(entries.count) steps")
            .accessibilityValue(open ? "Expanded" : "Collapsed")
            if open {
                VStack(alignment: .leading, spacing: 14) { ForEach(entries) { entry in row(entry) } }.padding(.leading, 18)
            }
        }
        .padding(.vertical, 4)
    }
}

/// The recorded patch of a local patch row, or nothing for every other tool;
/// shell output that happens to look like a diff never counts as one.
private func patchText(_ entry: ChatEntry) -> String {
    guard entry.tool == "apply_patch" else { return "" }
    return entry.detail?.isEmpty == false ? entry.detail! : entry.text
}

/// Consecutive local tool rows fold into one "Worked · N steps" disclosure,
/// as Codex Desktop collapses a run of activity. Delegate rows keep their
/// lanes and break a run; runs shorter than three stay as plain rows.
private enum ChatTranscriptSegment: Identifiable {
    case entry(ChatEntry)
    case fold([ChatEntry])
    static let minimumFold = 3

    var id: String {
        switch self {
        case .entry(let entry): return entry.id
        case .fold(let entries): return Self.foldID(entries)
        }
    }
    static func foldID(_ entries: [ChatEntry]) -> String { "fold-" + (entries.first?.id ?? "") }

    static func segments(_ entries: [ChatEntry]) -> [ChatTranscriptSegment] {
        var result: [ChatTranscriptSegment] = []
        var run: [ChatEntry] = []
        func flush() {
            if run.count >= minimumFold { result.append(.fold(run)) }
            else { result.append(contentsOf: run.map { ChatTranscriptSegment.entry($0) }) }
            run.removeAll()
        }
        for entry in entries {
            if entry.kind == "tool", entry.agentID == nil, entry.agentIDs?.isEmpty != false { run.append(entry) }
            else { flush(); result.append(.entry(entry)) }
        }
        flush()
        return result
    }
}

struct UserRow: View {
    var entry: ChatEntry
    var title = "You"
    @Environment(\.chatTextStyle) private var textStyle
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.system(size: 10.5, weight: .semibold)).foregroundStyle(Semantic.secondaryInk)
            if let attachments = entry.attachments, !attachments.isEmpty { ChatAttachmentStrip(attachments: attachments) }
            if !entry.text.isEmpty { Text(entry.text).font(textStyle.font).foregroundStyle(Semantic.ink).textSelection(.enabled) }
        }
        .padding(.vertical, 5)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
    }
}

struct AssistantRow: View {
    var entry: ChatEntry
    var label: String
    var accent: Color
    var presentation: ProviderPresentation?
    var streaming: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    ChatProviderIcon(presentation: presentation, size: 13)
                    Text(label).font(.system(size: 10.5, weight: .semibold)).foregroundStyle(Semantic.secondaryInk).lineLimit(1)
                    if streaming { ProgressView().controlSize(.mini).tint(accent) }
                }
                if entry.text.isEmpty, streaming {
                    ChatShimmerText(text: "Thinking…", active: true, accent: accent).font(HubTheme.Typography.body)
                } else {
                    ChatTranscriptText(text: entry.text, streaming: streaming)
                }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .contextMenu { Button("Copy reply") { ChatTranscriptTable.copy(entry.text) } }
        .accessibilityElement(children: .contain)
    }
}

struct ToolRow: View {
    var entry: ChatEntry
    @Binding var expanded: Bool
    var workspace: String?
    var onInspect: (() -> Void)? = nil
    var running = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                header
                if let onInspect {
                    Button(action: onInspect) { Image(systemName: "arrow.up.right").font(.system(size: 10)).frame(width: 20, height: 22) }
                        .buttonStyle(.plain).foregroundStyle(.secondary).help("Inspect subagent transcript")
                        .accessibilityLabel("Inspect subagent transcript")
                }
            }
            if expanded { body_ }
        }
        .padding(.vertical, 4)
    }

    private var header: some View {
        Button {
            if reduceMotion { expanded.toggle() } else { withAnimation(.easeInOut(duration: 0.15)) { expanded.toggle() } }
        } label: {
            HStack(spacing: 8) {
                Image(systemName: "chevron.right").font(.system(size: 9, weight: .semibold)).foregroundStyle(Semantic.secondaryInk)
                    .rotationEffect(.degrees(expanded ? 90 : 0)).frame(width: 10)
                if entry.tool == "delegate" { Image(systemName: "person.2").font(.system(size: 12)).foregroundStyle(Semantic.secondaryInk) }
                else { ChatToolGlyph(name: entry.tool ?? "read_file").foregroundStyle(Semantic.secondaryInk) }
                Text(entry.tool ?? "tool").font(.system(size: 11.5, weight: .medium, design: .monospaced)).foregroundStyle(Semantic.ink)
                Text(entry.summary ?? entry.text).font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
                    .lineLimit(1).truncationMode(.middle)
                Spacer(minLength: 0)
                if running { ProgressView().controlSize(.mini).accessibilityHidden(true) }
                if let stats = ChatPatchStats.count(patchText(entry)) { ChatPatchStatsLabel(stats: stats) }
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
        .accessibilityValue((expanded ? "Expanded" : "Collapsed") + (running ? ", Working" : ""))
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
                        Button("Reveal") { reveal(path, in: entry.workspace ?? workspace) }
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
struct PatchText: View {
    var text: String
    var onDark = false
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
            piece.foregroundColor = isPatch ? colour(line) : ink
            result.append(piece)
            if index < lines.count - 1 { result.append(AttributedString("\n")) }
        }
        if lines.count > Self.lineLimit {
            var more = AttributedString("… \(lines.count - Self.lineLimit) more lines")
            more.foregroundColor = secondaryInk
            result.append(more)
        }
        return result
    }

    private var ink: Color { onDark ? .white.opacity(0.88) : Semantic.ink }
    private var secondaryInk: Color { onDark ? .white.opacity(0.5) : Semantic.secondaryInk }

    private func colour(_ line: Substring) -> Color {
        if line.hasPrefix("+++") || line.hasPrefix("---") || line.hasPrefix("@@") || line.hasPrefix("*** ") { return secondaryInk }
        if line.hasPrefix("+") { return Color(nsColor: .systemGreen) }
        if line.hasPrefix("-") { return Color(nsColor: .systemRed) }
        return ink
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
        .padding(.vertical, 6)
        .accessibilityElement(children: .contain)
    }
}

struct NoticeRow: View {
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
        .padding(.horizontal, 20).padding(.top, 12).padding(.bottom, 6)
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
    /// The workspace waiting on a first-time YOLO confirmation, while shown.
    @State private var yoloWorkspace: String?

    var body: some View {
        VStack(spacing: 5) {
            VStack(alignment: .leading, spacing: 4) {
                if !model.attachments.isEmpty {
                    ChatAttachmentStrip(attachments: model.attachments, remove: model.removeAttachment)
                }
                HStack(alignment: .bottom, spacing: 8) {
                    Button { model.chooseAttachments() } label: {
                        Image(systemName: "plus").font(.system(size: 15, weight: .medium)).foregroundStyle(Semantic.secondaryInk)
                            .frame(width: 23, height: 24).contentShape(Rectangle())
                    }.buttonStyle(.plain).disabled(!editable).help("Attach files or images")
                        .accessibilityLabel("Attach files or images")
                    ComposerTextView(text: $model.draft, height: $height, placeholder: placeholder, enabled: editable,
                                     onSend: { model.send() }, onPasteFiles: { urls in
                        guard editable else { return false }
                        model.addAttachments(urls); return true
                    })
                        .frame(height: height)
                        .accessibilityLabel("Message")
                    if model.busy {
                        if !model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !model.attachments.isEmpty { interruptButton }
                        stopButton
                    } else { sendButton }
                }
            }
            .padding(.leading, 14).padding(.trailing, 7).padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: HubTheme.Radius.panel).fill(Semantic.raisedSurface))
            .overlay(RoundedRectangle(cornerRadius: HubTheme.Radius.panel)
                .stroke(yolo ? Semantic.approvalYolo.opacity(0.4) : Semantic.hairline, lineWidth: 1))
            footer
        }
        .padding(.horizontal, 16).padding(.top, 8).padding(.bottom, 10)
        .confirmationDialog("Run tools without approval prompts?", isPresented: Binding(
            get: { yoloWorkspace != nil }, set: { if !$0 { yoloWorkspace = nil } }
        ), titleVisibility: .visible) {
            Button("Use YOLO") {
                if let yoloWorkspace { ChatYoloAcknowledgement.record(yoloWorkspace); model.setApprovalMode("yolo") }
                yoloWorkspace = nil
            }
            Button("Cancel", role: .cancel) { yoloWorkspace = nil }
        } message: {
            Text("YOLO edits files and runs commands in \(folderName(yoloWorkspace ?? "")) without asking first. This is asked once per folder.")
        }
    }

    private var editable: Bool { model.connected && model.selectedID != nil }
    /// YOLO is the one mode worth marking on the composer itself: the outline
    /// takes a faint red, the way TaskWraith tints full access.
    private var yolo: Bool { model.selectedID != nil && model.approvalMode == "yolo" }

    private var placeholder: String {
        guard model.selectedID != nil else { return "Start a new chat to begin" }
        guard model.connected else { return "Connecting…" }
        guard let route = model.selectedRoute else { return "Choose a model above" }
        return "Message " + route.label + "…"
    }

    private var sendButton: some View {
        Button { model.send() } label: {
            Image(systemName: "arrow.up").font(.system(size: 13, weight: .bold)).foregroundStyle(Semantic.surface)
                .frame(width: 28, height: 28).background(Circle().fill(Semantic.ink))
        }
        .buttonStyle(.plain).disabled(!model.canSend).opacity(model.canSend ? 1 : 0.35)
        .accessibilityLabel("Send").help("Send (Return)")
    }

    private var stopButton: some View {
        Button { model.stop() } label: {
            Image(systemName: "stop.fill").font(.system(size: 11, weight: .bold)).foregroundStyle(Semantic.surface)
                .frame(width: 28, height: 28).background(Circle().fill(Semantic.ink))
        }
        .buttonStyle(.plain).keyboardShortcut(".", modifiers: .command)
        .accessibilityLabel("Stop").help("Stop and keep what has arrived (⌘.)")
    }
    private var interruptButton: some View {
        Button { model.send() } label: {
            Image(systemName: "arrow.uturn.forward").font(.system(size: 13, weight: .semibold)).foregroundStyle(model.activeAccent)
                .frame(width: 28, height: 28).contentShape(Rectangle())
        }.buttonStyle(.plain).disabled(!model.canInterrupt)
            .help("Interrupt and send this update (Return)").accessibilityLabel("Interrupt and send update")
    }

    private var footer: some View {
        HStack(alignment: .top) {
            approvalModeMenu
            if model.notice.isEmpty {
                ChatShimmerText(text: model.status, active: model.busy && model.approval == nil, accent: model.activeAccent)
                    .font(HubTheme.Typography.detail)
            } else {
                Text(model.notice).font(HubTheme.Typography.detail).foregroundStyle(Semantic.accentOnSurface).textSelection(.enabled)
            }
            Spacer(minLength: 8)
            ChatTurnTime(model: model)
        }
        .lineLimit(2).padding(.horizontal, 6).frame(minHeight: 16)
    }

    private var approvalModeMenu: some View {
        let modes = [("manual", "Manual"), ("accept_edits", "Accept Edits"), ("yolo", "YOLO")]
        let title = modes.first { $0.0 == model.approvalMode }?.1 ?? "Manual"
        let accent = ChatApprovalAccent.color(model.approvalMode)
        return Menu {
            ForEach(modes, id: \.0) { mode in
                Button { chooseApprovalMode(mode.0) } label: {
                    if model.approvalMode == mode.0 { Label(mode.1, systemImage: "checkmark") }
                    else { Text(mode.1) }
                }
            }
        } label: {
            HStack(spacing: 5) {
                Circle().fill(accent).frame(width: 6, height: 6)
                Text(title).font(HubTheme.Typography.detail).foregroundStyle(accent)
            }
        }
        .menuStyle(.borderlessButton).fixedSize()
        .disabled(model.busy || model.selectedID == nil)
        .accessibilityLabel("Approval mode: " + title)
        .help("Manual asks for edits and commands. Accept Edits allows repository patches and asks for commands. YOLO runs tools without prompts.")
    }

    /// YOLO asks once per folder before it is first used there; every other
    /// change, and a repeat choice, applies at once.
    private func chooseApprovalMode(_ mode: String) {
        if mode == "yolo", let workspace = model.selected?.workspace, !ChatYoloAcknowledgement.contains(workspace) {
            yoloWorkspace = workspace; return
        }
        model.setApprovalMode(mode)
    }
}

/// A plain NSTextView: Return and ⌘Return send, Shift-Return and Option-Return
/// insert a line, and the view grows with its text up to a few lines.
struct ComposerTextView: NSViewRepresentable {
    @Environment(\.chatTextStyle) private var textStyle
    @Binding var text: String
    @Binding var height: CGFloat
    var placeholder: String
    var enabled: Bool
    var onSend: () -> Void
    /// Files or an image on the pasteboard; return true to consume the paste.
    var onPasteFiles: (([URL]) -> Bool)? = nil

    private static let minHeight: CGFloat = 22
    private static let maxHeight: CGFloat = 160

    final class SendTextView: NSTextView {
        var onSend: (() -> Void)?
        var onPasteFiles: (([URL]) -> Bool)?
        var placeholder = "" { didSet { if placeholder != oldValue { needsDisplay = true } } }

        override func keyDown(with event: NSEvent) {
            let command = event.modifierFlags.intersection(.deviceIndependentFlagsMask).contains(.command)
            if event.keyCode == 36, command, isEditable { onSend?(); return }
            super.keyDown(with: event)
        }

        /// Pasted files and images become attachments, matching + and drop.
        /// An image with no file behind it (a copied screenshot) is written to
        /// a temporary PNG first. Text still pastes as text.
        override func paste(_ sender: Any?) {
            if isEditable, let onPasteFiles, let urls = Self.pastedFiles(NSPasteboard.general), onPasteFiles(urls) { return }
            super.paste(sender)
        }

        static func pastedFiles(_ board: NSPasteboard) -> [URL]? {
            if let urls = board.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL], !urls.isEmpty {
                return urls
            }
            // Text wins over an image unless the text is only the image's URL.
            let text = board.string(forType: .string)?.trimmingCharacters(in: .whitespacesAndNewlines)
            let textIsLink = text.map { $0.range(of: #"^https?://\S+$"#, options: .regularExpression) != nil } ?? true
            guard textIsLink, let image = NSImage(pasteboard: board), let tiff = image.tiffRepresentation,
                  let bitmap = NSBitmapImageRep(data: tiff), let png = bitmap.representation(using: .png, properties: [:]) else { return nil }
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent("ProviderHubChatPaste", isDirectory: true)
            let formatter = DateFormatter(); formatter.dateFormat = "yyyy-MM-dd 'at' HH.mm.ss"
            let stem = "Pasted image " + formatter.string(from: Date())
            do {
                try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
                var url = directory.appendingPathComponent(stem + ".png")
                var copy = 2
                while FileManager.default.fileExists(atPath: url.path) { url = directory.appendingPathComponent("\(stem) \(copy).png"); copy += 1 }
                try png.write(to: url)
                return [url]
            } catch { return nil }
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
        view.onPasteFiles = onPasteFiles
        view.placeholder = placeholder
        view.font = textStyle.editorFont
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
