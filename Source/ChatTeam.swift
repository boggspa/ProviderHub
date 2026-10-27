import SwiftUI

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
                            }.buttonStyle(.plain).padding(.horizontal, 7).padding(.vertical, 4)
                                .background(HubTheme.Semantic.cardFill, in: Capsule())
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
                    .dropDestination(for: String.self) { values, _ in
                        guard let value = values.first, let chat = model.stateChatID,
                              value.hasPrefix(chat + ":"),
                              let task = model.teamTasks.first(where: { $0.id == String(value.dropFirst(chat.count + 1)) }) else { return false }
                        assign(task, to: member.id)
                        return true
                    }
                }
                if model.teamTasks.contains(where: { $0.owner.isEmpty }) {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 5) {
                            Text("Unassigned").font(.system(size: 10.5)).foregroundStyle(.secondary)
                            ForEach(model.teamTasks.filter { $0.owner.isEmpty }) { task in
                                ChatTeamTaskChip(task: task, model: model, members: team.members)
                            }
                        }
                    }
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

private struct ChatTeamTaskChip: View {
    var task: ChatTeamTask
    @ObservedObject var model: ChatModel
    var members: [ChatTeamMember]
    @State private var showing = false
    private var label: String { task.status.replacingOccurrences(of: "_", with: " ").capitalized }
    var body: some View {
        Button { showing.toggle() } label: {
            HStack(spacing: 4) {
                Image(systemName: task.status == "completed" ? "checkmark.circle.fill" :
                      task.status == "blocked" ? "exclamationmark.circle" : task.status == "paused" ? "pause.circle" : "circle")
                Text(task.title).lineLimit(1)
            }.font(.system(size: 10.5)).padding(.horizontal, 8).padding(.vertical, 4)
                .background(HubTheme.Semantic.accentOnSurface.opacity(0.14), in: Capsule())
        }.buttonStyle(.plain).help("\(label) · drag to reassign")
        .draggable((model.stateChatID ?? "") + ":" + task.id)
        .popover(isPresented: $showing) {
            VStack(alignment: .leading, spacing: 8) {
                Text(task.title).font(.headline).textSelection(.enabled)
                Text("Status: \(label)").font(.caption).foregroundStyle(.secondary)
                ForEach(["pending", "in_progress", "blocked", "paused", "completed"], id: \.self) { status in
                    Button(status.replacingOccurrences(of: "_", with: " ").capitalized) {
                        update(owner: task.owner, status: status); showing = false
                    }.disabled(status == task.status)
                }
                Divider()
                Menu("Assign to") {
                    ForEach(members) { member in
                        Button(member.name) { update(owner: member.id); showing = false }
                    }
                    Button("Unassigned") { update(owner: ""); showing = false }
                }
            }.padding(14).frame(width: 260, alignment: .leading)
        }
    }
    private func update(owner: String, status: String? = nil) {
        var command: [String: Any] = ["command": "team_task_user", "task": task.id, "owner": owner,
                                      "expectedOwner": task.owner]
        if let status { command["status"] = status }
        model.inspectorCommand(command)
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
