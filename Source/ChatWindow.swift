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
        VStack(spacing: 0) {
            titleBar
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
        }
        .frame(minWidth: 720, minHeight: 500)
        .environment(\.chatTextStyle, ChatTextStyle(size: textSize,
            selection: ChatFonts.selection(fontChoice, legacyMonospaced: monospacedText), customName: customFontName))
        .background {
            if windowStyle == .glass { VibrancyBackground().ignoresSafeArea() }
            else { Color(nsColor: .windowBackgroundColor).ignoresSafeArea() }
        }
        // Own the hidden title-bar band, as CompactShell does, so the masthead
        // shares the traffic-light row rather than sitting beneath it.
        .ignoresSafeArea()
        .dropDestination(for: URL.self, action: { urls, _ in acceptFolder(urls) }, isTargeted: { dropTargeted = $0 })
        .overlay { if dropTargeted { dropHint } }
    }

    private var titleBar: some View {
        HStack(spacing: 8) {
            Image(nsImage: NSApp.applicationIconImage).resizable()
                .frame(width: 18, height: 18).clipShape(RoundedRectangle(cornerRadius: 5))
                .accessibilityHidden(true)
            Text("Provider Hub").font(.system(size: 12, weight: .semibold))
            HubUpdatePill()
            Spacer()
        }
        .padding(.leading, 78).padding(.trailing, 12).frame(height: 36)
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
    @State private var showingChats = false

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: HubTheme.Spacing.sm) {
                railButton
                newChatButton
                if model.selectedID != nil {
                    if model.team?.enabled == true {
                        Button { model.inspectedAgentID = nil; model.showInspector(.agents) } label: {
                            Label("Team · \(model.team?.members.count ?? 0)", systemImage: "person.3")
                        }.buttonStyle(.plain).font(HubTheme.Typography.rowTitle).help("Team members and progress")
                    } else { modelMenu }
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
        Button {
            if railAvailable { toggleRail() } else { showingChats.toggle() }
        } label: {
            Image(systemName: "sidebar.left").font(.system(size: 13)).foregroundStyle(Semantic.secondaryInk)
                .frame(width: 24, height: 22).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .keyboardShortcut("s", modifiers: [.command, .control])
        .accessibilityLabel(railVisible ? "Hide saved chats" : "Show saved chats")
        .help(railVisible ? "Hide saved chats (⌃⌘S)" : "Show saved chats (⌃⌘S)")
        .popover(isPresented: $showingChats, arrowEdge: .bottom) {
            ChatWorkspaceRail(model: model).frame(width: 260, height: 420)
        }
        .onChange(of: model.selectedID) { _, _ in showingChats = false }
    }

    private var newChatButton: some View {
        Button { model.newChat() } label: {
            Image(systemName: "square.and.pencil").font(.system(size: 13)).foregroundStyle(Semantic.secondaryInk)
                .frame(width: 24, height: 22).contentShape(Rectangle())
        }
        .buttonStyle(.plain).disabled(!model.connected)
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
        if let team = model.team, team.enabled, model.busy || team.status == "working" {
            // Every member's own window while the Team works; they are never
            // added together. The parent's returns once the Team stops.
            HStack(spacing: 10) {
                ForEach(team.members) { member in
                    memberContext(member, active: member.status == "working" || member.id == team.activeMemberID)
                }
            }
        } else if let used = model.tokenUsage {
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

    private func memberContext(_ member: ChatTeamMember, active: Bool) -> some View {
        let accent = model.accent(for: member.route)
        let readout = member.usage.map { tokenCount($0) + (member.context.map { " / " + tokenCount($0) } ?? "") } ?? "not started"
        return HStack(spacing: 5) {
            if let used = member.usage, let limit = member.context, limit > 0 {
                ChatContextRing(used: used, limit: limit, accent: accent)
            } else {
                Circle().stroke(accent.opacity(0.3), lineWidth: 2).frame(width: 12, height: 12)
            }
            Text(member.name).font(.system(size: 10.5, weight: active ? .semibold : .regular))
                .foregroundStyle(active ? Semantic.ink : Semantic.secondaryInk)
            if let used = member.usage {
                Text(tokenCount(used)).font(.system(size: 10.5, design: .monospaced)).foregroundStyle(Semantic.secondaryInk)
            }
        }
        .lineLimit(1).fixedSize()
        .help(member.name + " · " + readout + (active ? " · working" : "") + ". Each member has its own context window.")
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(member.name + " context " + readout + (active ? ", working" : ""))
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
                .disabled(!model.connected || model.models.isEmpty)
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
    @Environment(\.chatTextStyle) private var textStyle
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
        let lastID = model.entries.last?.id
        let segments = ChatTranscriptSegment<ChatEntry>.outline(model.entries) {
            model.isStreaming($0, fallback: model.busy && $0.id == lastID)
        }
        let liveSpeaker = model.busy ? segments.last(where: \.isSpeaker)?.id : nil
        return LazyVStack(alignment: .leading, spacing: 0) {
            if model.entries.isEmpty { ChatInvitation(model: model) }
            ForEach(segments) { segment in
                Group {
                    switch segment.content {
                    case .speaker(let entry):
                        ChatSpeakerHeader(member: entry.memberName, label: model.label(for: entry.route),
                                          presentation: model.route(named: entry.route)?.presentation,
                                          accent: model.accent(for: entry.route), live: segment.id == liveSpeaker)
                    case .entry(let entry): row(entry, after: segment.previousSpeaker)
                    case .fold(let entries): fold(entries, live: model.busy && segment.id == segments.last?.id)
                    }
                }
                .padding(.top, gap(segment.spacing))
            }
            Color.clear.frame(height: 1).id(bottomID)
        }
        .frame(maxWidth: textStyle.scaled(760)).frame(maxWidth: .infinity)
        .padding(.horizontal, 20).padding(.vertical, 16)
    }

    /// Wide between speakers, tight inside one speaker's block.
    private func gap(_ spacing: ChatTranscriptSegment<ChatEntry>.Spacing) -> CGFloat {
        switch spacing {
        case .none: return 0
        case .speaker: return textStyle.scaled(26)
        case .header: return textStyle.scaled(6)
        case .item: return textStyle.scaled(6)
        }
    }

    private func scrollToEnd(_ proxy: ScrollViewProxy) {
        proxy.scrollTo(bottomID, anchor: .bottom)
    }

    @ViewBuilder private func row(_ entry: ChatEntry, after speaker: ChatEntry? = nil) -> some View {
        let last = entry.id == model.entries.last?.id
        let live = model.busy && entry.detail == "Running…"
        switch entry.kind {
        case "user":
            UserRow(entry: entry)
        case "assistant":
            AssistantRow(entry: entry, accent: model.accent(for: entry.route), streaming: model.isStreaming(entry, fallback: model.busy && last))
        case "tool":
            if let ids = entry.agentIDs, !ids.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ToolRow(entry: entry, accent: model.accent(for: entry.route), expanded: expandedBinding(entry.id), workspace: entry.workspace ?? model.selected?.workspace, onInspect: {
                        model.inspectedAgentID = nil; model.showInspector(.agents)
                    }, running: model.agents.contains { ids.contains($0.id) && $0.busy }, live: live)
                    ChatParallelLanes(model: model, agentIDs: ids).padding(.leading, 18)
                }
            } else if let agentID = entry.agentID {
                ToolRow(entry: entry, accent: model.accent(for: entry.route), expanded: expandedBinding(entry.id), workspace: model.selected?.workspace, onInspect: {
                    model.inspectedAgentID = agentID; model.showInspector(.agents)
                }, running: model.agents.first { $0.id == agentID }?.busy == true, live: live)
            } else {
                ToolRow(entry: entry, accent: model.accent(for: entry.route), expanded: expandedBinding(entry.id),
                        workspace: entry.workspace ?? model.selected?.workspace, live: live)
            }
        case "error":
            ErrorRow(entry: entry, canRetry: last && !model.busy && model.connected) { model.retry() }
        default:
            NoticeRow(entry: entry, member: speaker?.memberName)
        }
    }

    private func expandedBinding(_ id: String) -> Binding<Bool> {
        Binding(get: { expanded.contains(id) }, set: { if $0 { expanded.insert(id) } else { expanded.remove(id) } })
    }

    /// A run of local tool rows behind one disclosure. The live tail run stays
    /// open so progress is visible; once the reply begins it folds, and a
    /// choice the user makes by hand sticks for that run. Collapsed and live,
    /// the header carries the latest step so nothing goes dark mid-turn. Open,
    /// the steps hang from a hairline under the header's glyphs.
    @ViewBuilder private func fold(_ entries: [ChatEntry], live: Bool) -> some View {
        let id = ChatTranscriptSegment<ChatEntry>.foldID(entries)
        let open = foldChoice[id] ?? live
        VStack(alignment: .leading, spacing: textStyle.scaled(4)) {
            Button {
                if reduceMotion { foldChoice[id] = !open } else { withAnimation(.easeInOut(duration: 0.15)) { foldChoice[id] = !open } }
            } label: {
                ChatFoldHeader(entries: entries, live: live, open: open, accent: model.accent(for: entries.first?.route ?? ""))
            }
            .buttonStyle(.plain)
            .accessibilityLabel((live ? "Working, " : "Worked, ") + ChatToolDisplay.activity(entries.map(\.tool)))
            .accessibilityValue(open ? "Expanded" : "Collapsed")
            if open {
                VStack(alignment: .leading, spacing: textStyle.scaled(2)) { ForEach(entries) { entry in row(entry) } }
                    .padding(.leading, textStyle.scaled(18))
                    .overlay(alignment: .leading) {
                        Rectangle().fill(Semantic.hairline).frame(width: 1).padding(.leading, textStyle.scaled(7)).padding(.vertical, 2)
                    }
            }
        }
    }
}

private struct ErrorRow: View {
    var entry: ChatEntry
    var canRetry: Bool
    var retry: () -> Void
    @Environment(\.chatTextStyle) private var textStyle

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill").font(textStyle.system(11)).foregroundStyle(Color(nsColor: .systemRed)).padding(.top, 2)
            Text(entry.text).font(textStyle.system(13)).foregroundStyle(Semantic.ink).textSelection(.enabled)
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
                Button("Deny") { model.decideApproval(allow: false, target: approval) }
                    .controlSize(.small).keyboardShortcut("d", modifiers: .command).help("Deny (⌘D)")
                Button("Allow once") { model.decideApproval(allow: true, target: approval) }
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
    @State private var yoloChatID: String?

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
            get: { yoloWorkspace != nil }, set: { if !$0 { yoloWorkspace = nil; yoloChatID = nil } }
        ), titleVisibility: .visible) {
            Button("Use YOLO") {
                if let yoloWorkspace, let yoloChatID {
                    ChatYoloAcknowledgement.record(yoloWorkspace)
                    model.setApprovalMode("yolo", chat: yoloChatID, workspace: yoloWorkspace)
                }
                yoloWorkspace = nil; yoloChatID = nil
            }
            Button("Cancel", role: .cancel) { yoloWorkspace = nil; yoloChatID = nil }
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
            yoloWorkspace = workspace; yoloChatID = model.selectedID; return
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
