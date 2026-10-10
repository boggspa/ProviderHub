import AppKit
import SwiftUI
import PDFKit

struct ChatSummary: Decodable, Identifiable {
    var id: String
    var title: String
    var updated: String
    var route: String
    var account: String
    var workspace: String
    var effort: String
    var approvalMode: String?
    var scope: String?
}

struct ChatRoute: Decodable, Identifiable {
    var id: String
    var route: String
    var label: String
    var provider: String
    var account: String
    var accountLabel: String
    var scope: String
    var efforts: [String]
    var context: Int?
    var supportsTools: Bool
    var supportsWebSearch: Bool?
    var presentation: ProviderPresentation?
    var connectionPresentation: ProviderPresentation?
    var accent: Color { presentation?.color ?? .secondary }
    var connectionID: String { connectionPresentation?.runtimeProvider ?? String(route.split(separator: "/").first ?? "") }
    var connectionLabel: String { connectionPresentation?.displayProvider ?? connectionID.capitalized }
    var connectionAccent: Color { connectionPresentation?.color ?? accent }
}

struct ChatEntry: Decodable, Identifiable {
    var id: String
    var kind: String
    var text: String
    var route: String
    var tool: String?
    var summary: String?
    var detail: String?
    var isError: Bool
    var changedFiles: [String]
    var attachments: [ChatAttachment]?
    var agentID: String?
    var agentIDs: [String]?
    var workspace: String?
    var memberID: String?
    var memberName: String?
    var contributionID: String?
    var clientRequest: String?
    var textOffset: Int?
    /// `@Name` tags as the worker resolved and routed them, in UTF-16 units.
    var mentions: [ChatMention]?
}

struct ChatAttachment: Decodable, Identifiable {
    var id: String
    var name: String
    var path: String
    var kind: String
    var size: Int
    var extractedText: String?
    var request: [String: Any] {
        var value: [String: Any] = ["path": path]
        if let extractedText { value["extractedText"] = extractedText }
        return value
    }
}

struct ChatApproval: Decodable, Identifiable {
    var id: String
    var summary: String
    var workspace: String
    var detail: String?
    var chat: String?
}
struct ChatGitStatus: Decodable {
    var files: Int
    var added: Int
    var deleted: Int
    var ahead: Int
    var behind: Int?
    var upstream: String?
    var branch: String
}

/// One local worker owns Chat's transcripts and execution loops. Its transport
/// contains no provider credentials; the existing authenticated gateway owns them.
private struct ChatSessionState {
    var entries: [ChatEntry] = []
    var draft = ""
    var attachments: [ChatAttachment] = []
    var busy = false
    var interrupting = false
    var turnStartedAt: TimeInterval?
    var notice = ""
    var approval: ChatApproval?
    var status = "Ready"
    var tokenUsage: Int?
    var contextLimit: Int?
    var gitStatus: ChatGitStatus?
    var gitChanges: ChatChanges?
    var gitChangesLoading = false
    var changesRequest: String?
    var changesRefreshPending = false
    var branches: ChatBranchesSnapshot?
    var branchesLoading = false
    var branchesRequest: String?
    var branchesRefreshPending = false
    var branchRequest: String?
    var branchBusy = false
    var inspectorNotice = ""
    var branchNotice = ""
    var agents: [ChatAgent] = []
    var team: ChatTeamSnapshot?
    var teamNotice = ""
    var teamRequest: String?
    var inspectedAgentID: String?
    var sideChat: ChatSide?
    var sideDraft = ""
    var sideNotice = ""
    var sideOpening = false
    var pendingSideText: String?
    // Background processes deliberately stay out of `active`: a dev server
    // must not block quitting or updates, and the worker stops them at quit.
    var processes: [ChatProcess] = []
    var processesNotice = ""
    var pendingSend: (chat: String, request: String, text: String, attachments: [ChatAttachment])?
    var active: Bool {
        busy || approval != nil || branchBusy || teamRequest != nil || sideOpening || sideChat?.busy == true || agents.contains(where: \.busy)
    }
    var unsent: Bool {
        !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !attachments.isEmpty ||
        !sideDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || pendingSend != nil || pendingSideText != nil
    }
}

@MainActor
final class ChatModel: ObservableObject {
    @Published var chats: [ChatSummary] = []
    @Published var selectedID: String?
    @Published var models: [ChatRoute] = []
    @Published var connected = false
    @Published private(set) var webSearchEnabled: Bool
    @Published var recentFolders: [String] = []
    @Published var inspectorVisible = false
    @Published var inspectorTab: ChatInspectorTab = .changes
    // Only the selected session publishes transcript changes. Background chats
    // retain control state; the worker rehydrates their transcripts on selection.
    private var sessions: [String: ChatSessionState] = [:]
    private var unselected = ChatSessionState()
    private var routedChatID: String?
    var stateChatID: String? { routedChatID ?? selectedID }
    private func value<T>(_ key: KeyPath<ChatSessionState, T>) -> T {
        let state = stateChatID.map { sessions[$0] ?? ChatSessionState() } ?? unselected
        return state[keyPath: key]
    }
    private func update<T>(_ key: WritableKeyPath<ChatSessionState, T>, _ value: T, background: Bool = false) {
        let wasActive = hasActiveWork
        let previous = stateChatID.map { sessions[$0] ?? ChatSessionState() } ?? unselected
        var state = previous; state[keyPath: key] = value
        let rowChanged = previous.busy != state.busy || previous.active != state.active || previous.approval?.id != state.approval?.id
        if stateChatID == selectedID || (background && rowChanged) { objectWillChange.send() }
        if let id = stateChatID {
            sessions[id] = state
        } else { unselected = state }
        if hasActiveWork != wasActive { onActivity?(hasActiveWork) }
    }
    var entries: [ChatEntry] { get { value(\.entries) } set { update(\.entries, newValue) } }
    var draft: String { get { value(\.draft) } set { update(\.draft, newValue) } }
    var attachments: [ChatAttachment] { get { value(\.attachments) } set { update(\.attachments, newValue) } }
    var busy: Bool { get { value(\.busy) } set { update(\.busy, newValue, background: true) } }
    var interrupting: Bool { get { value(\.interrupting) } set { update(\.interrupting, newValue) } }
    private(set) var turnStartedAt: TimeInterval? { get { value(\.turnStartedAt) } set { update(\.turnStartedAt, newValue) } }
    var notice: String { get { value(\.notice) } set { update(\.notice, newValue) } }
    var approval: ChatApproval? { get { value(\.approval) } set { update(\.approval, newValue, background: true) } }
    var status: String { get { value(\.status) } set { update(\.status, newValue) } }
    var tokenUsage: Int? { get { value(\.tokenUsage) } set { update(\.tokenUsage, newValue) } }
    var contextLimit: Int? { get { value(\.contextLimit) } set { update(\.contextLimit, newValue) } }
    var gitStatus: ChatGitStatus? { get { value(\.gitStatus) } set { update(\.gitStatus, newValue) } }
    var gitChanges: ChatChanges? { get { value(\.gitChanges) } set { update(\.gitChanges, newValue) } }
    var gitChangesLoading: Bool { get { value(\.gitChangesLoading) } set { update(\.gitChangesLoading, newValue) } }
    var changesRequest: String? { get { value(\.changesRequest) } set { update(\.changesRequest, newValue) } }
    var changesRefreshPending: Bool { get { value(\.changesRefreshPending) } set { update(\.changesRefreshPending, newValue) } }
    var branches: ChatBranchesSnapshot? { get { value(\.branches) } set { update(\.branches, newValue) } }
    var branchesLoading: Bool { get { value(\.branchesLoading) } set { update(\.branchesLoading, newValue) } }
    var branchesRequest: String? { get { value(\.branchesRequest) } set { update(\.branchesRequest, newValue) } }
    var branchesRefreshPending: Bool { get { value(\.branchesRefreshPending) } set { update(\.branchesRefreshPending, newValue) } }
    var branchRequest: String? { get { value(\.branchRequest) } set { update(\.branchRequest, newValue) } }
    var branchBusy: Bool { get { value(\.branchBusy) } set { update(\.branchBusy, newValue, background: true) } }
    var inspectorNotice: String { get { value(\.inspectorNotice) } set { update(\.inspectorNotice, newValue) } }
    var branchNotice: String { get { value(\.branchNotice) } set { update(\.branchNotice, newValue) } }
    var agents: [ChatAgent] { get { value(\.agents) } set { update(\.agents, newValue, background: true) } }
    var team: ChatTeamSnapshot? { get { value(\.team) } set { update(\.team, newValue) } }
    var teamNotice: String { get { value(\.teamNotice) } set { update(\.teamNotice, newValue) } }
    var teamRequest: String? { get { value(\.teamRequest) } set { update(\.teamRequest, newValue, background: true) } }
    var inspectedAgentID: String? { get { value(\.inspectedAgentID) } set { update(\.inspectedAgentID, newValue) } }
    var sideChat: ChatSide? { get { value(\.sideChat) } set { update(\.sideChat, newValue, background: true) } }
    var sideDraft: String { get { value(\.sideDraft) } set { update(\.sideDraft, newValue) } }
    var sideNotice: String { get { value(\.sideNotice) } set { update(\.sideNotice, newValue) } }
    var sideOpening: Bool { get { value(\.sideOpening) } set { update(\.sideOpening, newValue, background: true) } }
    var pendingSideText: String? { get { value(\.pendingSideText) } set { update(\.pendingSideText, newValue) } }
    var processes: [ChatProcess] { get { value(\.processes) } set { update(\.processes, newValue, background: true) } }
    var processesNotice: String { get { value(\.processesNotice) } set { update(\.processesNotice, newValue) } }
    private var pendingSend: (chat: String, request: String, text: String, attachments: [ChatAttachment])? { get { value(\.pendingSend) } set { update(\.pendingSend, newValue) } }
    var sideRequests: [String: String] = [:]
    var onActivity: ((Bool) -> Void)?
    var onSurfaceChange: (() -> Void)?
    private weak var bridge: BridgeModel?
    private var process: Process?
    private var input: Pipe?
    private var output: Pipe?
    private var buffer = Data()
    /// The worker line being consumed, for `payload`.
    private(set) var eventLine = Data()
    private var commandSink: (([String: Any]) -> Bool)?
    private var starting = false
    private let preferences: UserDefaults
    private var uptime: () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }

    init(bridge: BridgeModel) {
        self.bridge = bridge
        preferences = .standard
        webSearchEnabled = preferences.object(forKey: "chatAllowWebSearch") as? Bool ?? true
    }
    /// A local transport seam for exercising real UI state transitions without
    /// a gateway process or a provider account.
    init(sendCommand: @escaping ([String: Any]) -> Bool, uptime: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }, preferences: UserDefaults = .standard) {
        commandSink = sendCommand; self.uptime = uptime
        self.preferences = preferences
        webSearchEnabled = preferences.object(forKey: "chatAllowWebSearch") as? Bool ?? true
    }
    var turnElapsed: TimeInterval { turnStartedAt.map { max(0, uptime() - $0) } ?? 0 }
    var turnTimecode: String {
        let seconds = Int(min(turnElapsed, Double(Int.max / 2)))
        return seconds < 3600 ? String(format: "%02d:%02d", seconds / 60, seconds % 60)
            : String(format: "%02d:%02d:%02d", seconds / 3600, (seconds / 60) % 60, seconds % 60)
    }
    var selected: ChatSummary? { chats.first { $0.id == stateChatID } }
    var selectedRoute: ChatRoute? { models.first { $0.route == selected?.route && $0.account == selected?.account } }
    var activeAccent: Color { selectedRoute?.accent ?? .secondary }
    var hasActiveWork: Bool { unselected.active || sessions.values.contains(where: \.active) }
    var hasUnsentDrafts: Bool { unselected.unsent || sessions.values.contains(where: \.unsent) }
    func isBusy(_ chatID: String) -> Bool { sessions[chatID]?.busy == true }
    func needsApproval(_ chatID: String) -> Bool { sessions[chatID]?.approval != nil }
    func isActive(_ chatID: String) -> Bool { sessions[chatID]?.active == true }
    var approvalMode: String { selected?.approvalMode ?? "manual" }
    var canSend: Bool { connected && !busy && !branchBusy && teamRequest == nil && selectedRoute != nil && (!draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !attachments.isEmpty) }
    var canInterrupt: Bool { connected && busy && !interrupting && pendingSend == nil && selectedRoute != nil && (!draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !attachments.isEmpty) }
    func accent(for route: String) -> Color { models.first { $0.route == route }?.accent ?? .secondary }

    func start() async {
        guard !starting else { return }
        starting = true; defer { starting = false }
        guard let bridge, let python = bridge.python else { notice = "A supported Python runtime is needed."; return }
        do {
            do { try await bridge.startGateway() }
            catch { notice = "Saved chats remain available. " + error.localizedDescription }
            if process?.isRunning == true { refresh(); return }
            let child = Process(), stdin = Pipe(), stdout = Pipe(), stderr = Pipe()
            child.executableURL = URL(fileURLWithPath: python)
            child.arguments = [bridge.helper.deletingLastPathComponent().appendingPathComponent("chat_runtime.py").path]
            child.environment = bridge.workerEnvironment
            child.standardInput = stdin; child.standardOutput = stdout; child.standardError = stderr
            stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let data = handle.availableData
                if data.isEmpty { handle.readabilityHandler = nil; return }
                guard let owner = self else { return }
                Task { @MainActor in owner.consume(data) }
            }
            stderr.fileHandleForReading.readabilityHandler = { handle in
                if handle.availableData.isEmpty { handle.readabilityHandler = nil }
            }
            child.terminationHandler = { [weak self] _ in
                guard let owner = self else { return }
                Task { @MainActor in
                    guard owner.process === child else { return }
                    owner.connected = false; owner.process = nil; owner.input = nil
                    owner.output?.fileHandleForReading.readabilityHandler = nil; owner.output = nil
                    owner.disconnected("Chat disconnected. Reopen Chat to reconnect; saved work is kept.")
                }
            }
            buffer = Data(); input = stdin; output = stdout; process = child
            try child.run()
        } catch { notice = error.localizedDescription; connected = false; process = nil; input = nil }
    }

    func shutdown() {
        write(["command": "shutdown"])
        try? input?.fileHandleForWriting.close()
        input = nil
    }
    func refresh() { write(["command": "refresh"]) }
    func setWebSearchEnabled(_ enabled: Bool) {
        webSearchEnabled = enabled
        preferences.set(enabled, forKey: "chatAllowWebSearch")
        if connected { write(["command": "preferences", "webSearch": enabled]) }
    }
    func refreshGitStatus() {
        if connected, let stateChatID { write(["command": "git_status", "id": stateChatID, "chat": stateChatID]) }
    }
    func newChat() {
        newChat(in: selected?.workspace ?? recentFolders.first ?? NSHomeDirectory())
    }
    func newChat(in workspace: String) {
        let choice = selectedRoute ?? models.first
        guard let choice else { notice = "Configure a provider and refresh its models in Provider Hub."; return }
        write(["command": "create", "choice": choice.id, "workspace": workspace])
    }
    func select(_ id: String) {
        write(["command": "select", "id": id, "chat": id])
    }
    func send() {
        guard canSend || canInterrupt, let selectedID else { return }
        let command = busy ? "steer" : "send"
        let text = draft; draft = ""; notice = ""
        let files = attachments; attachments = []
        let request = UUID().uuidString
        pendingSend = (selectedID, request, text, files)
        setBusy(true); interrupting = command == "steer"
        status = interrupting ? "Interrupting for your update…" : "Connecting…"
        // Every chip the composer drew is the routing record; the worker
        // refuses chips drawn from a roster it no longer has.
        let mentions = ChatMentions.resolve(text, members: mentionTargets).map(\.wire)
        if !write(["command": command, "id": selectedID, "chat": selectedID, "request": request, "text": text,
                   "attachments": files.map(\.request), "mentions": mentions]) { restorePendingSend(); setBusy(false); interrupting = false }
    }
    func stop() {
        guard busy, let stateChatID else { return }
        if write(["command": "stop", "id": stateChatID, "chat": stateChatID]) { interrupting = true; status = "Stopping…" }
    }
    func retry() {
        guard !busy, !branchBusy, let selectedID else { return }
        notice = ""; setBusy(true)
        if !write(["command": "retry", "id": selectedID, "chat": selectedID]) { setBusy(false) }
    }
    func setRoute(_ choiceID: String) {
        guard !busy, teamRequest == nil, team?.enabled != true, let choice = models.first(where: { $0.id == choiceID }) else { return }
        configure(["choice": choice.id])
    }
    func setSelection(_ choiceID: String, effort: String) {
        guard !busy, teamRequest == nil, team?.enabled != true, let choice = models.first(where: { $0.id == choiceID }) else { return }
        configure(["choice": choice.id, "effort": effort])
    }
    private func configure(_ fields: [String: Any]) {
        var command = fields; command["command"] = "configure"
        if let stateChatID { command["id"] = stateChatID; command["chat"] = stateChatID }
        write(command)
    }
    func setEffort(_ effort: String) { guard !busy, teamRequest == nil, team?.enabled != true else { return }; configure(["effort": effort]) }
    func setApprovalMode(_ mode: String, chat: String? = nil, workspace: String? = nil) {
        guard let owner = chat ?? stateChatID else { return }
        withChat(owner) {
            guard workspace == nil || selected?.workspace == workspace else {
                notice = "The chat's workspace changed. Choose its approval mode again."
                return
            }
            if !busy {
                var fields: [String: Any] = ["approvalMode": mode]
                if let workspace { fields["expectedWorkspace"] = workspace }
                configure(fields)
            }
        }
    }
    func setFolder(_ path: String) { guard !busy, !branchBusy else { return }; configure(["workspace": path]) }
    func chooseFolder() {
        let owner = selectedID
        let picker = NSOpenPanel(); picker.canChooseDirectories = true; picker.canChooseFiles = false
        picker.allowsMultipleSelection = false; picker.prompt = "Use folder"
        if let path = selected?.workspace { picker.directoryURL = URL(fileURLWithPath: path) }
        if picker.runModal() == .OK, let url = picker.url {
            if let owner { withChat(owner) { setFolder(url.path) } }
            else if selectedID == nil { setFolder(url.path) }
        }
    }
    func decideApproval(allow: Bool, target: ChatApproval? = nil) {
        guard let request = target ?? approval, let owner = request.chat ?? stateChatID else { return }
        withChat(owner) {
            guard approval?.id == request.id else { return }
            if write(["command": "approve", "id": request.id, "chat": owner, "allow": allow]) {
                approval = nil; status = allow ? "Working…" : "Denied"
            }
        }
    }
    func chooseAttachments() {
        let owner = selectedID
        let picker = NSOpenPanel(); picker.canChooseFiles = true; picker.canChooseDirectories = false
        picker.allowsMultipleSelection = true; picker.prompt = "Attach"
        if picker.runModal() == .OK, let owner { withChat(owner) { addAttachments(picker.urls) } }
    }
    func addAttachments(_ urls: [URL]) {
        guard stateChatID != nil else { return }
        for url in urls {
            guard attachments.count < 8 else { notice = "Attach up to eight files per message."; return }
            if attachments.contains(where: { $0.path == url.path }) { continue }
            do {
                let values = try url.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey])
                guard values.isRegularFile == true, let size = values.fileSize, size <= 8 * 1024 * 1024 else {
                    notice = "Choose a regular file of up to 8 MiB."; continue
                }
                let ext = url.pathExtension.lowercased()
                let image = ["png", "jpg", "jpeg", "gif", "webp"].contains(ext)
                var text: String?
                if ext == "pdf" {
                    guard let document = PDFDocument(url: url), let content = document.string, !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                        notice = "This PDF has no extractable text. Attach images of its pages instead."; continue
                    }
                    guard content.count <= 200_000 else { notice = "Choose a PDF with less than 200,000 characters of text."; continue }
                    text = content
                }
                attachments.append(ChatAttachment(id: UUID().uuidString, name: url.lastPathComponent,
                    path: url.path, kind: image ? "image" : "file", size: size, extractedText: text))
                notice = ""
            } catch { notice = "Could not attach \(url.lastPathComponent): \(error.localizedDescription)" }
        }
    }
    func removeAttachment(_ id: String) { attachments.removeAll { $0.id == id } }
    func rename(_ id: String, title: String) { write(["command": "rename", "id": id, "chat": id, "title": title]) }
    func delete(_ id: String) { guard !isActive(id) else { return }; write(["command": "delete", "id": id, "chat": id]) }

    private func setBusy(_ value: Bool) {
        // A steer cancels/restarts the provider request while the logical turn
        // remains busy. Only the idle→working edge starts a new clock.
        if value && !busy { turnStartedAt = uptime() }
        if !value { turnStartedAt = nil; interrupting = false }
        busy = value
    }
    private func restorePendingSend() {
        guard let pendingSend else { return }
        draft = draft.isEmpty ? pendingSend.text : pendingSend.text + "\n\n" + draft
        attachments = pendingSend.attachments + attachments
        self.pendingSend = nil
    }
    private func withChat(_ id: String, _ operation: () -> Void) {
        let previous = routedChatID; routedChatID = id
        defer { routedChatID = previous }
        operation()
    }
    /// A transport failure settles every owner, including unacknowledged input
    /// in hidden chats. Accepted input has already cleared its pending record.
    func disconnected(_ message: String) {
        connected = false
        for id in Array(sessions.keys) {
            withChat(id) {
                let orphaned = runningProcessCount > 0
                restorePendingSend(); setBusy(false); approval = nil
                resetInspector(clearSessions: true); notice = message
                // Only an orderly worker shutdown is known to end them.
                if orphaned { processesNotice = "Chat disconnected while background processes were running. They may still be running." }
            }
        }
        unselected.notice = message; objectWillChange.send()
    }
    @discardableResult private func write(_ message: [String: Any]) -> Bool {
        if let commandSink { return commandSink(message) }
        guard let input, let data = try? JSONSerialization.data(withJSONObject: message) else { return false }
        do { try input.fileHandleForWriting.write(contentsOf: data + Data([10])); return true }
        catch { disconnected("Chat disconnected. Your saved transcript is kept."); return false }
    }
    private func decode<T: Decodable>(_ type: T.Type, _ raw: Any?) -> T? {
        guard let raw, JSONSerialization.isValidJSONObject(raw), let data = try? JSONSerialization.data(withJSONObject: raw) else { return nil }
        return try? JSONDecoder().decode(type, from: data)
    }
    /// `key` of the event being consumed, decoded from the worker's own bytes.
    /// JSONSerialization drops a string's leading U+FEFF, which would rename a
    /// Team member, so every chip drawn for it was refused, or move a recorded
    /// tag off its text. JSONDecoder keeps every character, so the payloads the
    /// `@Name` contract compares exactly (the roster and transcript) use this.
    func payload<T: Decodable>(_ type: T.Type, _ key: String) throws -> T? {
        let decoder = JSONDecoder()
        decoder.userInfo[ChatEventKey.field] = key
        return try decoder.decode(ChatEventField<T>.self, from: eventLine).value
    }
    private func decodeApproval(_ raw: Any?) -> ChatApproval? {
        guard var approval = decode(ChatApproval.self, raw) else { return nil }
        approval.chat = stateChatID
        return approval
    }
    func consume(_ data: Data) {
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 10) {
            let line = buffer.prefix(upTo: newline); buffer.removeSubrange(...newline)
            guard let event = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
            eventLine = line
            switch event["event"] as? String {
            case "ready":
                connected = true
                write(["command": "preferences", "webSearch": webSearchEnabled])
            case "catalogue":
                models = decode([ChatRoute].self, event["models"]) ?? []; recentFolders = event["folders"] as? [String] ?? []
                contextLimit = selectedRoute?.context; notice = ""
            case "chats":
                let previous = Dictionary(uniqueKeysWithValues: chats.map { ($0.id, $0.workspace) })
                chats = decode([ChatSummary].self, event["chats"]) ?? []
                for chat in chats where previous[chat.id] != nil && previous[chat.id] != chat.workspace {
                    withChat(chat.id) {
                        gitStatus = nil; gitChanges = nil; gitChangesLoading = false; branches = nil; branchesLoading = false
                        changesRequest = nil; changesRefreshPending = false; branchesRefreshPending = false
                        if !branchBusy { branchesRequest = nil }
                    }
                }
                let remaining = Set(chats.map(\.id))
                for id in previous.keys where !remaining.contains(id) && !isActive(id) { sessions[id] = nil; sideRequests[id] = nil }
            case "selected":
                let id = event["id"] as? String
                if id != selectedID {
                    // Keep control state and drafts; only the visible transcript
                    // needs a Swift cache. Selection snapshots rebuild it.
                    if let selectedID { sessions[selectedID]?.entries = [] }
                }
                selectedID = id; entries = (try? payload([ChatEntry].self, "entries")) ?? []
                tokenUsage = event["usage"] as? Int; contextLimit = selectedRoute?.context
                if let current = event["busy"] as? Bool { setBusy(current) }
                if let current = event["interrupting"] as? Bool { interrupting = current }
                if let current = event["status"] as? String { status = current }
                if event.keys.contains("approval") { approval = decodeApproval(event["approval"]) }
                if !busy || interrupting { approval = nil }
                if let pendingSend, entries.contains(where: { $0.kind == "user" && $0.clientRequest == pendingSend.request }) { self.pendingSend = nil }
            default:
                if let chat = event["chat"] as? String { withChat(chat) { consumeChatEvent(event) } }
                else if ["error", "notice", "rejected"].contains(event["event"] as? String ?? "") {
                    // Transport/catalogue errors have no execution owner. They
                    // must not stop or restore another chat's submitted input.
                    notice = event["message"] as? String ?? "Chat failed."
                }
            }
        }
    }

    private func consumeChatEvent(_ event: [String: Any]) {
        switch event["event"] as? String {
        case "entry":
            guard let entry = try? payload(ChatEntry.self, "entry") else { return }
            if entry.kind == "user", let pendingSend, entry.clientRequest == pendingSend.request { self.pendingSend = nil }
            guard stateChatID == selectedID else { return }
            if let index = entries.firstIndex(where: { $0.id == entry.id }) { entries[index] = entry } else { entries.append(entry) }
        case "delta":
            guard stateChatID == selectedID, let id = event["id"] as? String,
                  let index = entries.firstIndex(where: { $0.id == id }) else { return }
            entries[index].text = streamedText(entries[index].text, event: event, baseOffset: entries[index].textOffset ?? 0)
        case "approval": approval = decodeApproval(event["approval"]); status = "Needs approval"
        case "state":
            setBusy(event["busy"] as? Bool ?? false); status = event["status"] as? String ?? "Ready"
            interrupting = event["interrupting"] as? Bool ?? (busy && interrupting)
            tokenUsage = event["usage"] as? Int ?? tokenUsage
            if event.keys.contains("approval") { approval = decodeApproval(event["approval"]) }
            if !busy || (event["interrupting"] as? Bool == true) { approval = nil }
        case "error": restorePendingSend(); notice = event["message"] as? String ?? "Chat failed."; setBusy(false); approval = nil
        case "rejected":
            restorePendingSend(); notice = event["message"] as? String ?? "The update was not accepted."
            setBusy(event["busy"] as? Bool ?? false); interrupting = event["interrupting"] as? Bool ?? false
        case "notice": notice = event["message"] as? String ?? ""
        case "git_status":
            if (event["workspace"] as? String).map({ $0 == selected?.workspace }) ?? true { gitStatus = decode(ChatGitStatus.self, event["status"]) }
        default: consumeInspector(event)
        }
    }

    /// Offset is the start of the chunk in Python Unicode code points. A
    /// selection snapshot can already contain some or all of a queued chunk.
    func streamedText(_ current: String, event: [String: Any], baseOffset: Int = 0) -> String {
        let delta = event["text"] as? String ?? ""
        guard let offset = event["offset"] as? Int else { return current + delta }
        guard offset >= 0 else { return current }
        let overlap = current.unicodeScalars.count + baseOffset - offset
        guard overlap >= 0 else {
            // Request a fresh snapshot if a stream chunk arrives across a gap.
            if let selectedID, stateChatID == selectedID { select(selectedID) }
            return current
        }
        return current + String(String.UnicodeScalarView(delta.unicodeScalars.dropFirst(overlap)))
    }

    @discardableResult func inspectorCommand(_ command: [String: Any]) -> Bool {
        var command = command
        if let owner = command["id"] as? String ?? stateChatID { command["chat"] = owner }
        return write(command)
    }
}

/// One top-level field of a worker event, decoded on its own (`ChatModel.payload`).
private struct ChatEventField<T: Decodable>: Decodable {
    var value: T?
    init(from decoder: Decoder) throws {
        let key = ChatEventKey(stringValue: decoder.userInfo[ChatEventKey.field] as? String ?? "")
        value = try decoder.container(keyedBy: ChatEventKey.self).decodeIfPresent(T.self, forKey: key)
    }
}

private struct ChatEventKey: CodingKey {
    static let field = CodingUserInfoKey(rawValue: "ProviderHub.eventField")!
    var stringValue: String
    var intValue: Int? { nil }
    init(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { nil }
}
