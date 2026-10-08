import AppKit
import SwiftUI

enum ChatInspectorTab: String, CaseIterable, Identifiable {
    case changes, agents, side
    var id: String { rawValue }
    var title: String { switch self { case .changes: "File Changes"; case .agents: "Subagents"; case .side: "Side Chat" } }
    var icon: String { switch self { case .changes: "filemenu.and.selection"; case .agents: "person.2"; case .side: "bubble.left.and.bubble.right" } }
}

struct ChatChange: Decodable, Identifiable {
    var path: String
    var oldPath: String?
    var status: String
    var added: Int
    var deleted: Int
    var binary: Bool
    var diff: String
    var id: String { path }
}
struct ChatChanges: Decodable { var files: [ChatChange]; var truncated: Bool }
struct ChatBranch: Decodable, Identifiable {
    var name: String; var current: Bool; var worktree: String?
    var nameBytes: String?
    var id: String { nameBytes ?? name }
}
struct ChatWorktree: Decodable, Identifiable {
    var path: String; var branch: String?; var current: Bool; var locked: Bool?; var prunable: Bool?
    var pathBytes: String?
    var selectable: Bool?
    var id: String { pathBytes ?? path }
}
struct ChatBranchesSnapshot: Decodable {
    var root: String; var current: String?; var branches: [ChatBranch]; var worktrees: [ChatWorktree]
}
struct ChatAgent: Decodable, Identifiable {
    var id: String; var route: String; var account: String; var label: String; var task: String; var status: String
    var entries: [ChatEntry]; var usage: Int?; var changedFiles: [String]
    var workspace: String?
    var truncated: Bool?
    var readOnly: Bool?
    var busy: Bool { ["working", "running", "approval"].contains(status) }
}
struct ChatSide: Decodable, Identifiable {
    var id: String; var route: String; var account: String; var label: String; var effort: String
    var status: String; var busy: Bool; var entries: [ChatEntry]; var usage: Int?; var notice: String?
    var interrupting: Bool?
    var workspace: String?
    var truncated: Bool?
}

@MainActor extension ChatModel {
    var canChangeBranch: Bool { connected && selectedID != nil && !busy && !branchBusy && sideChat?.busy != true && !sideOpening }
    var canSendSide: Bool {
        connected && sideChat != nil && !branchBusy && !sideOpening && pendingSideText == nil && sideChat?.interrupting != true
            && !sideDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
    func showInspector(_ tab: ChatInspectorTab) {
        inspectorTab = tab; inspectorVisible = true
    }
    func refreshChanges() {
        guard connected, let selectedID else { return }
        if gitChangesLoading { changesRefreshPending = true; return }
        let request = UUID().uuidString; changesRequest = request; changesRefreshPending = false
        gitChangesLoading = true; inspectorNotice = ""
        if !inspectorCommand(["command": "inspect_git", "id": selectedID, "request": request]) { gitChangesLoading = false; changesRequest = nil }
    }
    func refreshBranches() {
        guard connected, let selectedID, !branchBusy else { return }
        if branchesLoading { branchesRefreshPending = true; return }
        let request = UUID().uuidString; branchesRequest = request; branchesRefreshPending = false
        branchesLoading = true; branchNotice = ""
        if !inspectorCommand(["command": "branches", "id": selectedID, "request": request]) { branchesLoading = false; branchesRequest = nil }
    }
    func branchAction(_ action: String, branch: String? = nil, path: String? = nil, branchBytes: String? = nil) {
        guard canChangeBranch, let selectedID else { return }
        var command: [String: Any] = ["command": "branch_action", "id": selectedID, "action": action]
        if let branch { command["branch"] = branch }
        if let path { command["path"] = path }
        if let branchBytes { command["branchBytes"] = branchBytes }
        let request = UUID().uuidString; command["request"] = request
        branchRequest = request; branchesRequest = request; branchesRefreshPending = false; branchesLoading = false
        branchBusy = true; branchNotice = ""
        if !inspectorCommand(command) { branchBusy = false; branchRequest = nil; branchesRequest = nil; branchNotice = "Chat is disconnected." }
    }
    func openSideChat(choice: String, effort: String) {
        guard connected, let selectedID, !branchBusy, !sideOpening, sideChat == nil else { return }
        sideOpening = true; sideNotice = ""
        let request = UUID().uuidString
        sideRequests[selectedID] = request
        if !inspectorCommand(["command": "open_side", "id": selectedID, "choice": choice, "effort": effort, "request": request]) {
            sideOpening = false; sideNotice = "Chat is disconnected."
        }
    }
    func setSideModel(choice: String, effort: String) {
        guard let selectedID, let side = sideChat, !side.busy, !branchBusy, pendingSideText == nil else { return }
        inspectorCommand(["command": "side_model", "id": selectedID, "side": side.id, "choice": choice, "effort": effort])
    }
    func sendSide() {
        guard canSendSide, let selectedID, let side = sideChat else { return }
        pendingSideText = sideDraft; sideNotice = ""
        if !inspectorCommand(["command": "side_send", "id": selectedID, "side": side.id, "text": sideDraft]) {
            pendingSideText = nil; sideNotice = "Chat is disconnected."
        }
    }
    func stopSide() {
        guard let selectedID, let side = sideChat, side.busy else { return }
        inspectorCommand(["command": "side_stop", "id": selectedID, "side": side.id])
    }
    func closeSideChat() {
        if let selectedID {
            var command: [String: Any] = ["command": "close_side", "id": selectedID]
            if let side = sideChat { command["side"] = side.id }
            if let request = sideRequests[selectedID] { command["request"] = request }
            inspectorCommand(command)
            sideDrafts[selectedID] = nil
            sideRequests[selectedID] = nil
        }
        sideChat = nil; sideDraft = ""; sideNotice = "Temporary conversation discarded."; sideOpening = false; pendingSideText = nil
    }
    func resetInspector(clearSessions: Bool = false) {
        if let selectedID { sideDrafts[selectedID] = sideDraft }
        if clearSessions { sideRequests = [:]; sideDrafts = [:] }
        gitChanges = nil; gitChangesLoading = false; branches = nil; branchesLoading = false; branchBusy = false
        changesRequest = nil; changesRefreshPending = false; branchesRequest = nil; branchesRefreshPending = false; branchRequest = nil
        inspectorNotice = ""; branchNotice = ""; agents = []; inspectedAgentID = nil
        sideChat = nil; sideDraft = ""; sideNotice = ""; sideOpening = false; pendingSideText = nil
    }
    func consumeInspector(_ event: [String: Any]) {
        guard let chat = event["chat"] as? String, chat == selectedID else { return }
        func decoded<T: Decodable>(_ value: Any?, as type: T.Type) -> T? {
            guard let value, JSONSerialization.isValidJSONObject(value), let data = try? JSONSerialization.data(withJSONObject: value) else { return nil }
            return try? JSONDecoder().decode(type, from: data)
        }
        switch event["event"] as? String {
        case "git_changes":
            if let workspace = event["workspace"] as? String, workspace != selected?.workspace { return }
            guard event["request"] as? String == changesRequest else { return }
            if changesRefreshPending { gitChangesLoading = false; refreshChanges(); return }
            gitChanges = decoded(event["changes"], as: ChatChanges.self); gitChangesLoading = false
            changesRequest = nil
            inspectorNotice = event["notice"] as? String ?? ""
        case "branches":
            if let workspace = event["workspace"] as? String, workspace != selected?.workspace { return }
            guard event["request"] as? String == branchesRequest else { return }
            if branchesRefreshPending && !branchBusy { branchesLoading = false; refreshBranches(); return }
            branches = decoded(event["branches"], as: ChatBranchesSnapshot.self); branchesLoading = false
            branchesRequest = nil; branchesRefreshPending = false
            if let notice = event["notice"] as? String { branchNotice = notice }
        case "branch_state":
            guard event["request"] as? String == branchRequest else { return }
            branchBusy = event["busy"] as? Bool ?? false
            branchNotice = event["notice"] as? String ?? ""
            if !branchBusy { branchRequest = nil; refreshGitStatus(); if inspectorVisible && inspectorTab == .changes { refreshChanges() } }
        case "agents":
            agents = decoded(event["agents"], as: [ChatAgent].self) ?? []
        case "agent_entry":
            guard let id = event["agent"] as? String, let index = agents.firstIndex(where: { $0.id == id }),
                  let entry = decoded(event["entry"], as: ChatEntry.self) else { return }
            if let row = agents[index].entries.firstIndex(where: { $0.id == entry.id }) { agents[index].entries[row] = entry }
            else { agents[index].entries.append(entry) }
        case "agent_delta":
            guard let id = event["agent"] as? String, let index = agents.firstIndex(where: { $0.id == id }),
                  let entryID = event["id"] as? String, let row = agents[index].entries.firstIndex(where: { $0.id == entryID }) else { return }
            agents[index].entries[row].text += event["text"] as? String ?? ""
        case "side":
            guard let request = event["request"] as? String, request == sideRequests[chat] else { return }
            let previousUserIDs = Set(sideChat?.entries.filter { $0.kind == "user" }.map(\.id) ?? [])
            guard let incoming = decoded(event["side"], as: ChatSide.self) else {
                sideChat = nil; sideOpening = false; pendingSideText = nil
                sideNotice = event["notice"] as? String ?? ""; return
            }
            // Only the request owned by this parent can open/restore its side.
            sideChat = incoming; sideOpening = false; sideNotice = incoming.notice ?? ""
            if let pending = pendingSideText, incoming.entries.contains(where: { $0.kind == "user" && $0.text == pending && !previousUserIDs.contains($0.id) }) {
                if sideDraft == pending { sideDraft = "" }; pendingSideText = nil
            }
            if !sideNotice.isEmpty { pendingSideText = nil }
        case "side_delta":
            guard let request = event["request"] as? String, request == sideRequests[chat] else { return }
            guard let id = event["side"] as? String, sideChat?.id == id, let entryID = event["id"] as? String,
                  let index = sideChat?.entries.firstIndex(where: { $0.id == entryID }) else { return }
            sideChat?.entries[index].text += event["text"] as? String ?? ""
        case "side_accepted":
            guard let request = event["request"] as? String, request == sideRequests[chat],
                  event["side"] as? String == sideChat?.id else { return }
            if let pending = pendingSideText, sideDraft == pending { sideDraft = "" }
            pendingSideText = nil
        case "side_error", "side_rejected":
            guard sideOpening || (event["side"] as? String) == sideChat?.id else { return }
            sideNotice = event["message"] as? String ?? "Side Chat could not complete the request."
            sideOpening = false; pendingSideText = nil
        default: break
        }
    }
}
