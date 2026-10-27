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
    @State private var browserTabs: [RemoteValue] = []
    @State private var browserTab = ""
    @State private var browserResult: RemoteValue = .null
    @State private var browserImage = ""
    @State private var browserRequest: String?
    @State private var browserTool = ""
    @State private var browserNotice = ""
    @State private var selectNewBrowserTab = false
    @State private var browserOwnerChat: String?
    @State private var closeSide = false
    @State private var sideRequest: String?
    @State private var sentSideDraft = ""
    @State private var expandedFiles = Set<String>()
    @State private var expandedProcesses = Set<String>()
    @State private var blackboard = false
    @AppStorage("companionSurface") private var surfaceStyle = "solid"

    private var side: RemoteValue { model.store.controls["side"] ?? .null }
    private var browser: RemoteValue { browserResult }
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
                    Button { if tab == .browser { refreshBrowserTabs() } else { model.refreshInspector(tab) } } label: { Image(systemName: "arrow.clockwise").frame(width: 32, height: 32) }
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
        .task(id: "\(model.selectedChatID ?? "")-\(model.store.selectedID ?? "")-\(tab.rawValue)") {
            if browserOwnerChat != model.selectedChatID {
                resetBrowser()
                browserOwnerChat = model.selectedChatID
            }
            if tab == .browser { refreshBrowserTabsWhenReady() } else { model.refreshInspector(tab) }
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
            if let request = browserRequest, acks[request] == false {
                browserRequest = nil
                browserNotice = "The Mac could not start this browser action. Refresh and try again."
            }
        }
        .onChange(of: model.isConnected) { _, connected in
            if !connected { sideRequest = nil; resetBrowser(); browserOwnerChat = nil }
            else if tab == .browser {
                browserOwnerChat = model.selectedChatID
                refreshBrowserTabsWhenReady()
            }
        }
        .onChange(of: model.store.selectedID) { _, selected in
            if selected != browserOwnerChat {
                resetBrowser()
                browserOwnerChat = selected
            }
            if tab == .browser { refreshBrowserTabsWhenReady() }
        }
        .onChange(of: model.store.controls["browserTabs"]) { _, value in receiveBrowserTabs(value) }
        .onChange(of: model.store.controls["browser"]) { _, value in receiveBrowserResult(value) }
        .onChange(of: model.store.controls["browserPreview"]) { _, value in
            guard let value, value["request"].text == browserRequest,
                  value["tab_id"].text == browserTab, browserTool == "browser_remote_preview" else { return }
            browserImage = value["image"].text
        }
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
            Text(model.team["execution"]["mode"].text == "contribution" ? "One contribution each. Members can continue when more work is needed." : "Up to twelve members working independently on the same task.")
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
                Text("Add up to twelve named models. The same Team runs on your Mac and appears in this inspector.")
                    .font(.callout).foregroundStyle(CompanionTheme.secondary)
            }
            if let todos = CompanionTodoLists(model.store.controls["todos"]) { CompanionTodos(model: model, lists: todos) }
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
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(browserTabs, id: \.self) { item in
                        let id = item["tab_id"].text
                        HStack(spacing: 4) {
                            Button {
                                clearBrowserPage()
                                browserAction("browser_select", ["tab_id": .string(id)])
                            } label: {
                                Text(item["title"].text.isEmpty ? (item["url"].text.isEmpty ? "New tab" : item["url"].text) : item["title"].text)
                                    .lineLimit(1).frame(maxWidth: 130)
                            }.buttonStyle(.plain).accessibilityLabel("Select browser tab \(item["title"].text)")
                            Button {
                                clearBrowserPage()
                                browserAction("browser_close", ["tab_id": .string(id)])
                            } label: { Image(systemName: "xmark").font(.caption) }
                                .buttonStyle(.plain).accessibilityLabel("Close browser tab \(item["title"].text)")
                        }.padding(8).background(id == browserTab ? CompanionTheme.cardFill : CompanionTheme.raised,
                                               in: RoundedRectangle(cornerRadius: 6))
                    }
                }
            }.disabled(browserRequest != nil)
            if let data = Data(base64Encoded: browserImage) {
                #if os(iOS)
                if let image = UIImage(data: data) { Image(uiImage: image).resizable().scaledToFit().clipShape(RoundedRectangle(cornerRadius: 8)) }
                #else
                if let image = NSImage(data: data) { Image(nsImage: image).resizable().scaledToFit().clipShape(RoundedRectangle(cornerRadius: 8)) }
                #endif
            }
            Button("Refresh Mac page preview") {
                browserImage = ""
                browserAction("browser_remote_preview", ["tab_id": .string(browserTab)])
            }.disabled(browserTab.isEmpty || browserRequest != nil)
            if model.store.controls["browserEnabled"]?.flag != true {
                Text("Enable Browser Use in the Mac’s Plugins settings and keep its Chat window open.").font(.callout)
            }
            HStack {
                TextField("https://…", text: $address).textFieldStyle(.roundedBorder)
                Button("Go") {
                    clearBrowserPage()
                    if browserTab.isEmpty {
                        selectNewBrowserTab = true
                        browserAction("browser_open", ["url": .string(address), "new_tab": .bool(true)])
                    } else {
                        browserAction("browser_navigate", ["url": .string(address), "tab_id": .string(browserTab)])
                    }
                }.disabled(address.isEmpty || browserRequest != nil)
                Button("New tab") {
                    selectNewBrowserTab = true
                    browserAction("browser_new", [:])
                }.disabled(browserRequest != nil)
            }
            if !browser["details"]["title"].text.isEmpty { Text(browser["details"]["title"].text).font(.headline) }
            Text(browser["details"]["url"].text).font(.caption).textSelection(.enabled)
            if !browser["text"].text.isEmpty { Text(browser["text"].text).font(.caption).textSelection(.enabled) }
            TextField("Text to enter into a page field", text: $browserInput).textFieldStyle(.roundedBorder)
            ForEach(browser["details"]["nodes"].rows, id: \.self) { node in browserNode(node) }
            Button("Observe page") {
                browserResult = .null
                browserAction("browser_observe", ["tab_id": .string(browserTab), "max_bytes": .number(10000), "depth": .number(3)])
            }.disabled(browserTab.isEmpty || browserRequest != nil)
            if !browserNotice.isEmpty { Text(browserNotice).font(.caption).foregroundStyle(CompanionTheme.secondary) }
        }
    }

    private func browserNode(_ node: RemoteValue) -> AnyView {
        AnyView(VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(node["name"].text.isEmpty ? node["text"].text : node["name"].text).font(.caption).textSelection(.enabled)
                Spacer()
                if !node["ref"].text.isEmpty {
                    Button("Click") { act(node, kind: "click") }.disabled(browserRequest != nil)
                    if ["input", "textarea", "select"].contains(node["tag"].text) || ["textbox", "searchbox", "combobox"].contains(node["role"].text) {
                        Button("Fill") { act(node, kind: "fill") }.disabled(browserRequest != nil)
                    }
                }
            }
            ForEach(node["children"].rows, id: \.self) { child in browserNode(child) }
        }.padding(.leading, 8))
    }
    private func browserAction(_ tool: String, _ arguments: [String: RemoteValue]) {
        guard browserRequest == nil else { return }
        browserTool = tool
        browserNotice = ""
        browserRequest = model.control("remote_browser", ["tool": .string(tool), "arguments": .object(arguments)])
    }
    private func act(_ node: RemoteValue, kind: String) {
        guard !browserTab.isEmpty, browser["details"]["tab_id"].text == browserTab,
              !browser["details"]["snapshot_id"].text.isEmpty else { return }
        var action = ["kind": RemoteValue.string(kind == "fill" ? "type" : kind), "ref": node["ref"]]
        if kind == "fill" { action["value"] = .string(browserInput) }
        browserAction("browser_act", ["tab_id": .string(browserTab), "snapshot_id": browser["details"]["snapshot_id"], "actions": .array([.object(action)])])
        browserResult = .null
    }

    private func clearBrowserPage() { browserResult = .null; browserImage = "" }

    private func resetBrowser() {
        browserTabs = []; browserTab = ""; browserRequest = nil; browserTool = ""
        browserNotice = ""; selectNewBrowserTab = false; clearBrowserPage()
    }

    private func refreshBrowserTabs() { browserAction("browser_list", [:]) }

    private func refreshBrowserTabsWhenReady() {
        guard model.selectedChatID == model.store.selectedID else { return }
        refreshBrowserTabs()
    }

    private func receiveBrowserTabs(_ value: RemoteValue?) {
        guard let value, value["request"].text == browserRequest, browserTool == "browser_list" else { return }
        browserRequest = nil
        let selected = value["details"]["selected_tab_id"].text
        if selected != browserTab { clearBrowserPage() }
        browserTab = selected
        browserTabs = value["details"]["tabs"].rows
        if value["details"]["truncated"].flag { browserNotice = "Some tabs are not shown. Close tabs on the Mac to see the rest." }
        if !browserTab.isEmpty, browserResult == .null {
            browserAction("browser_observe", ["tab_id": .string(browserTab), "max_bytes": .number(10000), "depth": .number(3)])
        }
    }

    private func receiveBrowserResult(_ value: RemoteValue?) {
        guard let value, value["request"].text == browserRequest else { return }
        let tool = browserTool
        browserRequest = nil
        if value["isError"].flag {
            selectNewBrowserTab = false
            browserNotice = value["text"].text.isEmpty ? value["summary"].text : value["text"].text
            return
        }
        if tool == "browser_observe" {
            guard value["details"]["tab_id"].text == browserTab else { return }
            browserResult = value
        } else if (tool == "browser_open" || tool == "browser_new"), selectNewBrowserTab {
            selectNewBrowserTab = false
            let newID = value["details"]["tab_id"].text
            if !newID.isEmpty { clearBrowserPage(); browserAction("browser_select", ["tab_id": .string(newID)]); return }
        } else if tool == "browser_open" || tool == "browser_navigate" {
            guard value["details"]["tab_id"].text == browserTab || value["details"]["tab_id"].text.isEmpty else { return }
            browserResult = value
        }
        if ["browser_new", "browser_open", "browser_close", "browser_select", "browser_navigate"].contains(tool) { refreshBrowserTabs() }
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
                if rows.count < CompanionTeamValidation.maxMembers { Button("Add member") { addMember() } }
                Section("Run settings") {
                    Picker("Mode", selection: $mode) { Text("Task").tag("task"); Text("Contribution").tag("contribution") }
                    Picker("Context per member", selection: $context) {
                        ForEach(Array(Set(CompanionTeamValidation.contextPresets + [Int(context) ?? 200000])).sorted(), id: \.self) { value in
                            Text(value.formatted() + " tokens").tag(String(value))
                        }
                    }.pickerStyle(.menu)
                    TextField("Custom context tokens", text: $context)
                    TextField("Concurrent processes", text: $processes)
                    TextField("Minutes (optional)", text: $minutes)
                    TextField("Token limit (optional)", text: $tokens)
                }
                if let validationMessage {
                    Section { Text(validationMessage).foregroundStyle(.red) }
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
                    }.disabled(validationMessage != nil)
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
    private var validationMessage: String? {
        CompanionTeamValidation.message(rows: rows, context: context, processes: processes, minutes: minutes, tokens: tokens)
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
