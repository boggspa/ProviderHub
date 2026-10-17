import SwiftUI
#if os(iOS)
import UIKit
#else
import AppKit
#endif

/// Same inspector sections and command semantics as the Mac, adapted to touch.
struct CompanionInspector: View {
    @ObservedObject var model: CompanionModel
    @Binding var tab: RemoteInspectorTab
    @State private var editingTeam = false
    @State private var sideDraft = ""
    @State private var sideChoice = ""
    @State private var sideEffort = ""
    @State private var sidePicker = false
    @State private var address = ""
    @State private var browserInput = ""
    @State private var closeSide = false
    @State private var sideRequest: String?
    @State private var sentSideDraft = ""
    @State private var expandedFiles = Set<String>()
    @State private var expandedProcesses = Set<String>()
    @State private var blackboard = false
    @AppStorage("companionSurface") private var surfaceStyle = "solid"

    private var side: RemoteValue { model.store.controls["side"] ?? .null }
    private var browser: RemoteValue { model.store.controls["browser"] ?? .null }
    private var connected: Bool { model.isConnected && model.controlSurfaceAvailable }

    var body: some View {
        VStack(spacing: 0) {
            ChatSharedInspectorTabs(items: RemoteInspectorTab.allCases.map {
                .init(id: $0.rawValue, label: $0.title, title: $0.title, icon: $0.icon,
                      count: $0 == .processes ? (model.store.controls["processes"]?.rows.filter { ["running", "stopping"].contains($0["status"].text) }.count ?? 0) : 0)
            }, selected: tab.rawValue, accent: activeAccent) { if let next = RemoteInspectorTab(rawValue: $0) { tab = next } }
            CompanionTheme.hairline.frame(height: 1)
            HStack {
                Text(tab == .changes ? changesSummary : tab.title)
                    .font(.system(size: tab == .agents ? 13 : 11.5, weight: tab == .agents ? .semibold : .regular))
                    .foregroundStyle(tab == .agents ? CompanionTheme.ink : CompanionTheme.secondary)
                Spacer()
                if model.extendedControlsAvailable {
                    Button { blackboard = true } label: { Image(systemName: "pin").frame(width: 32, height: 32) }
                        .buttonStyle(.plain).accessibilityLabel("Open Blackboard")
                }
                if tab == .agents {
                    Button(model.members.isEmpty ? "Set up" : "Edit") { editingTeam = true }.controlSize(.small).disabled(!connected)
                } else {
                    Button { model.refreshInspector(tab) } label: { Image(systemName: "arrow.clockwise").frame(width: 32, height: 32) }
                        .buttonStyle(.plain).accessibilityLabel("Refresh \(tab.title)")
                }
            }.padding(.horizontal, 14).padding(.top, 10)
            if !model.controlSurfaceAvailable {
                Text("Update the Mac host to enable the inspector and configuration controls.")
                    .font(.callout).foregroundStyle(CompanionTheme.secondary).padding()
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        switch tab {
                        case .agents: teamPane
                        case .changes: changesPane
                        case .processes: processesPane
                        case .side: sidePane
                        case .browser: browserPane
                        }
                    }.padding(14).frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            if let notice = model.store.notices.last {
                Text(notice).font(.caption).foregroundStyle(CompanionTheme.secondary).textSelection(.enabled).padding(12)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(CompanionTheme.pane(.inspector, surfaceStyle: surfaceStyle)).foregroundStyle(CompanionTheme.ink)
        .disabled(!model.isConnected)
        .task(id: "\(model.selectedChatID ?? "")-\(tab.rawValue)") {
            model.refreshInspector(tab)
            while !Task.isCancelled && (tab == .processes || tab == .changes) {
                do { try await Task.sleep(nanoseconds: 2_500_000_000) } catch { return }
                if !Task.isCancelled { model.refreshInspector(tab) }
            }
        }
        .onChange(of: model.store.acks) { _, acks in
            if let request = sideRequest, let ok = acks[request] {
                if ok && sideDraft == sentSideDraft { sideDraft = "" }
                sideRequest = nil
            }
        }
        .onChange(of: model.isConnected) { _, connected in if !connected { sideRequest = nil } }
        .sheet(isPresented: $editingTeam) { CompanionTeamEditor(model: model) }
        .sheet(isPresented: $blackboard) { CompanionBlackboard(model: model) }
        .sheet(isPresented: $sidePicker) {
            CompanionRoutePicker(model: model, initialChoiceID: sideChoice, initialEffort: sideEffort) { row, effort in
                sideChoice = row["id"].text; sideEffort = effort
                if !side["id"].text.isEmpty { model.control("side_model", ["side": side["id"], "choice": row["id"], "effort": .string(effort)]) }
                sidePicker = false
            }
        }
        .confirmationDialog("Close this temporary Side Chat?", isPresented: $closeSide) {
            Button("Close Side Chat", role: .destructive) { model.control("close_side", ["side": side["id"]]) }
        } message: { Text("Its temporary conversation will be discarded on the Mac.") }
    }

    private var teamPane: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(model.team["execution"]["mode"].text == "contribution" ? "One contribution each. Members can continue when more work is needed." : "Up to eight members working independently on the same task.")
                .font(.system(size: 11.5)).foregroundStyle(CompanionTheme.secondary)
            if !model.members.isEmpty {
                if !model.team["enabled"].flag { Text("Team is off").font(.system(size: 11.5)).foregroundStyle(CompanionTheme.secondary) }
                ForEach(model.members, id: \.self) { member in
                    ChatSharedMemberRow(name: member["name"].text, model: member["label"].text,
                        status: member["status"].text, responsibility: member["responsibility"].text, nextStep: member["nextStep"].text,
                        failure: member["failureReason"].text, waitReason: member["waitReason"].text,
                        pendingLabel: member["pendingChange"]["label"].text.isEmpty ? nil : member["pendingChange"]["label"].text,
                        pending: member["pendingChange"] != .null, used: member["usage"].optionalInt, limit: member["context"].optionalInt,
                        canControl: connected && model.team["enabled"].flag, stoppedByUser: member["memberStoppedByUser"].flag,
                        needsInput: model.members.contains { $0["status"].text == "needs_input" },
                        accent: activeAccent, fill: CompanionTheme.cardFill, ink: CompanionTheme.ink, secondary: CompanionTheme.secondary,
                        control: { start in model.control(start ? "team_member_start" : "team_member_stop", ["member": member["id"]]) }) {
                            CompanionProviderMark(presentation: CompanionBranding.presentation(route: member["route"].text.isEmpty ? String(member["choice"].text.split(separator: "|").first ?? "") : member["route"].text, label: member["label"].text, models: model.models), size: 14)
                        }
                }
                if ["stopped", "interrupted", "error", "limit_reached"].contains(model.team["status"].text) {
                    Button("Resume Team") { model.control("team_resume") }.buttonStyle(.bordered)
                }
            } else {
                Text("Add up to eight named models. The same Team runs on your Mac and appears in this inspector.")
                    .font(.callout).foregroundStyle(CompanionTheme.secondary)
            }
            ForEach(model.store.controls["agents"]?.rows ?? [], id: \.self) { agent in
                DisclosureGroup("\(agent["label"].text) · \(agent["status"].text)") { entries(agent["entries"]) }
            }
            if model.extendedControlsAvailable { CompanionBlackboardSummary(model: model) { blackboard = true } }
        }
    }

    private var changesPane: some View {
        let changes = model.store.controls["changes"] ?? .null
        return VStack(alignment: .leading, spacing: 12) {
            if changes["files"].rows.isEmpty { empty("No file changes", "Refresh to inspect the selected chat’s checkout.") }
            ForEach(changes["files"].rows, id: \.self) { file in fileRow(file) }
            if !changes["commits"].rows.isEmpty { Text("Committed this turn").font(.system(size: 11, weight: .medium)).foregroundStyle(CompanionTheme.secondary) }
            ForEach(changes["commits"].rows, id: \.self) { commit in
                DisclosureGroup("\(commit["hash"].text.prefix(7)) · \(commit["subject"].text)") {
                    ForEach(commit["files"].rows, id: \.self) { file in fileRow(file) }
                }.font(.caption)
            }
            if changes["truncated"].flag { Text("Showing a bounded preview. Full diffs remain on the Mac.").font(.caption) }
        }
    }

    private func fileRow(_ file: RemoteValue) -> some View {
        ChatSharedDiffDisclosure(path: file["path"].text, oldPath: file["oldPath"].text.isEmpty ? nil : file["oldPath"].text,
            added: file["added"].int, deleted: file["deleted"].int, binary: file["binary"].flag, diff: file["diff"].text,
            expanded: Binding(get: { expandedFiles.contains(file["path"].text) }, set: { value in
                if value { expandedFiles.insert(file["path"].text) } else { expandedFiles.remove(file["path"].text) }
            })) { EmptyView() }
    }

    private var processesPane: some View {
        let rows = model.store.controls["processes"]?.rows ?? []
        return VStack(alignment: .leading, spacing: 12) {
            if rows.isEmpty { empty("No background processes", "Processes started by this chat and its Team appear here.") }
            ForEach(rows, id: \.self) { process in
                let brand = CompanionBranding.presentation(route: process["route"].text, models: model.models)
                ChatSharedProcessRow(owner: model.members.first(where: { $0["id"] == process["memberID"] })?["name"].text ?? brand.label,
                    command: process["command"].text, canStop: process["status"].text == "running", expanded: Binding(
                        get: { expandedProcesses.contains(process["id"].text) }, set: { value in
                            if value { expandedProcesses.insert(process["id"].text) } else { expandedProcesses.remove(process["id"].text) }
                        }), stop: { model.control("stop_process", ["process": process["id"]]) },
                    mark: { CompanionProviderMark(presentation: brand, size: 14) },
                    status: { Text(process["status"].text.capitalized).font(.system(size: 10.5)).foregroundStyle(CompanionTheme.secondary) },
                    detail: { ChatSharedPatchText(text: process["output"].text, ink: CompanionTheme.ink, secondaryInk: CompanionTheme.secondary) })
            }
            if !rows.isEmpty { Button("Clear finished processes") { model.control("clear_processes") } }
        }
    }

    private var sidePane: some View {
        VStack(alignment: .leading, spacing: 12) {
            if side["id"].text.isEmpty {
                Text("Fork a temporary discussion from this chat. It uses the same Mac workspace with read-only tools.")
                    .font(.callout).foregroundStyle(CompanionTheme.secondary)
                Button(model.models.first(where: { $0["id"].text == sideChoice })?["label"].text ?? "Choose a model") { sidePicker = true }
                Button("Open Side Chat") { model.control("open_side", ["choice": .string(sideChoice), "effort": .string(sideEffort)]) }.disabled(sideChoice.isEmpty)
            } else {
                HStack {
                    Button(side["label"].text) { sidePicker = true }.disabled(side["busy"].flag)
                    Spacer()
                    if side["busy"].flag { Button("Stop") { model.control("side_stop", ["side": side["id"]]) } }
                    Button("Close") { closeSide = true }
                }
                Text(side["status"].text).font(.caption).foregroundStyle(CompanionTheme.secondary)
                entries(side["entries"])
                TextField("Message Side Chat", text: $sideDraft, axis: .vertical).textFieldStyle(.roundedBorder)
                Button("Send") {
                    sentSideDraft = sideDraft
                    sideRequest = model.control("side_send", ["text": .string(sideDraft), "side": side["id"]])
                }.disabled(sideDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || side["busy"].flag || sideRequest != nil)
                if !side["notice"].text.isEmpty { Text(side["notice"].text).font(.caption) }
            }
        }
    }

    private var browserPane: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("The Mac’s shared browser session").font(.caption).foregroundStyle(CompanionTheme.secondary)
            if let data = Data(base64Encoded: model.store.controls["browserPreview"]?.text ?? "") {
                #if os(iOS)
                if let image = UIImage(data: data) { Image(uiImage: image).resizable().scaledToFit().clipShape(RoundedRectangle(cornerRadius: 8)) }
                #else
                if let image = NSImage(data: data) { Image(nsImage: image).resizable().scaledToFit().clipShape(RoundedRectangle(cornerRadius: 8)) }
                #endif
            }
            Button("Refresh Mac page preview") { browserAction("browser_remote_preview", [:]) }
            if model.store.controls["browserEnabled"]?.flag != true {
                Text("Enable Browser Use in the Mac’s Plugins settings and keep its Chat window open.").font(.callout)
            }
            HStack {
                TextField("https://…", text: $address).textFieldStyle(.roundedBorder)
                Button("Go") { browserAction("browser_open", ["url": .string(address)]) }.disabled(address.isEmpty)
            }
            if !browser["details"]["title"].text.isEmpty { Text(browser["details"]["title"].text).font(.headline) }
            Text(browser["details"]["url"].text).font(.caption).textSelection(.enabled)
            if !browser["text"].text.isEmpty { Text(browser["text"].text).font(.caption).textSelection(.enabled) }
            TextField("Text to enter into a page field", text: $browserInput).textFieldStyle(.roundedBorder)
            ForEach(browser["details"]["nodes"].rows, id: \.self) { node in browserNode(node) }
            Button("Observe page") { model.refreshInspector(.browser) }
        }
    }

    private func browserNode(_ node: RemoteValue) -> AnyView {
        AnyView(VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(node["name"].text.isEmpty ? node["text"].text : node["name"].text).font(.caption).textSelection(.enabled)
                Spacer()
                if !node["ref"].text.isEmpty {
                    Button("Click") { act(node, kind: "click") }
                    if ["input", "textarea", "select"].contains(node["tag"].text) || ["textbox", "searchbox", "combobox"].contains(node["role"].text) {
                        Button("Fill") { act(node, kind: "fill") }
                    }
                }
            }
            ForEach(node["children"].rows, id: \.self) { child in browserNode(child) }
        }.padding(.leading, 8))
    }
    private func browserAction(_ tool: String, _ arguments: [String: RemoteValue]) {
        model.control("remote_browser", ["tool": .string(tool), "arguments": .object(arguments)])
    }
    private func act(_ node: RemoteValue, kind: String) {
        var action = ["kind": RemoteValue.string(kind == "fill" ? "type" : kind), "ref": node["ref"]]
        if kind == "fill" { action["value"] = .string(browserInput) }
        browserAction("browser_act", ["snapshot_id": browser["details"]["snapshot_id"], "actions": .array([.object(action)])])
    }
    private func entries(_ value: RemoteValue) -> some View {
        ForEach(value.rows, id: \.self) { entry in
            card {
                Text(entry["memberName"].text.isEmpty ? entry["role"].text.capitalized : entry["memberName"].text).font(.caption.weight(.semibold)).foregroundStyle(CompanionTheme.secondary)
                Text(entry["text"].text.isEmpty ? entry["summary"].text : entry["text"].text).font(.callout).textSelection(.enabled)
            }
        }
    }
    private func empty(_ title: String, _ message: String) -> some View {
        VStack(alignment: .leading, spacing: 6) { Text(title).font(.subheadline); Text(message).font(.caption).foregroundStyle(CompanionTheme.secondary) }
    }
    private func card<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10, content: content).padding(12).frame(maxWidth: .infinity, alignment: .leading)
            .background(CompanionTheme.raised, in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(CompanionTheme.hairline, lineWidth: 0.5))
    }
    private var activeAccent: Color {
        CompanionBranding.presentation(route: String(model.configuration["choice"].text.split(separator: "|").first ?? ""), models: model.models).accent
    }
    private var changesSummary: String {
        let changes = model.store.controls["changes"] ?? .null
        let count = changes["files"].rows.count
        var text = "\(count) changed \(count == 1 ? "file" : "files")"
        let commits = changes["commits"].rows.count
        if commits > 0 { text += " · \(commits) \(commits == 1 ? "commit" : "commits") this turn" }
        return text
    }
}

private struct CompanionTeamEditor: View {
    @ObservedObject var model: CompanionModel
    @Environment(\.dismiss) private var dismiss
    @State private var rows: [RemoteValue] = []
    @State private var enabled = true
    @State private var mode = "task"
    @State private var context = "200000"
    @State private var processes = "8"
    @State private var minutes = ""
    @State private var tokens = ""
    @State private var pickingMember: Int?

    var body: some View {
        NavigationStack {
            Form {
                Section("Team") {
                    Toggle("Enable Team", isOn: $enabled).disabled(model.configuration["busy"].flag)
                    Text("Edits to a working member apply at its next safe boundary.").font(.caption)
                }
                ForEach(rows.indices, id: \.self) { index in
                    Section("Member \(index + 1)") {
                        TextField("Name", text: field(index, "name"))
                        Button { pickingMember = index } label: {
                            HStack {
                                Text("Model and reasoning")
                                Spacer()
                                Text((model.models.first { $0["id"] == rows[index]["choice"] })?["label"].text ?? "Choose model")
                                Text(rows[index]["effort"].text).foregroundStyle(.secondary)
                                Image(systemName: "chevron.right").font(.caption)
                            }
                        }.buttonStyle(.plain)
                        TextField("Responsibility", text: field(index, "responsibility"), axis: .vertical)
                        if rows.count > 1 { Button("Remove member", role: .destructive) { rows.remove(at: index) } }
                    }
                }
                if rows.count < 8 { Button("Add member") { addMember() } }
                Section("Run settings") {
                    Picker("Mode", selection: $mode) { Text("Task").tag("task"); Text("Contribution").tag("contribution") }
                    TextField("Context tokens", text: $context)
                    TextField("Concurrent processes", text: $processes)
                    TextField("Minutes (optional)", text: $minutes)
                    TextField("Token limit (optional)", text: $tokens)
                }
            }
            .navigationTitle("Edit Team")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Apply") {
                        let execution: RemoteValue = .object(["mode": .string(mode), "contextTokens": .number(Double(context) ?? 200000),
                            "processes": .number(Double(processes) ?? 8), "minutes": Double(minutes).map { .number($0) } ?? .null,
                            "tokens": Double(tokens).map { .number($0) } ?? .null])
                        model.configureMembers(rows, enabled: enabled, execution: execution)
                        dismiss()
                    }.disabled(!valid)
                }
            }
        }.sheet(isPresented: Binding(get: { pickingMember != nil }, set: { if !$0 { pickingMember = nil } })) {
            if let index = pickingMember, rows.indices.contains(index) {
                CompanionRoutePicker(model: model, initialChoiceID: rows[index]["choice"].text, initialEffort: rows[index]["effort"].text, toolsOnly: true) { row, effort in
                    guard rows.indices.contains(index) else { return }
                    var member = rows[index].object
                    member["choice"] = row["id"]; member["effort"] = .string(effort)
                    rows[index] = .object(member); pickingMember = nil
                }
            }
        }.onAppear {
            rows = model.members
            enabled = model.team["enabled"].flag || rows.isEmpty
            if rows.isEmpty { addMember() }
            let execution = model.team["execution"]
            mode = execution["mode"].text.isEmpty ? "task" : execution["mode"].text
            if execution["contextTokens"].int > 0 { context = String(execution["contextTokens"].int) }
            if execution["processes"].int > 0 { processes = String(execution["processes"].int) }
            if execution["minutes"].int > 0 { minutes = String(execution["minutes"].int) }
            if execution["tokens"].int > 0 { tokens = String(execution["tokens"].int) }
        }
    }
    private var valid: Bool {
        !rows.isEmpty && rows.allSatisfy { !$0["name"].text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !$0["choice"].text.isEmpty } &&
        Set(rows.map { $0["name"].text.lowercased().trimmingCharacters(in: .whitespacesAndNewlines) }).count == rows.count &&
        (16000...2000000).contains(Int(context) ?? 0) && (1...8).contains(Int(processes) ?? 0) &&
        (minutes.isEmpty || (1...10080).contains(Int(minutes) ?? 0)) && (tokens.isEmpty || (1...2000000000).contains(Int(tokens) ?? 0))
    }
    private func field(_ index: Int, _ key: String) -> Binding<String> {
        Binding(get: { rows.indices.contains(index) ? rows[index][key].text : "" }, set: { value in
            guard rows.indices.contains(index) else { return }
            var row = rows[index].object; row[key] = .string(value)
            if key == "choice" { row["effort"] = .string("") }
            rows[index] = .object(row)
        })
    }
    private func addMember() {
        let choice = rows.isEmpty ? model.configuration["choice"] : model.models.first(where: { $0["supportsTools"].flag })?["id"] ?? .string("")
        rows.append(.object(["name": .string("Member \(rows.count + 1)"), "choice": choice, "effort": .string(""), "responsibility": .string("")]))
    }
}
