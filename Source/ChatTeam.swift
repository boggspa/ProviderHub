import SwiftUI

/// Small roster controls in the existing inspector; members speak in the main
/// transcript and share its composer. There is no separate orchestration view.
struct ChatTeamControls: View {
    @ObservedObject var model: ChatModel
    @State private var editing = false
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Team").font(.system(size: 13, weight: .semibold))
                Spacer()
                Button(model.team == nil ? "Set up" : "Edit") { editing = true }
                    .controlSize(.small).disabled(!model.canConfigureTeam)
            }
            Text("Up to 3 members working together. One contribution each; members opt in when more work is needed.")
                .font(.system(size: 11.5)).foregroundStyle(.secondary)
            if let team = model.team {
                if !team.enabled { Text("Team is off").font(.system(size: 11.5)).foregroundStyle(.secondary) }
                ForEach(team.members) { member in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 6) {
                            ChatProviderIcon(presentation: model.models.first { $0.id == member.choice }?.presentation)
                            Text(member.name).font(.system(size: 12, weight: .medium))
                            Spacer(minLength: 0)
                            if member.status == "working" { ProgressView().controlSize(.mini) }
                            Text(member.status.replacingOccurrences(of: "_", with: " ").capitalized)
                                .font(.system(size: 10.5)).foregroundStyle(member.status == "error" ? HubTheme.Semantic.contextCritical : HubTheme.Semantic.secondaryInk)
                        }
                        Text(member.label + (member.account.isEmpty ? "" : " · " + member.account))
                            .font(.system(size: 10.5)).foregroundStyle(.secondary).lineLimit(2)
                        if !member.responsibility.isEmpty {
                            Text(member.responsibility).font(.system(size: 11.5)).lineLimit(3)
                        }
                        if member.status == "error", let reason = member.failureReason, !reason.isEmpty {
                            Text(reason).font(.system(size: 11.5)).foregroundStyle(HubTheme.Semantic.contextCritical).lineLimit(2).textSelection(.enabled)
                        } else if !member.nextStep.isEmpty, member.status != "done" {
                            Text(member.nextStep).font(.system(size: 11.5)).foregroundStyle(.secondary).textSelection(.enabled)
                        }
                        if let usage = member.usage {
                            Text("Context \(usage.formatted())" + (member.context.map { " / \($0.formatted())" } ?? ""))
                                .font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary)
                        }
                    }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
                        .background(HubTheme.Semantic.surface, in: RoundedRectangle(cornerRadius: 7))
                }
                if team.needsInput {
                    Text("Answer in the main composer to continue.").font(.system(size: 11.5)).foregroundStyle(.secondary)
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
            Text("Members think and read together. Workspace changes and approvals queue one at a time, using this chat’s approval mode.")
                .font(.system(size: 10.5)).foregroundStyle(.secondary)
            if !model.teamNotice.isEmpty {
                Text(model.teamNotice).font(.system(size: 11.5)).foregroundStyle(.secondary).textSelection(.enabled)
            }
            if model.teamRequest != nil { ProgressView().controlSize(.small) }
        }.padding(14)
        .sheet(isPresented: $editing) { ChatTeamEditor(model: model) }
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

private struct ChatTeamEditor: View {
    @ObservedObject var model: ChatModel
    @Environment(\.dismiss) private var dismiss
    @State private var enabled = true
    @State private var drafts: [TeamDraft] = []
    @State private var picking: String?
    private var valid: Bool {
        (1...3).contains(drafts.count) && drafts.allSatisfy { draft in
            !draft.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && draft.name.count <= 60 &&
            draft.responsibility.count <= 2000 && model.models.contains { $0.id == draft.choice && $0.supportsTools }
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack { Text("Configure Team").font(.headline); Spacer(); Toggle("Use Team", isOn: $enabled).toggleStyle(.switch) }
            Text("The first member replaces the current chat model while Team is on. Every member keeps a private model history and shares visible work in this transcript.")
                .font(.system(size: 12)).foregroundStyle(.secondary)
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
                        }.padding(10).background(HubTheme.Semantic.surface, in: RoundedRectangle(cornerRadius: 8))
                    }
                }
            }.frame(maxHeight: 410)
            HStack {
                Button("Add member") { add() }.disabled(drafts.count >= 3 || model.models.allSatisfy { !$0.supportsTools })
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Save Team") {
                    model.configureTeam(enabled: enabled, members: drafts.map(\.wire)); dismiss()
                }.keyboardShortcut(.defaultAction).disabled(!valid || !model.canConfigureTeam)
            }
        }.padding(20).frame(width: 480)
        .onAppear {
            if let team = model.team {
                enabled = team.enabled
                drafts = team.members.map { TeamDraft(id: $0.id, existingID: $0.id, name: $0.name,
                                                      choice: $0.choice, effort: $0.effort, responsibility: $0.responsibility) }
            } else { add() }
        }
    }
    private func add() {
        guard let choice = model.selectedRoute.flatMap({ $0.supportsTools ? $0 : nil }) ?? model.models.first(where: \.supportsTools) else { return }
        let currentEffort = model.selected?.effort ?? ""
        drafts.append(TeamDraft(id: UUID().uuidString, name: "Member \(drafts.count + 1)",
                                choice: choice.id, effort: choice.efforts.contains(currentEffort) ? currentEffort : "", responsibility: ""))
    }
}
