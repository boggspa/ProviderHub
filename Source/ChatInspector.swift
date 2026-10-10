import AppKit
import SwiftUI

struct ChatInspector: View {
    @ObservedObject var model: ChatModel
    var body: some View {
        VStack(spacing: 0) {
            // The inspector can be 290 pt wide; when every label will not fit,
            // only the selected tab keeps its label.
            ViewThatFits(in: .horizontal) {
                tabBar(compact: false)
                tabBar(compact: true)
            }.frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 14).padding(.top, 12)
            Divider()
            switch model.inspectorTab {
            case .changes: ChatChangesPane(model: model)
            case .agents: ChatAgentsPane(model: model)
            case .side: ChatSidePane(model: model)
            case .processes: ChatProcessesPane(model: model)
            }
        }.frame(maxHeight: .infinity, alignment: .top)
    }
    private func tabBar(compact: Bool) -> some View {
        HStack(spacing: compact ? 14 : 12) {
            ForEach(ChatInspectorTab.allCases) { tab in tabButton(tab, compact: compact) }
        }
    }
    private func tabButton(_ tab: ChatInspectorTab, compact: Bool) -> some View {
        let selected = model.inspectorTab == tab
        let running = tab == .processes ? model.runningProcessCount : 0
        return Button { model.inspectorTab = tab } label: {
            VStack(spacing: 8) {
                HStack(spacing: 4) {
                    if selected || !compact { Label(tab.label, systemImage: tab.icon) } else { Image(systemName: tab.icon) }
                    if running > 0 {
                        Text("\(running)").font(.system(size: 9.5, weight: .semibold)).monospacedDigit()
                            .foregroundStyle(model.activeAccent).padding(.horizontal, 4.5).padding(.vertical, 1)
                            .background(model.activeAccent.opacity(0.14), in: Capsule())
                    }
                }.font(.system(size: 11.5, weight: selected ? .medium : .regular)).lineLimit(1).fixedSize()
                Rectangle().fill(selected ? model.activeAccent : .clear).frame(height: 2)
            }.contentShape(Rectangle())
        }.buttonStyle(.plain).foregroundStyle(selected ? .primary : .secondary).help(tab.title)
            .accessibilityLabel(tab.title).accessibilityValue(running > 0 ? "\(running) running" : "")
            .accessibilityAddTraits(selected ? .isSelected : [])
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
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 4) {
                        ChatTeamControls(model: model)
                        if let chat = model.selectedID {
                            Divider().padding(.horizontal, 14)
                            ChatBlackboardSection(model: model, chat: chat)
                        }
                        if !model.agents.isEmpty { Text("Earlier delegated tasks").font(.system(size: 11.5, weight: .medium)).foregroundStyle(.secondary).padding(8) }
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
                            let streaming = busy && entries.last?.id == entry.id
                            if streaming || !ChatReplySources.isBlank(entry.text) {
                                VStack(alignment: .leading, spacing: 6) {
                                    ChatSpeakerHeader(member: entry.memberName, label: route?.label ?? entry.route, presentation: route?.presentation,
                                                      accent: route?.accent ?? .secondary, live: streaming)
                                    AssistantRow(entry: entry, accent: route?.accent ?? .secondary, streaming: streaming)
                                }
                            }
                        case "tool": ToolRow(entry: entry, accent: model.accent(for: entry.route), expanded: Binding(get: { expanded.contains(entry.id) }, set: { yes in
                            if yes { expanded.insert(entry.id) } else { expanded.remove(entry.id) }
                        }), workspace: workspace, live: busy && entry.detail == "Running…")
                        default:
                            if ChatNotice.style(entry.text) != .hidden { NoticeRow(entry: entry) }
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

/// Commands agents left running between turns. The worker owns the processes
/// and publishes snapshots; this pane only asks it to refresh, stop or clear.
private struct ChatProcessesPane: View {
    @ObservedObject var model: ChatModel
    @State private var expanded = Set<String>()
    private var running: [ChatProcess] { model.processes.filter(\.running) }
    private var finished: [ChatProcess] { model.processes.filter { !$0.running } }
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Text(model.processes.isEmpty ? "Background Processes" : "\(running.count) running · \(finished.count) finished")
                    .font(.system(size: 11.5)).foregroundStyle(.secondary)
                Spacer()
                Button { model.clearFinishedProcesses() } label: { Image(systemName: "trash") }
                    .buttonStyle(.plain).disabled(finished.isEmpty || !model.connected)
                    .help("Clear finished processes").accessibilityLabel("Clear finished processes")
                Button { model.refreshProcesses(userInitiated: true) } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.plain).disabled(!model.connected).help("Refresh processes").accessibilityLabel("Refresh background processes")
            }.padding(14)
            if !model.processesNotice.isEmpty {
                Text(model.processesNotice).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(3)
                    .padding(.horizontal, 14).padding(.bottom, 8)
            }
            if model.processes.isEmpty {
                VStack(spacing: 6) {
                    Text("No background processes.").font(.system(size: 12)).foregroundStyle(.secondary)
                    Text("When an agent starts a server, watcher or long job in the background, it appears here.")
                        .font(.system(size: 10.5)).foregroundStyle(.tertiary)
                }.multilineTextAlignment(.center).padding(24).frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 12) {
                        ForEach(running) { row($0) }
                        if !finished.isEmpty {
                            Text("Finished").font(.system(size: 11.5, weight: .medium)).foregroundStyle(.secondary)
                                .padding(.top, running.isEmpty ? 0 : 6).accessibilityAddTraits(.isHeader)
                        }
                        ForEach(finished) { row($0) }
                    }.padding(.horizontal, 14).padding(.bottom, 14)
                }
            }
            Text("Background processes keep running between turns. They stop when you stop them here, delete this chat or quit Provider Hub.")
                .font(.system(size: 10.5)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true).padding(14)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .task(id: model.selectedID) { expanded = []; model.refreshProcesses() }
        // Poll only while something is alive; the task is cancelled when the
        // pane disappears, the chat changes or the last process finishes.
        .task(id: "\(model.selectedID ?? "")|\(model.runningProcessCount > 0)") {
            guard model.runningProcessCount > 0 else { return }
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                if Task.isCancelled { break }
                model.refreshProcesses()
            }
        }
    }
    private func route(_ process: ChatProcess) -> ChatRoute? {
        if let account = process.account, let match = model.models.first(where: { $0.route == process.route && $0.account == account }) { return match }
        return model.models.first { $0.route == process.route }
    }
    private func row(_ process: ChatProcess) -> some View {
        let route = route(process)
        let owner = process.owner.isEmpty ? route?.label ?? "Agent" : process.owner
        let isExpanded = expanded.contains(process.id)
        return VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .top, spacing: 6) {
                Button {
                    if isExpanded { expanded.remove(process.id) } else { expanded.insert(process.id) }
                } label: {
                    HStack(alignment: .top, spacing: 6) {
                        Image(systemName: "chevron.right").font(.system(size: 8.5, weight: .semibold)).foregroundStyle(.secondary)
                            .rotationEffect(.degrees(isExpanded ? 90 : 0)).frame(width: 10, height: 15)
                        ChatProviderIcon(presentation: route?.presentation).padding(.top, 0.5)
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 6) {
                                Text(owner).font(.system(size: 11.5, weight: .medium)).lineLimit(1)
                                Spacer(minLength: 4)
                                status(process, accent: route?.accent ?? model.activeAccent)
                            }
                            Text(process.command).font(.system(size: 11, design: .monospaced)).lineLimit(2).truncationMode(.middle)
                                .foregroundStyle(HubTheme.Semantic.ink).frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }.contentShape(Rectangle())
                }.buttonStyle(.plain).help(process.command)
                    .accessibilityLabel("\(owner): \(process.command), \(process.statusText())")
                    .accessibilityHint(isExpanded ? "Hides output" : "Shows output")
                if process.status == "running" {
                    Button { model.stopProcess(process.id) } label: { Image(systemName: "stop.circle").font(.system(size: 13)) }
                        .buttonStyle(.plain).foregroundStyle(.secondary).disabled(!model.connected).padding(.top, 0.5)
                        .help("Stop this process").accessibilityLabel("Stop \(process.command)")
                }
            }
            if isExpanded { detail(process).padding(.leading, 16) }
        }
    }
    @ViewBuilder private func status(_ process: ChatProcess, accent: Color) -> some View {
        switch process.status {
        case "running":
            TimelineView(.periodic(from: .now, by: 1)) { context in
                HStack(spacing: 4) {
                    Circle().fill(accent).frame(width: 6, height: 6).accessibilityHidden(true)
                    Text(process.statusText(now: context.date)).foregroundStyle(.secondary)
                }
            }.font(.system(size: 10.5)).monospacedDigit().lineLimit(1).fixedSize()
        case "stopping":
            HStack(spacing: 4) {
                ProgressView().controlSize(.mini).scaleEffect(0.7).frame(width: 10, height: 10).accessibilityHidden(true)
                Text(process.statusText()).foregroundStyle(.secondary)
            }.font(.system(size: 10.5)).lineLimit(1).fixedSize()
        default:
            Text(process.statusText()).font(.system(size: 10.5)).monospacedDigit().lineLimit(1).fixedSize()
                .foregroundStyle(process.failed ? Color(nsColor: .systemRed) : .secondary)
                .help(stoppedHelp(process))
        }
    }
    private func stoppedHelp(_ process: ChatProcess) -> String {
        switch process.stoppedBy {
        case "user": "Stopped by you"
        case "agent": "Stopped by agent"
        case "chat": "Stopped with the chat"
        default: process.code.map { "Exit status \($0)" } ?? process.statusText()
        }
    }
    private func detail(_ process: ChatProcess) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text("PID \(process.pid) · " + (process.workspace as NSString).abbreviatingWithTildeInPath)
                .font(.system(size: 10.5)).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle).help(process.workspace)
            if process.truncated { Text("Earlier output not shown.").font(.system(size: 10.5)).foregroundStyle(.secondary) }
            ScrollView {
                Text(process.output.isEmpty ? (process.running ? "(no output yet)" : "(no output)") : process.output)
                    .font(.system(size: 11, design: .monospaced)).foregroundStyle(Color.white.opacity(process.output.isEmpty ? 0.5 : 0.88))
                    .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading).padding(10)
            }.defaultScrollAnchor(.bottom)
                .frame(minHeight: 44, maxHeight: 220)
                .background(Color.black.opacity(0.88), in: RoundedRectangle(cornerRadius: 6))
                .accessibilityLabel("Output")
        }
    }
}
