import AppKit
import SwiftUI

enum ChatInspectorTab: String, CaseIterable, Identifiable {
    case changes, agents, side, processes
    var id: String { rawValue }
    var title: String { switch self { case .changes: "File Changes"; case .agents: "Team"; case .side: "Side Chat"; case .processes: "Background Processes" } }
    var label: String { switch self { case .changes: "Changes"; case .agents: "Team"; case .side: "Side Chat"; case .processes: "Processes" } }
    var icon: String { switch self { case .changes: "filemenu.and.selection"; case .agents: "person.2"; case .side: "bubble.left.and.bubble.right"; case .processes: "terminal" } }
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
/// A shell command an agent left running in the background. The worker owns
/// the process; this is its latest published snapshot.
struct ChatProcess: Decodable, Identifiable, Equatable {
    var id: String; var pid: Int; var command: String; var workspace: String; var owner: String; var route: String
    var account: String?; var memberID: String?
    var status: String; var code: Int?; var started: Double; var ended: Double?
    var output: String; var truncated: Bool; var stoppedBy: String?
    private enum CodingKeys: String, CodingKey {
        case id, pid, command, workspace, owner, route, account, memberID, status, code, started, ended, output, truncated, stoppedBy
    }
    init(id: String, pid: Int = 0, command: String, workspace: String = "", owner: String = "", route: String = "", account: String? = nil,
         memberID: String? = nil, status: String, code: Int? = nil, started: Double = 0, ended: Double? = nil,
         output: String = "", truncated: Bool = false, stoppedBy: String? = nil) {
        self.id = id; self.pid = pid; self.command = command; self.workspace = workspace; self.owner = owner; self.route = route
        self.account = account; self.memberID = memberID; self.status = status; self.code = code; self.started = started
        self.ended = ended; self.output = output; self.truncated = truncated; self.stoppedBy = stoppedBy
    }
    init(from decoder: Decoder) throws {
        let row = try decoder.container(keyedBy: CodingKeys.self)
        id = try row.decode(String.self, forKey: .id); command = try row.decode(String.self, forKey: .command)
        status = try row.decode(String.self, forKey: .status)
        pid = try row.decodeIfPresent(Int.self, forKey: .pid) ?? 0
        workspace = try row.decodeIfPresent(String.self, forKey: .workspace) ?? ""
        owner = try row.decodeIfPresent(String.self, forKey: .owner) ?? ""
        route = try row.decodeIfPresent(String.self, forKey: .route) ?? ""
        account = try? row.decodeIfPresent(String.self, forKey: .account)
        memberID = try? row.decodeIfPresent(String.self, forKey: .memberID)
        code = try? row.decodeIfPresent(Int.self, forKey: .code)
        started = try row.decodeIfPresent(Double.self, forKey: .started) ?? 0
        ended = try? row.decodeIfPresent(Double.self, forKey: .ended)
        output = try row.decodeIfPresent(String.self, forKey: .output) ?? ""
        truncated = try row.decodeIfPresent(Bool.self, forKey: .truncated) ?? false
        stoppedBy = try? row.decodeIfPresent(String.self, forKey: .stoppedBy)
    }
    /// "stopping" is still alive: SIGTERM has been sent but the process has not exited.
    var running: Bool { status == "running" || status == "stopping" }
    var failed: Bool { status == "exited" && (code ?? 0) != 0 }
    func elapsed(now: Date = Date()) -> String {
        let end = ended ?? (running ? now.timeIntervalSince1970 : started)
        let seconds = Int(max(0, min(end - started, Double(Int.max / 2))))
        if seconds < 60 { return "\(seconds)s" }
        if seconds < 3600 { return "\(seconds / 60)m \(seconds % 60)s" }
        return "\(seconds / 3600)h \((seconds / 60) % 60)m"
    }
    func statusText(now: Date = Date()) -> String {
        switch status {
        case "running": "Running · " + elapsed(now: now)
        case "stopping": "Stopping…"
        case "stopped": "Stopped · " + elapsed(now: now)
        // A negative code is the signal that ended it outside Provider Hub.
        case "exited" where (code ?? 0) < 0: "Failed (signal \(-(code ?? 0))) · " + elapsed(now: now)
        case "exited" where failed: "Failed (exit \(code ?? 0)) · " + elapsed(now: now)
        case "exited": "Completed · " + elapsed(now: now)
        default: status.capitalized
        }
    }
}

struct ChatTeamMember: Decodable, Identifiable {
    var id: String; var name: String; var label: String; var choice: String; var route: String
    var account: String; var effort: String; var responsibility: String; var status: String
    var nextStep: String; var contributions: Int; var contributionID: String?; var usage: Int?; var context: Int?
    var configuration: [String: Any] {
        ["id": id, "name": name, "choice": choice, "effort": effort, "responsibility": responsibility]
    }
}
struct ChatTeamSnapshot: Decodable {
    var enabled: Bool; var status: String; var activeMemberID: String?; var members: [ChatTeamMember]
    var activeMemberIDs: [String]?
    var active: ChatTeamMember? { members.first { $0.id == activeMemberID } }
    var needsInput: Bool { members.contains { $0.status == "needs_input" } }
    var canResume: Bool { enabled && !needsInput && ["stopped", "interrupted", "error"].contains(status) && members.contains { $0.status != "done" } }
}

@MainActor extension ChatModel {
    var canConfigureTeam: Bool { connected && selectedID != nil && !busy && !branchBusy && teamRequest == nil }
    func configureTeam(enabled: Bool, members: [[String: Any]]) {
        guard canConfigureTeam, let selectedID = stateChatID else { return }
        guard (1...3).contains(members.count), members.allSatisfy({ member in
            guard let choice = member["choice"] as? String, let name = member["name"] as? String else { return false }
            return !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && models.contains { $0.id == choice && $0.supportsTools }
        }) else { teamNotice = "Choose one to three named members with enabled models."; return }
        let request = UUID().uuidString
        teamRequest = request; teamNotice = ""
        if !inspectorCommand(["command": "configure_team", "id": selectedID, "request": request,
                              "enabled": enabled, "members": members]) {
            teamRequest = nil; teamNotice = "Chat is disconnected."
        }
    }
    func resumeTeam(member: String? = nil) {
        guard canConfigureTeam, team?.canResume == true, let selectedID = stateChatID else { return }
        let request = UUID().uuidString; teamRequest = request; teamNotice = ""
        var command: [String: Any] = ["command": "team_resume", "id": selectedID, "request": request]
        if let member { command["member"] = member }
        if !inspectorCommand(command) { teamRequest = nil; teamNotice = "Chat is disconnected." }
    }
    func isStreaming(_ entry: ChatEntry, fallback: Bool) -> Bool {
        guard let memberID = entry.memberID else { return fallback }
        guard busy, team?.enabled == true,
              let member = team?.members.first(where: { $0.id == memberID && $0.status == "working" }),
              let contribution = member.contributionID, contribution == entry.contributionID else { return false }
        return entries.last(where: { $0.memberID == memberID && $0.contributionID == contribution })?.id == entry.id
    }
    var canChangeBranch: Bool { connected && selectedID != nil && !busy && !branchBusy && sideChat?.busy != true && !sideOpening }
    var canSendSide: Bool {
        connected && sideChat != nil && !branchBusy && !sideOpening && pendingSideText == nil && sideChat?.interrupting != true
            && !sideDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
    func showInspector(_ tab: ChatInspectorTab) {
        inspectorTab = tab; inspectorVisible = true
    }
    func refreshChanges() {
        guard connected, let selectedID = stateChatID else { return }
        if gitChangesLoading { changesRefreshPending = true; return }
        let request = UUID().uuidString; changesRequest = request; changesRefreshPending = false
        gitChangesLoading = true; inspectorNotice = ""
        if !inspectorCommand(["command": "inspect_git", "id": selectedID, "request": request]) { gitChangesLoading = false; changesRequest = nil }
    }
    var runningProcessCount: Int { processes.filter(\.running).count }
    /// The pane's poll leaves a notice in place; the refresh button clears it.
    func refreshProcesses(userInitiated: Bool = false) {
        guard connected, let selectedID = stateChatID else { return }
        if userInitiated { processesNotice = "" }
        inspectorCommand(["command": "processes", "id": selectedID])
    }
    func stopProcess(_ id: String) {
        guard connected, let selectedID = stateChatID, let index = processes.firstIndex(where: { $0.id == id && $0.status == "running" }) else { return }
        processesNotice = ""
        if inspectorCommand(["command": "stop_process", "id": selectedID, "process": id]) { processes[index].status = "stopping" }
        else { processesNotice = "Chat is disconnected." }
    }
    func clearFinishedProcesses() {
        guard connected, let selectedID = stateChatID, processes.contains(where: { !$0.running }) else { return }
        processesNotice = ""
        if !inspectorCommand(["command": "clear_processes", "id": selectedID]) { processesNotice = "Chat is disconnected." }
    }
    func refreshBranches() {
        guard connected, let selectedID = stateChatID, !branchBusy else { return }
        if branchesLoading { branchesRefreshPending = true; return }
        let request = UUID().uuidString; branchesRequest = request; branchesRefreshPending = false
        branchesLoading = true; branchNotice = ""
        if !inspectorCommand(["command": "branches", "id": selectedID, "request": request]) { branchesLoading = false; branchesRequest = nil }
    }
    func branchAction(_ action: String, branch: String? = nil, path: String? = nil, branchBytes: String? = nil) {
        guard canChangeBranch, let selectedID = stateChatID else { return }
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
        guard connected, let selectedID = stateChatID, !branchBusy, !sideOpening, sideChat == nil else { return }
        sideOpening = true; sideNotice = ""
        let request = UUID().uuidString
        sideRequests[selectedID] = request
        if !inspectorCommand(["command": "open_side", "id": selectedID, "choice": choice, "effort": effort, "request": request]) {
            sideOpening = false; sideNotice = "Chat is disconnected."
        }
    }
    func setSideModel(choice: String, effort: String) {
        guard let selectedID = stateChatID, let side = sideChat, !side.busy, !branchBusy, pendingSideText == nil else { return }
        inspectorCommand(["command": "side_model", "id": selectedID, "side": side.id, "choice": choice, "effort": effort])
    }
    func sendSide() {
        guard canSendSide, let selectedID = stateChatID, let side = sideChat else { return }
        pendingSideText = sideDraft; sideNotice = ""
        if !inspectorCommand(["command": "side_send", "id": selectedID, "side": side.id, "text": sideDraft]) {
            pendingSideText = nil; sideNotice = "Chat is disconnected."
        }
    }
    func stopSide() {
        guard let selectedID = stateChatID, let side = sideChat, side.busy else { return }
        inspectorCommand(["command": "side_stop", "id": selectedID, "side": side.id])
    }
    func closeSideChat() {
        if let selectedID = stateChatID {
            var command: [String: Any] = ["command": "close_side", "id": selectedID]
            if let side = sideChat { command["side"] = side.id }
            if let request = sideRequests[selectedID] { command["request"] = request }
            inspectorCommand(command)
            sideRequests[selectedID] = nil
        }
        sideChat = nil; sideDraft = ""; sideNotice = "Temporary conversation discarded."; sideOpening = false; pendingSideText = nil
    }
    func resetInspector(clearSessions: Bool = false) {
        if clearSessions, let stateChatID { sideRequests[stateChatID] = nil }
        gitChanges = nil; gitChangesLoading = false; branches = nil; branchesLoading = false; branchBusy = false
        changesRequest = nil; changesRefreshPending = false; branchesRequest = nil; branchesRefreshPending = false; branchRequest = nil
        inspectorNotice = ""; branchNotice = ""; agents = []; inspectedAgentID = nil
        team = nil; teamNotice = ""; teamRequest = nil
        sideChat = nil; sideNotice = ""; sideOpening = false; pendingSideText = nil
        processes = []; processesNotice = ""
    }
    func consumeInspector(_ event: [String: Any]) {
        guard let chat = event["chat"] as? String, chat == stateChatID else { return }
        func decoded<T: Decodable>(_ value: Any?, as type: T.Type) -> T? {
            guard let value, JSONSerialization.isValidJSONObject(value), let data = try? JSONSerialization.data(withJSONObject: value) else { return nil }
            return try? JSONDecoder().decode(type, from: data)
        }
        switch event["event"] as? String {
        case "team":
            if let request = event["request"] as? String {
                guard request == teamRequest else { return }
                teamRequest = nil
            }
            if let value = event["team"], !(value is NSNull) {
                guard let snapshot = decoded(value, as: ChatTeamSnapshot.self),
                      (1...3).contains(snapshot.members.count), Set(snapshot.members.map(\.id)).count == snapshot.members.count else { return }
                team = snapshot
            } else { team = nil }
            teamNotice = event["notice"] as? String ?? ""
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
            guard chat == selectedID, let id = event["agent"] as? String, let index = agents.firstIndex(where: { $0.id == id }),
                  let entry = decoded(event["entry"], as: ChatEntry.self) else { return }
            if let row = agents[index].entries.firstIndex(where: { $0.id == entry.id }) { agents[index].entries[row] = entry }
            else { agents[index].entries.append(entry) }
        case "agent_delta":
            guard chat == selectedID, let id = event["agent"] as? String, let index = agents.firstIndex(where: { $0.id == id }),
                  let entryID = event["id"] as? String, let row = agents[index].entries.firstIndex(where: { $0.id == entryID }) else { return }
            agents[index].entries[row].text = streamedText(agents[index].entries[row].text, event: event, baseOffset: agents[index].entries[row].textOffset ?? 0)
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
            guard chat == selectedID, let request = event["request"] as? String, request == sideRequests[chat] else { return }
            guard let id = event["side"] as? String, sideChat?.id == id, let entryID = event["id"] as? String,
                  let index = sideChat?.entries.firstIndex(where: { $0.id == entryID }) else { return }
            if let text = sideChat?.entries[index].text { sideChat?.entries[index].text = streamedText(text, event: event, baseOffset: sideChat?.entries[index].textOffset ?? 0) }
        case "side_accepted":
            guard let request = event["request"] as? String, request == sideRequests[chat],
                  event["side"] as? String == sideChat?.id else { return }
            if let pending = pendingSideText, sideDraft == pending { sideDraft = "" }
            pendingSideText = nil
        case "side_error", "side_rejected":
            guard sideOpening || (event["side"] as? String) == sideChat?.id else { return }
            sideNotice = event["message"] as? String ?? "Side Chat could not complete the request."
            sideOpening = false; pendingSideText = nil
        case "processes":
            // Decode row by row so one malformed process cannot hide the rest.
            let rows = (event["processes"] as? [Any] ?? []).compactMap { decoded($0, as: ChatProcess.self) }
            // An unchanged poll must not republish the whole Chat window.
            if rows != processes { processes = rows }
            // A snapshot without a notice keeps the last one, so a poll cannot
            // wipe a Stop result before it is read. Actions in the tab clear it.
            if let notice = event["notice"] as? String { processesNotice = notice }
        default: break
        }
    }
}
