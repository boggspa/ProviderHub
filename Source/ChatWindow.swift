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
    @AppStorage("chatRailWidth") private var railWidth = 190.0
    @AppStorage("chatInspectorWidth") private var inspectorWidth = 0.0
    @AppStorage(HubTheme.WindowStyle.defaultsKey) private var windowStyle = HubTheme.WindowStyle.Mode.glass
    @AppStorage("chatMonospacedText") private var monospacedText = false
    @AppStorage("chatFontChoice") private var fontChoice = ""
    @AppStorage("chatCustomFontName") private var customFontName = ""
    @AppStorage("chatTextSize") private var textSize = 13.0
    @AppStorage(ChatZoom.defaultsKey) private var zoom = 1.0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var dropTargeted = false
    @StateObject private var workspace = ChatWorkspaceController()
    @StateObject private var browser = ChatBrowserStore(storageRoot: FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent(Bundle.main.object(forInfoDictionaryKey: "BridgeStateName") as? String ?? "Provider Hub", isDirectory: true)
        .appendingPathComponent("browser", isDirectory: true))

    var body: some View {
        VStack(spacing: 0) {
            titleBar
            GeometryReader { proxy in
                let panes = ChatPaneSizing.layout(window: proxy.size.width, railPreferred: railPreferred,
                    inspectorVisible: model.inspectorVisible, rail: railWidth, inspector: inspectorWidth)
                HStack(spacing: 0) {
                    if panes.rail > 0 {
                        ChatWorkspaceRail(model: model).hubGlassPane(.sidebar).frame(width: panes.rail)
                            .environment(\.chatPaneOpener, ChatPaneOpener(openChats: workspace.chatIDs, split: workspace.layout.panes.count > 1) { chat, below in
                                workspace.open(chat: chat, edge: below ? .bottom : .right, model: model)
                            })
                        ChatPaneDivider(label: "Workspace rail width", width: panes.rail, direction: 1,
                            resize: { railWidth = Double(ChatPaneSizing.clamp($0, 150, 360)) },
                            reset: { railWidth = Double(ChatPaneSizing.railDefault) })
                    }
                    ChatWorkspaceContent(model: model, workspace: workspace, browser: browser,
                                         railVisible: panes.rail > 0, railAvailable: panes.railAvailable) { railPreferred.toggle() }
                    if model.inspectorVisible {
                        ChatPaneDivider(label: "Inspector width", width: panes.inspector, direction: -1,
                            resize: { inspectorWidth = Double(ChatPaneSizing.clamp($0, ChatPaneSizing.inspectorMinimum,
                                ChatPaneSizing.inspectorMaximum(proxy.size.width))) },
                            reset: { inspectorWidth = 0 })
                        ChatInspector(model: model, browserStore: browser,
                                      browserInPane: model.selectedID.flatMap { workspace.browserPane(for: $0) } != nil)
                            .hubGlassPane(.inspector)
                            .frame(width: panes.inspector)
                    }
                }
                .animation(reduceMotion ? nil : .easeInOut(duration: 0.18), value: railPreferred)
                .animation(reduceMotion ? nil : .easeInOut(duration: 0.18), value: model.inspectorVisible)
            }
        }
        .frame(minWidth: 720, minHeight: 500)
        .environment(\.hubSurfaceStyle, windowStyle)
        .environment(\.chatTextStyle, ChatTextStyle(size: textSize,
            selection: ChatFonts.selection(fontChoice, legacyMonospaced: monospacedText), customName: customFontName,
            zoom: ChatZoom.clamp(zoom)))
        .chatHues()
        .background {
            if windowStyle == .glass { VibrancyBackground().overlay(Semantic.glassScrim).ignoresSafeArea() }
            else { Color(nsColor: .windowBackgroundColor).ignoresSafeArea() }
        }
        // Own the hidden title-bar band, as CompactShell does, so the masthead
        // shares the traffic-light row rather than sitting beneath it.
        .ignoresSafeArea()
        .dropDestination(for: URL.self, action: { urls, _ in acceptFolder(urls) }, isTargeted: { dropTargeted = $0 })
        .overlay { if dropTargeted { dropHint } }
        .onAppear {
            if !model.chats.isEmpty { workspace.reconcile(model) }
            model.nativeActionHandler = { event in
                guard model.browserUseEnabled, let request = event["id"] as? String, let chat = event["chat"] as? String,
                      model.chats.contains(where: { $0.id == chat }), let tool = event["tool"] as? String,
                      let arguments = event["arguments"] as? [String: Any] else {
                    return ChatBrowserResult.failure("Browser unavailable", "Browser Use is disabled or its chat is unavailable.")
                }
                if model.selectedID == chat, workspace.browserPane(for: chat) == nil { model.showInspector(.browser) }
                return await browser.perform(chatID: chat, requestID: request, tool: tool, arguments: arguments)
            }
            model.nativeCancelHandler = { request, chat in browser.cancel(chatID: chat, requestID: request) }
        }
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
                    .hubCard(cornerRadius: HubTheme.Radius.row, accent: model.activeAccent)
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

extension ChatModel {
    func route(named route: String) -> ChatRoute? { models.first { $0.route == route } }
    /// Only a provider with branding metadata may colour the transcript.
    fileprivate func isKnownRoute(_ route: String) -> Bool { self.route(named: route)?.presentation != nil }
    func label(for route: String) -> String { self.route(named: route)?.label ?? route }
    fileprivate func providerTitle(_ route: ChatRoute) -> String { route.presentation?.displayProvider ?? route.provider }
    fileprivate func hasSeveralAccounts(_ provider: String) -> Bool {
        Set(models.filter { $0.provider == provider }.map(\.account)).count > 1
    }
}

private func shortPath(_ path: String) -> String { (path as NSString).abbreviatingWithTildeInPath }
private func folderName(_ path: String) -> String {
    let name = (path as NSString).lastPathComponent
    return name.isEmpty ? shortPath(path) : name
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

struct ChatPane: View {
    @ObservedObject var model: ChatModel
    var railVisible: Bool
    var railAvailable: Bool
    var compactHeader: Bool
    var toggleRail: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            ChatHeader(model: model, railVisible: railVisible, railAvailable: railAvailable, compact: compactHeader, toggleRail: toggleRail)
            Rectangle().fill(Semantic.hairline).frame(height: HubTheme.Separator.width)
            // One sheet of paper from the header rule to the window's bottom
            // edge: the live strip and the composer sit on the transcript
            // rather than on a band of glass under a hard edge.
            VStack(spacing: 0) {
                if model.selectedID == nil { ChatWelcome(model: model) }
                else { ChatTranscript(model: model) }
                if model.selectedID != nil { ChatActivityStrip(model: model) }
                if let approval = model.approval { ApprovalStrip(model: model, approval: approval) }
                ChatComposer(model: model)
            }
            .hubGlassPane(.transcript)
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
    private var workspaceOnSecondRow: Bool { compact || model.team?.enabled == true }

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
                    if !workspaceOnSecondRow { workspaceControls }
                }
                Spacer(minLength: 4)
                usage
                connection
                ChatZoomControl()
                inspectorButton
            }
            if workspaceOnSecondRow, model.selectedID != nil { workspaceControls.padding(.leading, 4) }
        }
        .padding(.horizontal, HubTheme.Spacing.md).padding(.vertical, 7)
    }

    private var workspaceControls: some View {
        HStack(spacing: 9) {
            folderMenu
            ChatBranchChip(model: model)
            Spacer(minLength: 0)
            ChatGitIndicator(model: model).contentShape(Rectangle())
                .layoutPriority(1)
                .onTapGesture { model.showInspector(.changes) }
                .accessibilityAction(named: Text("Inspect file changes")) { model.showInspector(.changes) }
        }
    }

    private var inspectorButton: some View {
        Button { model.inspectorVisible.toggle() } label: {
            Image(systemName: "sidebar.right").font(.system(size: 13))
                .foregroundStyle(model.inspectorVisible ? Semantic.ink : Semantic.secondaryInk)
                .frame(width: 24, height: 22).contentShape(Rectangle())
        }.buttonStyle(.plain).keyboardShortcut(model.hasPaneFocus ? KeyboardShortcut("i", modifiers: [.command, .control]) : nil)
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
        .keyboardShortcut(model.hasPaneFocus ? KeyboardShortcut("s", modifiers: [.command, .control]) : nil)
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
        .keyboardShortcut(model.hasPaneFocus ? KeyboardShortcut("n", modifiers: .command) : nil)
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
                    Text("· " + ChatSharedEffort.title(effort)).font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
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
            ViewThatFits(in: .horizontal) {
                teamContext(team, named: true)
                teamContext(team, named: false)
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

    private func teamContext(_ team: ChatTeamSnapshot, named: Bool) -> some View {
        HStack(spacing: 10) {
            ForEach(team.members) { member in
                memberContext(member, active: member.status == "working" || member.id == team.activeMemberID,
                              named: named, counted: named && (!compact || team.members.count < 4),
                              capped: compact && team.members.count > 3)
            }
        }
    }

    /// A ring and name per member; the token count drops out on a narrow
    /// header with a larger Team. On a compact header with four or five
    /// members the names also yield and truncate to keep every ring visible;
    /// both remain in the tooltip and accessibility label.
    private func memberContext(_ member: ChatTeamMember, active: Bool, named: Bool, counted: Bool, capped: Bool) -> some View {
        let accent = model.accent(for: member.route)
        let readout = member.usage.map { tokenCount($0) + (member.context.map { " / " + tokenCount($0) } ?? "") } ?? "not started"
        return ChatSharedMemberChip(name: member.name, model: member.label, used: member.usage, limit: member.context,
            active: active, named: named, counted: counted, capped: capped,
            accent: accent, ink: Semantic.ink, secondary: Semantic.secondaryInk) {
                ChatProviderIcon(presentation: model.models.first(where: { $0.id == member.choice })?.presentation, size: 13)
        }
        .lineLimit(1)
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

/// "− 100% +" in the header: the reading zoom for transcript and composer
/// text. The percentage resets to 100%. The View menu offers the same steps
/// as ⌘+, ⌘− and ⌘0.
private struct ChatZoomControl: View {
    @AppStorage(ChatZoom.defaultsKey) private var zoom = 1.0

    var body: some View {
        let current = ChatZoom.clamp(zoom)
        HStack(spacing: 0) {
            step("minus", label: "Zoom out", help: "Smaller text (⌘−)", enabled: current > ChatZoom.steps[0]) { zoom = ChatZoom.smaller(zoom) }
            Button { zoom = 1 } label: {
                Text(ChatZoom.percent(current)).font(.system(size: 10.5, design: .monospaced))
                    .foregroundStyle(current == 1 ? Semantic.secondaryInk : Semantic.ink)
                    .frame(minWidth: 34).frame(height: 20).contentShape(Rectangle())
            }
            .buttonStyle(.plain).help("Text zoom · click for actual size (⌘0)")
            .accessibilityLabel("Text zoom \(ChatZoom.percent(current))").accessibilityHint("Resets to 100%")
            step("plus", label: "Zoom in", help: "Larger text (⌘+)", enabled: current < ChatZoom.steps[ChatZoom.steps.count - 1]) { zoom = ChatZoom.larger(zoom) }
        }
        .background(Capsule().fill(Semantic.raisedSurface))
        .fixedSize()
    }

    private func step(_ icon: String, label: String, help: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: icon).font(.system(size: 9, weight: .semibold)).foregroundStyle(Semantic.secondaryInk)
                .frame(width: 20, height: 20).contentShape(Rectangle())
        }
        .buttonStyle(.plain).disabled(!enabled).opacity(enabled ? 1 : 0.35)
        .help(help).accessibilityLabel(label)
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

@MainActor
struct ChatTranscript: View {
    @ObservedObject var model: ChatModel
    @State private var expanded: Set<String> = []
    /// Explicit open/closed choices per fold; a fold without one stays closed
    /// unless it holds a row the user expanded.
    @State private var foldChoice: [String: Bool] = [:]
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.chatTextStyle) private var textStyle
    @StateObject private var follow = ChatTranscriptFollow()
    @State private var visibleSegmentLimit = 100
    private let bottomID = "chat-transcript-bottom"

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                content(proxy).background(ChatTranscriptScrollObserver(follow: follow))
            }
            .onChange(of: model.transcriptVersion) { _, _ in scrollToEnd(proxy) }
            // Back at the latest activity, history paged in for reading goes
            // again, so the eager stack stays one page tall while it streams.
            .onChange(of: follow.followsLatest) { _, follows in if follows { visibleSegmentLimit = 100 } }
            .onChange(of: model.jumpRequest) { _, request in
                if let request { jump(to: request.id, proxy: proxy) }
            }
            .onChange(of: model.selectedID) { _, _ in
                visibleSegmentLimit = 100; follow.resume()
                scrollToEnd(proxy)
            }
            .onAppear { scrollToEnd(proxy) }
            .onDisappear { follow.cancel() }
            .overlay(alignment: .bottomTrailing) {
                if !follow.followsLatest {
                    Button {
                        follow.resume(); scrollToEnd(proxy)
                    } label: {
                        Label("Latest", systemImage: "arrow.down").font(.system(size: 11.5, weight: .medium))
                            .padding(.horizontal, 11).padding(.vertical, 7)
                            .background(.regularMaterial, in: Capsule())
                    }.buttonStyle(.plain).padding(12).accessibilityLabel("Go to latest activity")
                }
            }
        }
    }

    private func content(_ proxy: ScrollViewProxy) -> some View {
        let liveEntryIDs = model.streamingEntryIDs()
        // While a turn runs its close-out waits; it appears once the turn ends.
        let segments = ChatTranscriptSegment<ChatEntry>.outline(model.entries, open: model.busy) {
            liveEntryIDs.contains($0.id)
        }
        // Streaming belongs to a member's current section, which can sit
        // above sections opened by peers. Do not assume the bottom row is live.
        var liveSpeakers: Set<String> = []
        var currentSpeaker: String?
        for segment in segments {
            switch segment.content {
            case .speaker: currentSpeaker = segment.id
            case .entry(let entry):
                if liveEntryIDs.contains(entry.id), let currentSpeaker { liveSpeakers.insert(currentSpeaker) }
            case .fold(let entries):
                if entries.contains(where: { liveEntryIDs.contains($0.id) }), let currentSpeaker { liveSpeakers.insert(currentSpeaker) }
            case .changes: currentSpeaker = nil
            }
        }
        if model.team?.enabled != true, model.busy, let speaker = segments.last(where: \.isSpeaker) {
            liveSpeakers.insert(speaker.id)
        }
        let visible = ChatTranscriptSegment<ChatEntry>.visibleTail(segments, limit: visibleSegmentLimit, liveEntryIDs: liveEntryIDs)
        // Eager, not lazy: a lazy stack only estimates the height of rows it
        // has not drawn, so when a Team member's section above the viewport
        // shrinks or leaves the tail, the viewport can land past the rows it
        // drew and show nothing until a scroll makes it measure again, and a
        // follow aimed at the estimated end stops short of it. The tail is
        // paged, so every row it holds is measured exactly.
        return VStack(alignment: .leading, spacing: 0) {
            if model.entries.isEmpty { ChatInvitation(model: model) }
            if segments.count > visibleSegmentLimit {
                Button("Load earlier messages") {
                    follow.pause()
                    visibleSegmentLimit += 100
                    // Rows arrive above the one being read; keep it in place.
                    if let first = visible.first?.id { DispatchQueue.main.async { proxy.scrollTo(first, anchor: .top) } }
                }
                .frame(maxWidth: .infinity).padding(.bottom, 16)
            }
            ForEach(visible) { segment in
                Group {
                    switch segment.content {
                    case .speaker(let entry):
                        ChatSpeakerHeader(member: entry.memberName, time: entry.time, label: model.label(for: entry.route),
                                          presentation: model.route(named: entry.route)?.presentation,
                                          accent: model.accent(for: entry.route), live: liveSpeakers.contains(segment.id))
                    case .entry(let entry): row(entry, after: segment.previousSpeaker, liveEntryIDs: liveEntryIDs)
                    case .fold(let entries):
                        fold(entries, live: model.team?.enabled == true
                             ? entries.contains { liveEntryIDs.contains($0.id) }
                             : model.busy && segment.id == segments.last?.id, liveEntryIDs: liveEntryIDs)
                    case .changes(let entries):
                        ChatTurnChangesRow(entries: entries, workspace: model.selected?.workspace) { expandedBinding($0) }
                    }
                }
                .padding(.top, gap(segment.spacing))
            }
            // Headroom: the follow target is this spacer's bottom edge, so the
            // newest row sits a little above the composer instead of against
            // it. Growing the spacer moves nothing else: scrollToEnd still
            // anchors the same id, and the observer still measures the
            // document's end, so following engages and disengages as before.
            Color.clear.frame(height: textStyle.scaled(36)).id(bottomID)
        }
        .frame(maxWidth: textStyle.scaled(760)).frame(maxWidth: .infinity)
        .padding(.horizontal, 20).padding(.top, 16).padding(.bottom, 4)
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
        follow.request { proxy.scrollTo(bottomID, anchor: .bottom) }
    }

    /// Show the segment holding an entry: its own row, or the fold it sits
    /// in, which opens so the row is visible. The tail is paged out far
    /// enough to include it first. The user asked for this position, so
    /// following pauses as it does for a manual scroll; Latest resumes it.
    private func jump(to entryID: String, proxy: ScrollViewProxy) {
        let liveEntryIDs = model.streamingEntryIDs()
        let segments = ChatTranscriptSegment<ChatEntry>.outline(model.entries, open: model.busy) { liveEntryIDs.contains($0.id) }
        guard let index = segments.firstIndex(where: { segment in
            switch segment.content {
            case .entry(let entry): return entry.id == entryID
            case .fold(let entries): return entries.contains { $0.id == entryID }
            default: return false
            }
        }) else { return }
        let target = segments[index].id
        var inFold = false
        follow.pause()
        if case .fold = segments[index].content { foldChoice[target] = true; inFold = true }
        if segments.count - index > visibleSegmentLimit { visibleSegmentLimit = segments.count - index + 20 }
        func scroll(to id: String) {
            if reduceMotion { proxy.scrollTo(id, anchor: .center) }
            else { withAnimation(.easeInOut(duration: 0.2)) { proxy.scrollTo(id, anchor: .center) } }
        }
        // After the paging and fold state above have laid out: the segment
        // first, then, once an opened fold has its rows, the row itself, so a
        // tall fold never leaves the current row off screen.
        DispatchQueue.main.async {
            scroll(to: target)
            if inFold { DispatchQueue.main.async { scroll(to: entryID) } }
        }
    }

    @ViewBuilder private func row(_ entry: ChatEntry, after speaker: ChatEntry? = nil, liveEntryIDs: Set<String>) -> some View {
        let last = entry.id == model.entries.last?.id
        let live = model.busy && entry.detail == "Running…"
        let canPin = model.team?.enabled == true && model.connected && ["user", "assistant", "tool"].contains(entry.kind)
            && !live && (!entry.text.isEmpty || entry.detail?.isEmpty == false)
        let canReact = model.connected && ["user", "assistant"].contains(entry.kind) && !liveEntryIDs.contains(entry.id)
            && (!entry.text.isEmpty || entry.attachments?.isEmpty == false)
        let react: ((String, Bool) -> Void)? = canReact ? { emoji, mine in model.react(entry, emoji: emoji, remove: mine) } : nil
        Group {
        switch entry.kind {
        case "user":
            UserRow(entry: entry, accent: { model.accent(for: $0) }, onReact: react)
        case "assistant":
            AssistantRow(entry: entry, accent: model.accent(for: entry.route), streaming: liveEntryIDs.contains(entry.id), onReact: react)
        case "question":
            if let question = entry.question {
                ChatQuestionCard(question: question, accent: model.accent(for: entry.route), enabled: model.connected) { answers, dismiss in
                    model.answerQuestion(question, answers: answers, dismiss: dismiss)
                }
            }
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
        .environment(\.chatBlackboardPin, canPin ? { pinEntry(entry) } : nil)
        .environment(\.chatReact, canReact ? { emoji in model.react(entry, emoji: emoji) } : nil)
        .environment(\.chatWorkspace, entry.workspace ?? model.selected?.workspace)
        .contextMenu {
            if canReact {
                Menu("React") {
                    ForEach(chatReactionTapbacks, id: \.self) { emoji in
                        Button(emoji) { model.react(entry, emoji: emoji) }
                    }
                }
            }
            if canPin {
                Button("Pin to Blackboard") { pinEntry(entry) }
            }
        }
    }

    private func pinEntry(_ entry: ChatEntry) {
        let source = entry.kind == "tool" ? (entry.detail?.isEmpty == false ? entry.detail! : entry.text) : entry.text
        // Keep an exact bounded prefix; the worker retains the source ID.
        var body = ""
        for character in source {
            if (body + String(character)).utf8.count > 1500 { break }
            body.append(character)
        }
        model.postToBlackboard(key: "quote-" + entry.id.lowercased(), body: body, category: "note", quote: entry.id)
    }

    private func expandedBinding(_ id: String) -> Binding<Bool> {
        Binding(get: { expanded.contains(id) }, set: { if $0 { expanded.insert(id) } else { expanded.remove(id) } })
    }

    /// A run of local tool rows behind one disclosure. It opens only by hand
    /// (or a jump to a row inside it): opening a live run on each call and
    /// closing it between rounds made busy Team transcripts jump. Collapsed
    /// and live, the header carries the counts and latest step, so progress
    /// updates in place. A row the user expanded before the run folded keeps
    /// it open. Open, the steps hang from a hairline under the header's glyphs.
    @ViewBuilder private func fold(_ entries: [ChatEntry], live: Bool, liveEntryIDs: Set<String>) -> some View {
        let id = ChatTranscriptSegment<ChatEntry>.foldID(entries)
        let open = foldChoice[id] ?? entries.contains { expanded.contains($0.id) }
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
                // Each row is a scroll target of its own, so a jump from the
                // activity strip lands on the row, not the fold's centre.
                VStack(alignment: .leading, spacing: textStyle.scaled(2)) { ForEach(entries) { entry in row(entry, liveEntryIDs: liveEntryIDs).id(entry.id) } }
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
                    .controlSize(.small).keyboardShortcut(model.hasPaneFocus ? KeyboardShortcut("d", modifiers: .command) : nil).help("Deny (⌘D)")
                Button("Allow once") { model.decideApproval(allow: true, target: approval) }
                    .buttonStyle(HubTheme.Control.prominentButton).controlSize(.small)
                    .keyboardShortcut(model.hasPaneFocus ? KeyboardShortcut("y", modifiers: .command) : nil).help("Allow this once (⌘Y)")
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
            Text("Action details").font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
        }
    }
}

// MARK: - Composer

private struct ChatComposer: View {
    @Environment(\.displayScale) private var displayScale
    @Environment(\.chatHues) private var hues
    @ObservedObject var model: ChatModel
    @State private var height: CGFloat = 22
    @StateObject private var mentionMenu = ChatMentionMenu()
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
                    }, mentions: model.mentionTargets, mentionMenu: mentionMenu)
                        .frame(height: height)
                        .accessibilityLabel("Message")
                    if model.busy {
                        if !model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !model.attachments.isEmpty { interruptButton }
                        stopButton
                    } else { sendButton }
                }
            }
            .padding(.leading, 14).padding(.trailing, 7).padding(.vertical, 7)
            .background(composerSurface)
            .overlay(alignment: .topLeading) {
                ChatMentionList(menu: mentionMenu, model: model).padding(.leading, 6).alignmentGuide(.top) { $0[.bottom] + 6 }
            }
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

    private var composerSurface: some View {
        let shape = RoundedRectangle(cornerRadius: HubTheme.Radius.panel, style: .continuous)
        return shape.fill(hues.composerFill ?? Semantic.composerFill)
            .overlay(shape.strokeBorder(
                yolo ? Semantic.approvalYolo.opacity(0.38) : Semantic.composerRim,
                lineWidth: 1 / max(displayScale, 1)))
    }

    private var editable: Bool { model.connected && model.selectedID != nil }
    /// YOLO is the one mode worth marking on the composer itself: the outline
    /// takes a faint red, the way TaskWraith tints full access.
    private var yolo: Bool { model.selectedID != nil && model.approvalMode == "yolo" }

    private var placeholder: String {
        guard model.selectedID != nil else { return "Start a new chat to begin" }
        guard model.connected else { return "Connecting…" }
        if model.team?.enabled == true { return "Message the Team, or @ a member…" }
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
        .buttonStyle(.plain).keyboardShortcut(model.hasPaneFocus ? KeyboardShortcut(".", modifiers: .command) : nil)
        .accessibilityLabel("Stop").help("Stop and keep what has arrived (⌘.)")
    }
    private var interruptButton: some View {
        Button { model.send() } label: {
            Image(systemName: "arrow.uturn.forward").font(.system(size: 13, weight: .semibold)).foregroundStyle(model.activeAccent)
                .frame(width: 28, height: 28).contentShape(Rectangle())
        }.buttonStyle(.plain).disabled(!model.canInterrupt)
            .help("Interrupt and send this update (Return)").accessibilityLabel("Interrupt and send update")
    }

    /// The approval mode, any notice or status, and the turn clock. A Team
    /// leaves what is running to the activity strip above the composer: with
    /// several members the status line here wrapped and unwrapped as each
    /// tool started, moving the composer. A solo chat keeps its status line.
    private var footer: some View {
        HStack(alignment: .top) {
            approvalModeMenu
            if !model.notice.isEmpty {
                Text(model.notice).font(HubTheme.Typography.detail).foregroundStyle(Semantic.accentOnSurface).textSelection(.enabled)
            } else if model.team?.enabled != true {
                ChatShimmerText(text: model.status, active: model.busy && model.approval == nil, accent: model.activeAccent)
                    .font(HubTheme.Typography.detail)
            }
            Spacer(minLength: 8)
            ChatTurnTime(model: model)
        }
        .lineLimit(2).padding(.horizontal, 6).frame(minHeight: 16)
    }

    /// A plain-styled menu, so the label keeps its dot and colour: AppKit's
    /// borderless menu flattens both to label grey. While a turn runs the mode
    /// cannot change, but it is still in force, so it shows as a plain label
    /// in full colour rather than a dimmed control.
    @ViewBuilder private var approvalModeMenu: some View {
        let modes = [("manual", "Manual"), ("accept_edits", "Accept Edits"), ("yolo", "YOLO")]
        let title = modes.first { $0.0 == model.approvalMode }?.1 ?? "Manual"
        let accent = ChatApprovalAccent.color(model.approvalMode)
        let help = "Manual asks for edits and commands. Accept Edits allows repository patches and asks for commands. YOLO runs tools without prompts."
        let label = HStack(spacing: 5) {
            Circle().fill(accent).frame(width: 6, height: 6)
            Text(title).font(HubTheme.Typography.detail).foregroundStyle(accent)
            if !model.busy {
                Image(systemName: "chevron.down").font(.system(size: 7.5, weight: .semibold)).foregroundStyle(accent.opacity(0.7))
            }
        }
        if model.busy, model.selectedID != nil {
            label.fixedSize()
                .accessibilityElement(children: .ignore).accessibilityLabel("Approval mode: " + title)
                .help(help + " The mode can change when this turn ends.")
        } else {
            Menu {
                ForEach(modes, id: \.0) { mode in
                    Button { chooseApprovalMode(mode.0) } label: {
                        if model.approvalMode == mode.0 { Label(mode.1, systemImage: "checkmark") }
                        else { Text(mode.1) }
                    }
                }
            } label: { label }
            .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden).fixedSize()
            .disabled(model.selectedID == nil)
            .accessibilityLabel("Approval mode: " + title)
            .help(help)
        }
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

/// The Team members `@` can address, floated above the composer while a tag
/// is typed: each member's mark, name and model, the chosen row tinted with
/// its accent. The text view keeps the keys; a click or hover works too.
private struct ChatMentionList: View {
    @ObservedObject var menu: ChatMentionMenu
    var model: ChatModel

    var body: some View {
        if let state = menu.state {
            VStack(alignment: .leading, spacing: 1) {
                ForEach(Array(state.candidates.enumerated()), id: \.element.id) { index, target in
                    row(target, index: index, selected: index == state.selected)
                }
            }
            .padding(4)
            .frame(width: 270, alignment: .leading)
            // Opaque, a step above the window, so the transcript never shows through.
            .background {
                RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Semantic.surface)
                    .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Semantic.cardFill))
            }
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(model.activeAccent.opacity(0.22), lineWidth: 0.5))
            .shadow(color: .black.opacity(0.18), radius: 14, y: 4)
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Team members to address")
        }
    }

    private func row(_ target: ChatMentionTarget, index: Int, selected: Bool) -> some View {
        let route = model.route(named: target.route)
        return Button { menu.choose?(target) } label: {
            HStack(spacing: 8) {
                ChatProviderIcon(presentation: route?.presentation, size: 15)
                Text(target.name).font(.system(size: 12.5, weight: .semibold)).foregroundStyle(Semantic.ink)
                Text(route?.label ?? target.route).font(.system(size: 11.5)).foregroundStyle(Semantic.secondaryInk)
                Spacer(minLength: 8)
                if selected {
                    Image(systemName: "return").font(.system(size: 9.5, weight: .semibold)).foregroundStyle(Semantic.secondaryInk)
                }
            }
            .lineLimit(1)
            .padding(.horizontal, 8).frame(height: 28)
            .background(RoundedRectangle(cornerRadius: 7, style: .continuous)
                .fill(selected ? Color(nsColor: ChatMentionStyle.fill(target.accent)) : .clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { if $0 { menu.select(index) } }
        .accessibilityLabel("Address " + target.name + ", " + (route?.label ?? target.route))
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}
