import SwiftUI
import UniformTypeIdentifiers

private typealias Semantic = HubTheme.Semantic

extension UTType {
    /// A Team task chip in a drag: "<chat id>\n<task id>", visible only to
    /// this process. A private type keeps the window's file-and-folder drop,
    /// which accepts URLs, from claiming it; a "chat:task" string parses as a
    /// URL. Mirrors the sidebar's chat drag (ChatWorkspaces.swift).
    static let chatTeamTask = UTType(exportedAs: "com.mistralbridge.providerhub.team-task")
}

private struct ChatTeamPreset: Codable, Identifiable {
    struct Member: Codable {
        var name: String
        var choice: String
        var effort: String
        var responsibility: String
    }
    struct RunSettings: Codable {
        var mode: String
        var minutes: Int?
        var tokens: Int?
        var contextTokens: Int
        var processes: Int
    }
    var id: String
    var name: String
    var members: [Member]
    var execution: RunSettings
}

@MainActor
private final class ChatTeamPresetStore: ObservableObject {
    @Published private(set) var presets: [ChatTeamPreset]
    private let defaults = UserDefaults.standard
    private let key = "providerHub.teamPresets.v1"

    init() {
        if let data = defaults.data(forKey: key),
           let saved = try? JSONDecoder().decode([ChatTeamPreset].self, from: data) {
            presets = saved
        } else {
            presets = []
        }
    }

    func save(name: String, members: [TeamDraft], execution: ChatTeamExecution) {
        let cleanName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanName.isEmpty, members.count <= ChatTeamSnapshot.maxMembers else { return }
        let preset = ChatTeamPreset(id: UUID().uuidString, name: String(cleanName.prefix(60)),
            members: members.map { .init(name: $0.name, choice: $0.choice, effort: $0.effort, responsibility: $0.responsibility) },
            execution: .init(mode: execution.mode, minutes: execution.minutes, tokens: execution.tokens,
                             contextTokens: execution.contextTokens, processes: execution.processes))
        presets.append(preset)
        persist()
    }

    func remove(_ preset: ChatTeamPreset) {
        presets.removeAll { $0.id == preset.id }
        persist()
    }

    private func persist() {
        guard let data = try? JSONEncoder().encode(presets) else { return }
        defaults.set(data, forKey: key)
    }
}

/// Small roster controls in the existing inspector; members speak in the main
/// transcript and share its composer. There is no separate orchestration view.
struct ChatTeamControls: View {
    @ObservedObject var model: ChatModel
    @State private var expanded = true
    @State private var newTaskFor: String?
    @State private var taskTitle = ""
    /// The member card (or "" for Unassigned) a task chip is over mid-drag.
    @State private var dropTarget: String?
    var openEditor: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Button { expanded.toggle() } label: {
                    Label("Team", systemImage: expanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 13, weight: .semibold))
                }.buttonStyle(.plain).accessibilityValue(expanded ? "Expanded" : "Collapsed")
                Spacer()
                Button(model.team == nil ? "Set up" : "Edit") { openEditor() }
                    .controlSize(.small).disabled(!model.canEditTeam)
            }
            if expanded {
            Text(model.team?.taskMode == false
                 ? "One contribution each. Members can continue when more work is needed."
                 : "Up to \(ChatTeamSnapshot.maxMembers) members working independently on the same task.")
                .font(.system(size: 11.5)).foregroundStyle(.secondary)
            if let team = model.team {
                if !team.enabled { Text("Team is off").font(.system(size: 11.5)).foregroundStyle(.secondary) }
                ForEach(team.members) { member in
                    VStack(alignment: .leading, spacing: 0) {
                    ChatSharedMemberRow(name: member.name, model: member.label + (member.account.isEmpty ? "" : " · " + member.account),
                        status: member.status, responsibility: member.responsibility, nextStep: member.nextStep,
                        failure: member.failureReason ?? "", waitReason: member.waitReason ?? "",
                        pendingLabel: member.pendingChange?.remove == true ? "Removing…" : member.pendingChange?.label,
                        pending: member.pendingChange != nil, used: member.usage, limit: member.context,
                        canControl: model.canControlTeamMember, stoppedByUser: member.memberStoppedByUser == true,
                        needsInput: team.needsInput, accent: model.activeAccent, fill: HubTheme.Semantic.cardFill,
                        ink: HubTheme.Semantic.ink, secondary: HubTheme.Semantic.secondaryInk,
                        control: { model.controlTeamMember(member.id, start: $0) }) {
                            ChatProviderIcon(presentation: model.models.first { $0.id == member.choice }?.presentation)
                        }
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 5) {
                            ForEach(model.teamTasks.filter { $0.owner == member.id }) { task in
                                ChatTeamTaskChip(task: task, model: model, members: team.members)
                            }
                            Button { taskTitle = ""; newTaskFor = member.id } label: {
                                Label("Task", systemImage: "plus").font(.system(size: 10.5))
                            }.buttonStyle(.plain).padding(.horizontal, 7).frame(height: 22)
                                .background(HubTheme.Semantic.cardFill, in: RoundedRectangle(cornerRadius: ChatTeamTaskChip.radius, style: .continuous))
                                .disabled(!model.connected || !team.enabled)
                                .popover(isPresented: Binding(get: { newTaskFor == member.id }, set: { if !$0 { newTaskFor = nil } })) {
                                    VStack(alignment: .leading, spacing: 8) {
                                        Text("Assign task to \(member.name)").font(.headline)
                                        TextField("Task", text: $taskTitle).onSubmit { addTask(member.id) }
                                        Button("Add task") { addTask(member.id) }
                                            .disabled(taskTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                                    }.padding(14).frame(width: 280)
                                }
                        }.padding(.horizontal, 8).padding(.bottom, 8)
                    }.background(HubTheme.Semantic.cardFill)
                    }.clipShape(RoundedRectangle(cornerRadius: 7))
                    .overlay { dropRing(member.id) }
                    .onDrop(of: [.chatTeamTask], isTargeted: targeting(member.id)) { receive($0, owner: member.id) }
                    .accessibilityHint("Drop a task chip here to assign it to \(member.name).")
                }
                if model.teamTasks.contains(where: { $0.owner.isEmpty }) {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 5) {
                            Text("Unassigned").font(.system(size: 10.5)).foregroundStyle(.secondary)
                            ForEach(model.teamTasks.filter { $0.owner.isEmpty }) { task in
                                ChatTeamTaskChip(task: task, model: model, members: team.members)
                            }
                        }.padding(4)
                    }
                    .overlay { dropRing("") }
                    .onDrop(of: [.chatTeamTask], isTargeted: targeting("")) { receive($0, owner: "") }
                }
                if !model.teamTasksNotice.isEmpty {
                    Text(model.teamTasksNotice).font(.system(size: 11)).foregroundStyle(.secondary).textSelection(.enabled)
                }
                if team.needsInput {
                    Text("Answer in the main composer to continue.").font(.system(size: 11.5)).foregroundStyle(.secondary)
                }
                if let advice = team.limitAdvice {
                    Text(advice)
                        .font(.system(size: 11.5)).foregroundStyle(.secondary).textSelection(.enabled)
                }
                if let run = team.runUsage, let tokens = run.tokens, (run.requests ?? 0) > 0 {
                    TimelineView(.periodic(from: .now, by: 60)) { tick in
                        Text(runReadout(team, tokens: tokens, now: tick.date))
                            .font(.system(size: 10.5)).foregroundStyle(.secondary)
                            .help("Cumulative input, output and cache tokens across all members; not a billing estimate." +
                                  (run.usageComplete == false ? " Some requests did not report usage." : ""))
                    }
                }
                HStack {
                    if team.canResume {
                        Button("Resume unfinished work") { model.resumeTeam() }.controlSize(.small)
                            .disabled(!model.canConfigureTeam)
                    }
                    if team.enabled, model.busy { Button("Stop Team") { model.stop() }.controlSize(.small) }
                    Spacer(minLength: 0)
                }
            }
            Text("Members think and read together. Workspace changes and approvals queue one at a time, using this chat’s approval mode. Tag @Name in a message to address only those members.")
                .font(.system(size: 10.5)).foregroundStyle(.secondary)
            if !model.teamNotice.isEmpty {
                Text(model.teamNotice).font(.system(size: 11.5)).foregroundStyle(.secondary).textSelection(.enabled)
            }
            if model.teamRequest != nil { ProgressView().controlSize(.small) }
            } else if let team = model.team {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 12) {
                        ForEach(team.members) { member in
                            HStack(spacing: 4) {
                                ChatProviderIcon(presentation: model.models.first { $0.id == member.choice }?.presentation, size: 12)
                                Text(member.name).font(.system(size: 11, weight: .medium))
                            }.help("\(member.name) · \(member.status)")
                        }
                    }.padding(9)
                }.background(HubTheme.Semantic.cardFill, in: RoundedRectangle(cornerRadius: 8))
            }
        }.padding(14)
    }
    private func addTask(_ owner: String) {
        let title = taskTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else { return }
        model.inspectorCommand(["command": "team_task_user", "owner": owner, "title": title])
        newTaskFor = nil; taskTitle = ""
    }
    private func assign(_ task: ChatTeamTask, to owner: String) {
        model.inspectorCommand(["command": "team_task_user", "task": task.id, "owner": owner, "expectedOwner": task.owner])
    }
    private func targeting(_ owner: String) -> Binding<Bool> {
        Binding(get: { dropTarget == owner }, set: { over in
            if over { dropTarget = owner } else if dropTarget == owner { dropTarget = nil }
        })
    }
    /// Without this a refused drop and a missing feature look the same.
    @ViewBuilder private func dropRing(_ owner: String) -> some View {
        if dropTarget == owner {
            RoundedRectangle(cornerRadius: 7, style: .continuous)
                .fill(model.activeAccent.opacity(0.08))
                .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous).strokeBorder(model.activeAccent.opacity(0.8), lineWidth: 1.5))
                .allowsHitTesting(false)
        }
    }
    private func receive(_ providers: [NSItemProvider], owner: String) -> Bool {
        let type = UTType.chatTeamTask.identifier
        guard let provider = providers.first(where: { $0.hasItemConformingToTypeIdentifier(type) }) else { return false }
        provider.loadDataRepresentation(forTypeIdentifier: type) { data, _ in
            guard let data, data.count <= 512, let text = String(data: data, encoding: .utf8) else { return }
            let parts = text.components(separatedBy: "\n")
            guard parts.count == 2 else { return }
            Task { @MainActor in
                // The chat may have changed while the drag was in flight.
                guard parts[0] == model.stateChatID, let task = model.teamTasks.first(where: { $0.id == parts[1] }),
                      task.owner != owner else { return }
                assign(task, to: owner)
            }
        }
        return true
    }
    private func runReadout(_ team: ChatTeamSnapshot, tokens: Int, now: Date) -> String {
        var parts = ["Run"]
        if let limit = team.execution?.minutes, let start = team.runUsage?.started {
            let end = team.runUsage?.ended ?? now.timeIntervalSince1970
            parts.append("\(max(0, Int((end - start) / 60))) of \(limit) min")
        }
        parts.append(tokens.formatted() + (team.execution?.tokens.map { " of \($0.formatted())" } ?? "") + " reported tokens" +
                     (team.runUsage?.usageComplete == false ? " (partial)" : ""))
        return parts.joined(separator: " · ")
    }
}

/// A task as a rounded rectangle: state glyph, title, the status as plain
/// words, and a pin count. Members set status; the chip's rim and wash carry
/// it (red blocked or paused, green completed, neutral otherwise), so the
/// route accent stays on the member card. Opening it shows the task's pinned
/// context and hand-over trail rather than status buttons; the user's status
/// override is in the context menu, for tasks a stopped seat never closed.
private struct ChatTeamTaskChip: View {
    static let radius: CGFloat = 5
    static let statuses = ["pending", "in_progress", "blocked", "paused", "completed"]
    var task: ChatTeamTask
    @ObservedObject var model: ChatModel
    var members: [ChatTeamMember]
    @State private var showing = false

    static func label(_ status: String) -> String { status == "in_progress" ? "In progress" : status.capitalized }
    static func tone(_ status: String) -> Color? {
        switch status {
        case "blocked", "paused": return ChatSharedPatchText.removed
        case "completed": return ChatSharedPatchText.added
        default: return nil
        }
    }
    private var label: String { Self.label(task.status) }
    private var tone: Color? { Self.tone(task.status) }
    private var shape: RoundedRectangle { RoundedRectangle(cornerRadius: Self.radius, style: .continuous) }

    var body: some View {
        Button { showing.toggle() } label: {
            HStack(spacing: 6) {
                ChatTeamTaskGlyph(status: task.status).foregroundStyle(tone ?? Semantic.secondaryInk)
                // The title gives way first, so the status never leaves the chip.
                Text(task.title).lineLimit(1).truncationMode(.tail)
                    .foregroundStyle(task.status == "completed" ? Semantic.secondaryInk : Semantic.ink)
                Rectangle().fill(Semantic.secondaryInk.opacity(0.3)).frame(width: 0.5, height: 11)
                Text(label).foregroundStyle(Semantic.secondaryInk).fixedSize().layoutPriority(1)
                if !task.pinList.isEmpty {
                    HStack(spacing: 2) {
                        Image(systemName: "pin").font(.system(size: 8.5))
                        Text("\(task.pinList.count)").monospacedDigit()
                    }.foregroundStyle(Semantic.secondaryInk.opacity(0.7)).fixedSize().layoutPriority(1)
                }
            }
            .font(.system(size: 10.5)).padding(.leading, 6).padding(.trailing, 8).frame(height: 22)
            .background {
                shape.fill(Semantic.raisedSurface)
                if let tone { shape.fill(tone.opacity(0.10)) }
            }
            .overlay(shape.strokeBorder(tone?.opacity(0.55) ?? Semantic.secondaryInk.opacity(0.2), lineWidth: 0.5))
            .contentShape(shape)
        }
        .buttonStyle(.plain)
        .help("\(label)" + (task.setBy.map { " · set by \($0.name)" } ?? "") + " · drag to reassign")
        .accessibilityLabel("\(task.title), \(label)")
        .accessibilityHint("Shows pinned context. Drag onto another member to reassign.")
        .onDrag {
            let provider = NSItemProvider()
            guard let chat = model.stateChatID else { return provider }
            let payload = Data((chat + "\n" + task.id).utf8)
            provider.registerDataRepresentation(forTypeIdentifier: UTType.chatTeamTask.identifier, visibility: .ownProcess) { completion in
                completion(payload, nil)
                return nil
            }
            return provider
        }
        .contextMenu {
            Menu("Assign to") { assignItems }
            Menu("Override status") {
                ForEach(Self.statuses, id: \.self) { status in
                    Button(Self.label(status)) { send(["status": status]) }.disabled(status == task.status)
                }
            }
        }
        .popover(isPresented: $showing, arrowEdge: .bottom) {
            ChatTeamTaskDetail(task: task, model: model, members: members, send: send, close: { showing = false })
        }
    }

    @ViewBuilder private var assignItems: some View {
        ForEach(members) { member in
            Button(member.name) { send(["owner": member.id]) }.disabled(member.id == task.owner)
        }
        Divider()
        Button("Unassigned") { send(["owner": ""]) }.disabled(task.owner.isEmpty)
    }

    /// One change, checked against the owner this chip was drawn with.
    private func send(_ change: [String: Any]) {
        var command: [String: Any] = ["command": "team_task_user", "task": task.id, "owner": task.owner, "expectedOwner": task.owner]
        command.merge(change) { $1 }
        model.inspectorCommand(command)
    }
}

/// The state glyph. In progress turns slowly unless Reduce Motion is on.
private struct ChatTeamTaskGlyph: View {
    var status: String
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var turning = false
    var body: some View {
        Image(systemName: Self.symbol(status)).font(.system(size: 10))
            .rotationEffect(.degrees(turning ? 360 : 0))
            .animation(turning ? .linear(duration: 2.4).repeatForever(autoreverses: false) : .default, value: turning)
            .onAppear { turning = status == "in_progress" && !reduceMotion }
            .onChange(of: status) { _, next in turning = next == "in_progress" && !reduceMotion }
            .accessibilityHidden(true)
    }
    static func symbol(_ status: String) -> String {
        switch status {
        case "in_progress": return "circle.dashed"
        case "blocked": return "exclamationmark.circle"
        case "paused": return "pause.circle"
        case "completed": return "checkmark.circle.fill"
        default: return "circle"
        }
    }
}

/// The chip's popover: who set the status and when, the context pinned to
/// the task, and the seats it has passed through.
private struct ChatTeamTaskDetail: View {
    var task: ChatTeamTask
    @ObservedObject var model: ChatModel
    var members: [ChatTeamMember]
    var send: ([String: Any]) -> Void
    var close: () -> Void

    private var board: ChatBlackboardSnapshot? { model.blackboard.state(model.stateChatID).snapshot }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                Text(task.title).font(.headline).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 5) {
                    RoundedRectangle(cornerRadius: 2).fill(ChatTeamTaskChip.tone(task.status) ?? Semantic.secondaryInk).frame(width: 6, height: 6)
                    Text(meta)
                }.font(.system(size: 11)).foregroundStyle(Semantic.secondaryInk)
            }
            HStack {
                heading("Pinned context")
                Spacer()
                pinMenu
            }
            if task.pinList.isEmpty {
                Text("Nothing pinned. Members pin to-dos, Blackboard posts, files and transcript entries here as they work.")
                    .font(.system(size: 10.5)).foregroundStyle(Semantic.secondaryInk).fixedSize(horizontal: false, vertical: true)
            }
            ForEach(task.pinList) { pin in pinRow(pin) }
            if !task.handovers.isEmpty {
                heading("Handed over")
                ForEach(Array(task.handovers.enumerated().reversed()), id: \.offset) { _, step in handoverRow(step) }
            }
            Divider()
            HStack {
                Text("Drag the chip to reassign").font(.system(size: 10.5)).foregroundStyle(Semantic.secondaryInk)
                Spacer()
                Menu("Assign to") {
                    ForEach(members) { member in
                        Button(member.name) { send(["owner": member.id]); close() }.disabled(member.id == task.owner)
                    }
                    Divider()
                    Button("Unassigned") { send(["owner": ""]); close() }.disabled(task.owner.isEmpty)
                }.controlSize(.small).fixedSize()
            }
        }.padding(14).frame(width: 300, alignment: .leading)
    }

    private var meta: String {
        var parts = [ChatTeamTaskChip.label(task.status)]
        if let by = task.setBy { parts.append("set by " + by.name) }
        if let ago = Self.ago(task.updated) { parts.append(ago) }
        return parts.joined(separator: " · ")
    }

    static func ago(_ stamp: String?) -> String? {
        guard let stamp, let date = ChatBlackboardSnapshot.date(stamp) else { return nil }
        let formatter = RelativeDateTimeFormatter(); formatter.unitsStyle = .short
        return formatter.localizedString(for: date, relativeTo: Date())
    }

    private func heading(_ text: String) -> some View {
        Text(text.uppercased()).font(.system(size: 9.5, weight: .semibold)).kerning(0.5).foregroundStyle(Semantic.secondaryInk)
    }

    private static func icon(_ kind: String) -> String {
        switch kind {
        case "todo": return "checklist"
        case "post": return "pin"
        case "file": return "paperclip"
        default: return "text.bubble"
        }
    }
    private static func source(_ kind: String) -> String {
        switch kind {
        case "todo": return "To-do"
        case "post": return "Blackboard post"
        case "file": return "Blackboard file"
        default: return "Transcript"
        }
    }

    /// Where a pin leads: its transcript entry, a post's quoted entry, or a
    /// file. A to-do, or a target that has since gone, only shows its label.
    private func destination(_ pin: ChatTeamTask.Pin) -> (() -> Void)? {
        switch pin.kind {
        case "entry": return { model.jump(to: pin.ref); close() }
        case "post":
            guard let entry = board?.posts.first(where: { $0.id == pin.ref })?.quote?.entryID else { return nil }
            return { model.jump(to: entry); close() }
        case "file":
            guard let file = board?.attachments.first(where: { $0.id == pin.ref }) else { return nil }
            if let link = file.url, let url = URL(string: link), ["http", "https"].contains(url.scheme?.lowercased() ?? "") {
                return { NSWorkspace.shared.open(url) }
            }
            guard let path = file.path else { return nil }
            return { NSWorkspace.shared.open(URL(fileURLWithPath: path)) }
        default: return nil
        }
    }

    private func pinRow(_ pin: ChatTeamTask.Pin) -> some View {
        let open = destination(pin)
        return HStack(alignment: .top, spacing: 8) {
            Button { open?() } label: {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: Self.icon(pin.kind)).font(.system(size: 11)).frame(width: 14).foregroundStyle(Semantic.secondaryInk)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(pin.label).font(.system(size: 11.5)).foregroundStyle(Semantic.ink).lineLimit(3)
                            .multilineTextAlignment(.leading).fixedSize(horizontal: false, vertical: true)
                        Text(pin.by.name + " · " + Self.source(pin.kind) + (open == nil ? "" : " ↗"))
                            .font(.system(size: 10)).foregroundStyle(Semantic.secondaryInk)
                    }
                    Spacer(minLength: 0)
                }.contentShape(Rectangle())
            }.buttonStyle(.plain).disabled(open == nil)
            Button { send(["unpin": pin.id]) } label: {
                Image(systemName: "xmark").font(.system(size: 9, weight: .semibold)).foregroundStyle(Semantic.secondaryInk)
            }.buttonStyle(.plain).help("Unpin").accessibilityLabel("Unpin \(pin.label)")
        }
    }

    private func handoverRow(_ step: ChatTeamTask.Handover) -> some View {
        Button { if let entry = step.entry { model.jump(to: entry); close() } } label: {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "clock.arrow.circlepath").font(.system(size: 11)).frame(width: 14).foregroundStyle(Semantic.secondaryInk)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Held by \(step.name)").font(.system(size: 11.5)).foregroundStyle(Semantic.ink)
                    Text([step.entry == nil ? nil : "Open their last entry ↗", "moved by \(step.by.name)", Self.ago(step.at)]
                            .compactMap { $0 }.joined(separator: " · "))
                        .font(.system(size: 10)).foregroundStyle(Semantic.secondaryInk)
                }
                Spacer(minLength: 0)
            }.contentShape(Rectangle())
        }.buttonStyle(.plain).disabled(step.entry == nil)
    }

    /// Context the user can pin: Blackboard posts and added files, and every
    /// lane's to-dos. Members also pin transcript entries by ID.
    private var pinMenu: some View {
        let pinned = Set(task.pinList.map { $0.kind + "\u{0}" + $0.ref })
        let posts = (board?.posts ?? []).filter { !pinned.contains("post\u{0}" + $0.id) }
        let files = (board?.attachments ?? []).filter { $0.removable && !pinned.contains("file\u{0}" + $0.id) }
        let lanes = (model.todos?.all ?? []).map { lane in
            (lane, lane.items.filter { !pinned.contains("todo\u{0}" + lane.id + ":" + $0.id) })
        }.filter { !$0.1.isEmpty }
        return Menu {
            if !posts.isEmpty {
                Section("Blackboard posts") {
                    ForEach(posts) { post in Button(post.key) { send(["pin": ["kind": "post", "ref": post.id]]) } }
                }
            }
            if !files.isEmpty {
                Section("Blackboard files") {
                    ForEach(files) { file in Button(file.name) { send(["pin": ["kind": "file", "ref": file.id]]) } }
                }
            }
            ForEach(lanes, id: \.0.id) { lane, items in
                Section("To-dos · " + (lane.name.isEmpty ? "Shared" : lane.name)) {
                    ForEach(items, id: \.id) { item in
                        Button(item.content) { send(["pin": ["kind": "todo", "ref": lane.id + ":" + item.id]]) }
                    }
                }
            }
        } label: {
            Label("Pin", systemImage: "plus").font(.system(size: 10.5))
        }
        .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
        .disabled(posts.isEmpty && files.isEmpty && lanes.isEmpty)
        .help("Pin a Blackboard post, file or to-do to this task")
    }
}

private struct TeamDraft: Identifiable {
    let id: String
    var existingID: String?
    var name: String
    var choice: String
    var effort: String
    var responsibility: String
    var wire: [String: Any] {
        var value: [String: Any] = ["name": name, "choice": choice, "effort": effort, "responsibility": responsibility]
        if let existingID { value["id"] = existingID }
        return value
    }
}

struct ChatTeamEditor: View {
    @ObservedObject var model: ChatModel
    @Environment(\.dismiss) private var dismiss
    @State private var enabled = true
    @State private var drafts: [TeamDraft] = []
    @State private var picking: String?
    @State private var mode = "task"
    @State private var minutes = ""
    @State private var tokens = ""
    @State private var contextTokens = "200000"
    @State private var processSlots = 8
    @StateObject private var presetStore = ChatTeamPresetStore()
    @State private var savingPreset = false
    @State private var presetName = ""

    init(model: ChatModel) { self.model = model }

    private var execution: ChatTeamExecution? {
        let minuteText = minutes.trimmingCharacters(in: .whitespacesAndNewlines)
        let tokenText = tokens.trimmingCharacters(in: .whitespacesAndNewlines)
        guard minuteText.isEmpty || Int(minuteText) != nil, tokenText.isEmpty || Int(tokenText) != nil,
              let context = Int(contextTokens.trimmingCharacters(in: .whitespacesAndNewlines)) else { return nil }
        let value = ChatTeamExecution(mode: mode, contextTokens: context, processes: processSlots,
                                      minutes: Int(minuteText), tokens: Int(tokenText))
        return value.isValid ? value : nil
    }
    private var valid: Bool {
        execution != nil && (1...ChatTeamSnapshot.maxMembers).contains(drafts.count) && drafts.allSatisfy { draft in
            !draft.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && draft.name.count <= 60 &&
            draft.responsibility.count <= 2000 && model.models.contains { $0.id == draft.choice && $0.supportsTools }
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Configure Team").font(.headline); Spacer()
                Toggle("Use Team", isOn: $enabled).toggleStyle(.switch).disabled(model.busy)
                    .help(model.busy ? "Stop the Team before turning it off." : "")
            }
            Text("The first member replaces the current chat model while Team is on. Every member keeps a private model history and shares visible work in this transcript.")
                .font(.system(size: 12)).foregroundStyle(.secondary)
            if model.busy {
                Text("The Team is working. Each member changes at its next safe point: a model request is cancelled, while a running tool finishes and records its result first. A replaced member keeps its place and picks up its work on the new model.")
                    .font(.system(size: 11.5)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            ScrollView {
                VStack(spacing: 12) {
                    ForEach($drafts) { $member in
                        VStack(alignment: .leading, spacing: 8) {
                            HStack {
                                TextField("Member name", text: $member.name).textFieldStyle(.roundedBorder)
                                if drafts.count > 1 {
                                    Button { drafts.removeAll { $0.id == member.id } } label: { Image(systemName: "minus.circle") }
                                        .buttonStyle(.plain).accessibilityLabel("Remove \(member.name)")
                                }
                            }
                            Button { picking = member.id } label: {
                                HStack {
                                    Text(model.models.first { $0.id == member.choice }?.label ?? "Choose model/account")
                                    if let account = model.models.first(where: { $0.id == member.choice })?.accountLabel, !account.isEmpty {
                                        Text(account).foregroundStyle(.secondary)
                                    }
                                    if !member.effort.isEmpty { Text(member.effort).foregroundStyle(.secondary) }
                                    Image(systemName: "chevron.down")
                                }.font(.system(size: 12))
                            }.buttonStyle(.plain)
                                .popover(isPresented: Binding(get: { picking == member.id }, set: { if !$0 { picking = nil } })) {
                                    ChatModelPicker(model: model, dismiss: { picking = nil }, initialChoiceID: member.choice,
                                        initialEffort: member.effort, onSelect: { choice, effort in
                                            member.choice = choice; member.effort = effort; picking = nil
                                        })
                                }
                            TextField("Responsibility (optional)", text: $member.responsibility, axis: .vertical)
                                .lineLimit(2...4).textFieldStyle(.roundedBorder)
                        }.padding(10).hubCard(cornerRadius: 8, accent: model.activeAccent)
                    }
                    runSettings
                }
            }.frame(maxHeight: 410)
            HStack {
                Button("Add member") { add() }.disabled(drafts.count >= ChatTeamSnapshot.maxMembers || model.models.allSatisfy { !$0.supportsTools })
                Menu("Team Presets") {
                    Button("Save current as preset…") {
                        presetName = ""
                        savingPreset = true
                    }.disabled(!valid)
                    if !presetStore.presets.isEmpty {
                        Divider()
                        ForEach(presetStore.presets) { preset in
                            Button(preset.name) { apply(preset) }
                        }
                        Divider()
                        Menu("Delete preset") {
                            ForEach(presetStore.presets) { preset in
                                Button(preset.name, role: .destructive) { presetStore.remove(preset) }
                            }
                        }
                    }
                }.disabled(model.busy)
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Save Team") {
                    model.configureTeam(enabled: enabled, members: drafts.map(\.wire), execution: execution); dismiss()
                }.keyboardShortcut(.defaultAction).disabled(!valid || !model.canEditTeam)
            }
        }.padding(20).frame(width: 480)
        .alert("Save Team Preset", isPresented: $savingPreset) {
            TextField("Preset name", text: $presetName)
            Button("Save") {
                if let execution { presetStore.save(name: presetName, members: drafts, execution: execution) }
            }.disabled(presetName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Save this roster and its run settings for quick reuse.")
        }
        .onAppear {
            if let team = model.team {
                enabled = team.enabled
                let settings = team.execution ?? ChatTeamExecution(mode: "contribution")
                mode = settings.mode
                minutes = settings.minutes.map(String.init) ?? ""
                tokens = settings.tokens.map(String.init) ?? ""
                contextTokens = String(settings.contextTokens)
                processSlots = settings.processes
                // A pending edit is what the user last asked for; saving again supersedes it.
                drafts = team.members.filter { $0.pendingChange?.remove != true }.map { member in
                    let pending = member.pendingChange
                    return TeamDraft(id: member.id, existingID: member.id, name: pending?.name ?? member.name,
                                     choice: pending?.choice ?? member.choice, effort: pending?.effort ?? member.effort,
                                     responsibility: pending?.responsibility ?? member.responsibility)
                }
            } else { add() }
        }
    }
    private func add() {
        guard let choice = model.selectedRoute.flatMap({ $0.supportsTools ? $0 : nil }) ?? model.models.first(where: \.supportsTools) else { return }
        let currentEffort = model.selected?.effort ?? ""
        drafts.append(TeamDraft(id: UUID().uuidString, name: "Member \(drafts.count + 1)",
                                choice: choice.id, effort: choice.efforts.contains(currentEffort) ? currentEffort : "", responsibility: ""))
    }
    private func apply(_ preset: ChatTeamPreset) {
        drafts = preset.members.map { member in
            TeamDraft(id: UUID().uuidString, name: member.name, choice: member.choice,
                      effort: member.effort, responsibility: member.responsibility)
        }
        mode = preset.execution.mode
        minutes = preset.execution.minutes.map(String.init) ?? ""
        tokens = preset.execution.tokens.map(String.init) ?? ""
        contextTokens = String(preset.execution.contextTokens)
        processSlots = preset.execution.processes
    }

    private var runSettings: some View {
        DisclosureGroup("Run settings · " + (mode == "task" ? "Task" : "One contribution") +
                        (execution?.minutes.map { " · \($0) min" } ?? "")) {
            VStack(alignment: .leading, spacing: 10) {
                Picker("Work mode", selection: $mode) {
                    Text("Task").tag("task")
                    Text("One contribution").tag("contribution")
                }.pickerStyle(.segmented)
                Text(mode == "task" ? "Keep working through checkpoints until finished or stopped." :
                     "One contribution per member; members can request another.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                HStack {
                    Text("Time limit (minutes)").frame(width: 160, alignment: .leading)
                    TextField("No limit", text: $minutes).textFieldStyle(.roundedBorder).accessibilityLabel("Time limit in minutes")
                }
                HStack {
                    Text("Reported token limit").frame(width: 160, alignment: .leading)
                    TextField("No limit", text: $tokens).textFieldStyle(.roundedBorder).accessibilityLabel("Reported token limit")
                }
                Picker("Context per member", selection: $contextTokens) {
                    ForEach(Array(Set([16000, 32000, 64000, 120000, 128000, 200000, 325000, 500000, 650000, 1000000, 2000000,
                                       Int(contextTokens) ?? 200000])).sorted(), id: \.self) { value in
                        Text(value.formatted() + " tokens").tag(String(value))
                    }
                }.help("Uses the smaller of this ceiling and the model’s window; space is reserved for output and notes.")
                Stepper("Background processes: \(processSlots)", value: $processSlots, in: 1...ChatTeamExecution.maxProcesses)
                    .help("Shared across this Team. Extra launches reserve capacity for active peers; 128 processes maximum across all chats.")
                Text("A new message or Resume starts a fresh run allowance; updates to active work keep it. Limits are checked between requests and actions, so work already running can go over. A token limit pauses if usage is unavailable. Spending limits are unavailable because providers do not report reliable charges.")
                    .font(.system(size: 10.5)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if execution == nil {
                    Text("Use whole numbers: 1–10,080 minutes or 1–2 billion tokens. Leave run limits blank for no limit.")
                        .font(.system(size: 10.5)).foregroundStyle(HubTheme.Semantic.contextCritical)
                }
            }.font(.system(size: 12)).padding(.top, 8)
        }
    }
}
