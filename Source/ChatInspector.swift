import AppKit
import SwiftUI

struct ChatInspector: View {
    @ObservedObject var model: ChatModel
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                ForEach(ChatInspectorTab.allCases) { tab in
                    Button { model.inspectorTab = tab } label: {
                        VStack(spacing: 8) {
                            Label(tab == .changes ? "Changes" : tab == .agents ? "Subagents" : "Side Chat", systemImage: tab.icon)
                                .font(.system(size: 11.5, weight: model.inspectorTab == tab ? .medium : .regular)).lineLimit(1)
                            Rectangle().fill(model.inspectorTab == tab ? model.activeAccent : .clear).frame(height: 2)
                        }.contentShape(Rectangle())
                    }.buttonStyle(.plain).foregroundStyle(model.inspectorTab == tab ? .primary : .secondary)
                        .accessibilityLabel(tab.title).accessibilityAddTraits(model.inspectorTab == tab ? .isSelected : [])
                }
                Spacer(minLength: 0)
            }.padding(.horizontal, 14).padding(.top, 12)
            Divider()
            switch model.inspectorTab {
            case .changes: ChatChangesPane(model: model)
            case .agents: ChatAgentsPane(model: model)
            case .side: ChatSidePane(model: model)
            }
        }.frame(maxHeight: .infinity, alignment: .top)
    }
}

private struct ChatChangesPane: View {
    @ObservedObject var model: ChatModel
    @State private var expanded = Set<String>()
    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(model.gitChanges.map { "\($0.files.count) changed \($0.files.count == 1 ? "file" : "files")" } ?? "File Changes")
                    .font(.system(size: 11.5)).foregroundStyle(.secondary)
                Spacer()
                if model.gitChangesLoading { ProgressView().controlSize(.mini) }
                Button { model.refreshChanges() } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.plain).disabled(model.gitChangesLoading).help("Refresh changes").accessibilityLabel("Refresh file changes")
            }.padding(14)
            if !model.inspectorNotice.isEmpty { inspectorEmpty(model.inspectorNotice) }
            else if let changes = model.gitChanges {
                if changes.files.isEmpty { inspectorEmpty("No file changes.") }
                else {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 10) {
                            ForEach(changes.files) { file in
                                DisclosureGroup(isExpanded: Binding(get: { expanded.contains(file.id) }, set: { value in
                                    if value { expanded.insert(file.id) } else { expanded.remove(file.id) }
                                })) {
                                    if file.binary { Text("Binary file").font(.system(size: 11)).foregroundStyle(.secondary).padding(.vertical, 6) }
                                    else {
                                        ScrollView([.horizontal, .vertical]) { PatchText(text: file.diff, onDark: true).padding(10).fixedSize(horizontal: true, vertical: true) }
                                            .frame(minHeight: 80, maxHeight: 300).background(Color.black.opacity(0.88), in: RoundedRectangle(cornerRadius: 6))
                                            .padding(.top, 5)
                                    }
                                } label: {
                                    HStack(spacing: 5) {
                                        Text(file.path).font(.system(size: 11.5)).lineLimit(1).truncationMode(.middle)
                                        Spacer(minLength: 0)
                                        Text("+\(file.added)").foregroundStyle(Color(nsColor: .systemGreen))
                                        Text("−\(file.deleted)").foregroundStyle(Color(nsColor: .systemRed))
                                    }.font(.system(size: 10.5, design: .monospaced))
                                        .help(file.oldPath.map { $0 + " → " + file.path } ?? file.path)
                                }
                            }
                            if changes.truncated { Text("Large changes are truncated.").font(.system(size: 11)).foregroundStyle(.secondary) }
                        }.padding(.horizontal, 14).padding(.bottom, 14)
                    }
                }
            } else { inspectorEmpty(model.gitChangesLoading ? "Reading changes…" : "Choose a Git workspace.") }
            Spacer(minLength: 0)
        }
        .task(id: model.selected?.workspace) { model.refreshChanges() }
        .onChange(of: model.busy) { _, busy in if !busy { model.refreshChanges() } }
        .onChange(of: model.gitChanges?.files.map(\.path)) { _, paths in
            if expanded.isEmpty, let first = paths?.first { expanded.insert(first) }
        }
    }
}

private struct ChatAgentsPane: View {
    @ObservedObject var model: ChatModel
    private var selected: ChatAgent? { model.agents.first { $0.id == model.inspectedAgentID } }
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let selected {
                HStack(spacing: 7) {
                    Button { model.inspectedAgentID = nil } label: { Image(systemName: "chevron.left") }
                        .buttonStyle(.plain).help("All subagents").accessibilityLabel("All subagents")
                    ChatProviderIcon(presentation: model.models.first { $0.route == selected.route && $0.account == selected.account }?.presentation)
                    Text(selected.label).font(.system(size: 12, weight: .medium)).lineLimit(1)
                    Spacer(minLength: 0)
                    if selected.busy { ProgressView().controlSize(.mini) }
                }.padding(14)
                Text(selected.task).font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(3).padding(.horizontal, 14).padding(.bottom, 8)
                InspectorTranscript(model: model, entries: selected.entries, busy: selected.busy, workspace: selected.workspace, userTitle: "Parent task")
                if selected.truncated == true { Text("Recent activity shown; full transcript is saved.").font(.system(size: 10)).foregroundStyle(.secondary).padding(.horizontal, 14) }
                Text(selected.status.capitalized + (selected.readOnly == true ? " · Read only" : "") + (selected.changedFiles.isEmpty ? "" : " · \(selected.changedFiles.count) files changed"))
                    .font(.system(size: 10.5)).foregroundStyle(.secondary).padding(14)
            } else if model.agents.isEmpty { inspectorEmpty("Delegated tasks appear here.") }
            else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 4) {
                        ForEach(model.agents) { agent in
                            Button { model.inspectedAgentID = agent.id } label: {
                                HStack(alignment: .top, spacing: 8) {
                                    ChatProviderIcon(presentation: model.models.first { $0.route == agent.route && $0.account == agent.account }?.presentation)
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(agent.label).font(.system(size: 11.5, weight: .medium)).lineLimit(1)
                                        Text(agent.task).font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(2)
                                        Text(agent.status.capitalized).font(.system(size: 10)).foregroundStyle(.secondary)
                                    }
                                    Spacer(minLength: 0)
                                    if agent.busy { ProgressView().controlSize(.mini).accessibilityHidden(true) }
                                    else { Image(systemName: "chevron.right").font(.system(size: 9)).foregroundStyle(.secondary) }
                                }.padding(8).frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                            }.buttonStyle(.plain)
                        }
                    }.padding(6)
                }
            }
        }.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

private struct ChatSidePane: View {
    @ObservedObject var model: ChatModel
    @State private var showingModels = false
    @State private var choiceID: String?
    @State private var effort: String?
    @State private var height: CGFloat = 22
    private var route: ChatRoute? {
        if let side = model.sideChat { return model.models.first { $0.route == side.route && $0.account == side.account } }
        return choiceID.flatMap { id in model.models.first { $0.id == id } } ?? model.selectedRoute ?? model.models.first
    }
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 6) {
                Button { showingModels = true; if !model.busy { model.refresh() } } label: {
                    HStack(spacing: 6) {
                        ChatProviderIcon(presentation: route?.presentation)
                        Text(route?.label ?? "Choose model").font(.system(size: 11.5, weight: .medium)).lineLimit(1)
                        Image(systemName: "chevron.down").font(.system(size: 8))
                    }
                }.buttonStyle(.plain).disabled(model.sideChat?.busy == true || model.sideOpening || model.branchBusy)
                    .accessibilityLabel("Side Chat model")
                    .popover(isPresented: $showingModels, arrowEdge: .bottom) {
                        ChatModelPicker(model: model, dismiss: { showingModels = false }, initialChoiceID: route?.id,
                                        initialEffort: model.sideChat?.effort ?? effort ?? "", onSelect: { id, level in
                            choiceID = id; effort = level
                            if model.sideChat != nil { model.setSideModel(choice: id, effort: level) }
                        })
                    }
                Spacer(minLength: 0)
                if model.sideChat != nil {
                    Button { model.closeSideChat() } label: { Image(systemName: "xmark") }
                        .buttonStyle(.plain).accessibilityLabel("Discard Side Chat").help("Discard this temporary conversation")
                }
            }.padding(14)
            if let side = model.sideChat {
                InspectorTranscript(model: model, entries: side.entries, busy: side.busy, workspace: side.workspace)
                if side.truncated == true { Text("Recent activity shown.").font(.system(size: 10)).foregroundStyle(.secondary) }
                if let workspace = side.workspace, workspace != model.selected?.workspace {
                    Text((workspace as NSString).abbreviatingWithTildeInPath).font(.system(size: 10)).foregroundStyle(.secondary)
                        .lineLimit(1).truncationMode(.middle).help("Side Chat keeps its original workspace").padding(.horizontal, 12)
                }
                HStack(alignment: .bottom, spacing: 5) {
                    ComposerTextView(text: $model.sideDraft, height: $height, placeholder: "Ask a side question…",
                                     enabled: model.connected && model.pendingSideText == nil && !model.branchBusy) { model.sendSide() }
                        .frame(height: min(height, 120)).accessibilityLabel("Side Chat message")
                    if side.busy {
                        if !model.sideDraft.isEmpty { sideSendButton("arrow.uturn.forward", label: "Interrupt Side Chat and send update") }
                        Button { model.stopSide() } label: { Image(systemName: "stop.fill").frame(width: 24, height: 24) }
                            .buttonStyle(.plain).accessibilityLabel("Stop Side Chat")
                    } else { sideSendButton("arrow.up", label: "Send to Side Chat") }
                }.padding(8).background(HubTheme.Semantic.raisedSurface, in: RoundedRectangle(cornerRadius: 10)).padding(.horizontal, 12)
                Text(model.sideNotice.isEmpty ? "Temporary · Read only" : model.sideNotice)
                    .font(.system(size: 10.5)).foregroundStyle(.secondary).lineLimit(3).padding(12)
            } else {
                Spacer()
                Text("A side conversation from this point.").font(.system(size: 12)).foregroundStyle(.secondary)
                Text("Temporary · Read only").font(.system(size: 10.5)).foregroundStyle(.tertiary).padding(.top, 5)
                Button("Fork conversation") {
                    if let route { model.openSideChat(choice: route.id, effort: effort ?? "") }
                }.controlSize(.small).disabled(route == nil || model.sideOpening || !model.connected || model.selectedID == nil || model.branchBusy).padding(.top, 12)
                if model.sideOpening { ProgressView().controlSize(.small).padding(10) }
                if !model.sideNotice.isEmpty { Text(model.sideNotice).font(.system(size: 11)).foregroundStyle(.secondary).padding(12) }
                Spacer()
            }
        }
    }
    private func sideSendButton(_ icon: String, label: String) -> some View {
        Button { model.sendSide() } label: { Image(systemName: icon).frame(width: 24, height: 24).foregroundStyle(route?.accent ?? .primary) }
            .buttonStyle(.plain).disabled(!model.canSendSide).accessibilityLabel(label)
    }
}

private struct InspectorTranscript: View {
    @ObservedObject var model: ChatModel
    var entries: [ChatEntry]
    var busy: Bool
    var workspace: String?
    var userTitle = "You"
    @State private var expanded = Set<String>()
    @State private var nearBottom = true
    var body: some View {
      GeometryReader { outer in
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 15) {
                    ForEach(entries) { entry in
                        switch entry.kind {
                        case "user": UserRow(entry: entry, title: userTitle)
                        case "assistant":
                            let route = model.models.first { $0.route == entry.route }
                            AssistantRow(entry: entry, label: route?.label ?? entry.route, accent: route?.accent ?? .secondary,
                                         presentation: route?.presentation, streaming: busy && entries.last?.id == entry.id)
                        case "tool": ToolRow(entry: entry, expanded: Binding(get: { expanded.contains(entry.id) }, set: { yes in
                            if yes { expanded.insert(entry.id) } else { expanded.remove(entry.id) }
                        }), workspace: workspace)
                        default: NoticeRow(entry: entry)
                        }
                    }
                    Color.clear.frame(height: 1).id("inspector-bottom")
                }.padding(14).background(GeometryReader { inner in
                    Color.clear.preference(key: InspectorBottomKey.self, value: inner.frame(in: .named("inspector-transcript")).maxY)
                })
            }.coordinateSpace(name: "inspector-transcript")
                .onPreferenceChange(InspectorBottomKey.self) { y in nearBottom = y - outer.size.height < 100 }
                .onChange(of: entries.count) { _, _ in if nearBottom || entries.last?.kind == "user" { proxy.scrollTo("inspector-bottom", anchor: .bottom) } }
                .onChange(of: entries.last?.text.count) { _, _ in if nearBottom { proxy.scrollTo("inspector-bottom", anchor: .bottom) } }
                .onChange(of: entries.first?.id) { _, _ in proxy.scrollTo("inspector-bottom", anchor: .bottom) }
                .onAppear { proxy.scrollTo("inspector-bottom", anchor: .bottom) }
        }
        }.frame(maxHeight: .infinity)
    }
}

private struct InspectorBottomKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

/// Parallel activity lives with its originating tool call. These quiet links
/// reuse the existing inspector; they never create another composer or tab.
struct ChatParallelLanes: View {
    @ObservedObject var model: ChatModel
    var agentIDs: [String]
    private var agents: [ChatAgent] {
        agentIDs.compactMap { id in model.agents.first { $0.id == id } }
    }
    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 125), alignment: .topLeading)], alignment: .leading, spacing: 9) {
            ForEach(agents) { agent in lane(agent) }
        }
    }
    private func lane(_ agent: ChatAgent) -> some View {
        let route = model.models.first { $0.route == agent.route && $0.account == agent.account }
        return Button {
            model.inspectedAgentID = agent.id; model.showInspector(.agents)
        } label: {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 5) {
                    ChatProviderIcon(presentation: route?.presentation, size: 12)
                    Text(agent.label).font(.system(size: 10.5, weight: .medium)).lineLimit(1)
                    Spacer(minLength: 0)
                    activity(agent)
                }
                Text(agent.task).font(.system(size: 11.5)).lineLimit(2).foregroundStyle(.secondary)
            }
            .padding(.vertical, 5).frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
        }.buttonStyle(.plain).help(agent.task + " · " + agent.status + " · Read only")
            .accessibilityLabel(agent.label + ": " + agent.task + ", " + agent.status)
    }
    @ViewBuilder private func activity(_ agent: ChatAgent) -> some View {
        if agent.busy { ProgressView().controlSize(.mini).accessibilityHidden(true) }
        else { Image(systemName: agent.status == "ready" ? "checkmark" : "exclamationmark.circle").font(.system(size: 10)).foregroundStyle(.secondary) }
    }
}

private func inspectorEmpty(_ text: String) -> some View {
    Text(text).font(.system(size: 12)).foregroundStyle(.secondary).multilineTextAlignment(.center)
        .padding(24).frame(maxWidth: .infinity, maxHeight: .infinity)
}
