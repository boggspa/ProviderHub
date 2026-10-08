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
}

/// One local worker owns Chat's transcript and execution loop. Its transport
/// contains no provider credentials; the existing authenticated gateway owns them.
@MainActor
final class ChatModel: ObservableObject {
    @Published var chats: [ChatSummary] = []
    @Published var selectedID: String?
    @Published var entries: [ChatEntry] = []
    @Published var models: [ChatRoute] = []
    @Published var draft = ""
    @Published var attachments: [ChatAttachment] = []
    @Published var busy = false
    @Published var notice = ""
    @Published var connected = false
    @Published var approval: ChatApproval?
    @Published var status = "Ready"
    @Published var recentFolders: [String] = []
    @Published var tokenUsage: Int?
    @Published var contextLimit: Int?
    var onActivity: ((Bool) -> Void)?
    private weak var bridge: BridgeModel?
    private var process: Process?
    private var input: Pipe?
    private var output: Pipe?
    private var buffer = Data()
    private var drafts: [String: String] = [:]
    private var attachmentDrafts: [String: [ChatAttachment]] = [:]
    private var pendingSend: (chat: String, text: String, attachments: [ChatAttachment])?
    private var commandSink: (([String: Any]) -> Bool)?
    private var starting = false

    init(bridge: BridgeModel) { self.bridge = bridge }
    /// A local transport seam for exercising real UI state transitions without
    /// a gateway process or a provider account.
    init(sendCommand: @escaping ([String: Any]) -> Bool) { commandSink = sendCommand }
    var selected: ChatSummary? { chats.first { $0.id == selectedID } }
    var selectedRoute: ChatRoute? { models.first { $0.route == selected?.route && $0.account == selected?.account } }
    var activeAccent: Color { selectedRoute?.accent ?? .secondary }
    var approvalMode: String { selected?.approvalMode ?? "manual" }
    var canSend: Bool { connected && !busy && selectedRoute != nil && (!draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !attachments.isEmpty) }
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
                    owner.restorePendingSend()
                    owner.output?.fileHandleForReading.readabilityHandler = nil; owner.output = nil
                    owner.setBusy(false); owner.approval = nil
                    owner.notice = "Chat disconnected. Reopen Chat to reconnect; saved work is kept."
                }
            }
            buffer = Data(); input = stdin; output = stdout; process = child
            try child.run()
        } catch { notice = error.localizedDescription; connected = false; process = nil; input = nil }
    }

    func shutdown() {
        write(["command": "stop"])
        try? input?.fileHandleForWriting.close()
        input = nil
    }
    func refresh() { write(["command": "refresh"]) }
    func newChat() {
        guard !busy else { return }
        let choice = selectedRoute ?? models.first
        guard let choice else { notice = "Configure a provider and refresh its models in Provider Hub."; return }
        write(["command": "create", "choice": choice.id, "workspace": selected?.workspace ?? recentFolders.first ?? NSHomeDirectory()])
    }
    func select(_ id: String) {
        guard !busy else { return }
        write(["command": "select", "id": id])
    }
    func send() {
        guard canSend, let selectedID else { return }
        let text = draft; draft = ""; drafts[selectedID] = ""; notice = ""
        let files = attachments; attachments = []; attachmentDrafts[selectedID] = []
        pendingSend = (selectedID, text, files)
        setBusy(true); status = "Connecting…"
        if !write(["command": "send", "id": selectedID, "text": text, "attachments": files.map(\.request)]) { restorePendingSend(); setBusy(false) }
    }
    func stop() { write(["command": "stop"]); status = "Stopping…" }
    func retry() {
        guard !busy, let selectedID else { return }
        notice = ""; setBusy(true); write(["command": "retry", "id": selectedID])
    }
    func setRoute(_ choiceID: String) {
        guard !busy, let choice = models.first(where: { $0.id == choiceID }) else { return }
        write(["command": "configure", "choice": choice.id])
    }
    func setSelection(_ choiceID: String, effort: String) {
        guard !busy, let choice = models.first(where: { $0.id == choiceID }) else { return }
        write(["command": "configure", "choice": choice.id, "effort": effort])
    }
    func setEffort(_ effort: String) { guard !busy else { return }; write(["command": "configure", "effort": effort]) }
    func setApprovalMode(_ mode: String) { guard !busy else { return }; write(["command": "configure", "approvalMode": mode]) }
    func setFolder(_ path: String) { guard !busy else { return }; write(["command": "configure", "workspace": path]) }
    func chooseFolder() {
        let picker = NSOpenPanel(); picker.canChooseDirectories = true; picker.canChooseFiles = false
        picker.allowsMultipleSelection = false; picker.prompt = "Use folder"
        if let path = selected?.workspace { picker.directoryURL = URL(fileURLWithPath: path) }
        if picker.runModal() == .OK, let url = picker.url { setFolder(url.path) }
    }
    func decideApproval(allow: Bool) {
        guard let approval else { return }; write(["command": "approve", "id": approval.id, "allow": allow])
        self.approval = nil; status = allow ? "Working…" : "Denied"
    }
    func chooseAttachments() {
        let picker = NSOpenPanel(); picker.canChooseFiles = true; picker.canChooseDirectories = false
        picker.allowsMultipleSelection = true; picker.prompt = "Attach"
        if picker.runModal() == .OK { addAttachments(picker.urls) }
    }
    func addAttachments(_ urls: [URL]) {
        guard selectedID != nil else { return }
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
    func rename(_ id: String, title: String) { guard !busy else { return }; write(["command": "rename", "id": id, "title": title]) }
    func delete(_ id: String) { guard !busy else { return }; write(["command": "delete", "id": id]) }

    private func setBusy(_ value: Bool) { busy = value; onActivity?(value) }
    private func restorePendingSend() {
        guard let pendingSend else { return }
        if selectedID == pendingSend.chat {
            draft = draft.isEmpty ? pendingSend.text : pendingSend.text + "\n\n" + draft
            attachments = pendingSend.attachments + attachments
        } else {
            let existing = drafts[pendingSend.chat] ?? ""
            drafts[pendingSend.chat] = existing.isEmpty ? pendingSend.text : pendingSend.text + "\n\n" + existing
            attachmentDrafts[pendingSend.chat] = pendingSend.attachments + (attachmentDrafts[pendingSend.chat] ?? [])
        }
        self.pendingSend = nil
    }
    @discardableResult private func write(_ message: [String: Any]) -> Bool {
        if let commandSink { return commandSink(message) }
        guard let input, let data = try? JSONSerialization.data(withJSONObject: message) else { return false }
        do { try input.fileHandleForWriting.write(contentsOf: data + Data([10])); return true }
        catch { notice = "Chat disconnected. Your saved transcript is kept."; connected = false; return false }
    }
    private func decode<T: Decodable>(_ type: T.Type, _ raw: Any?) -> T? {
        guard let raw, let data = try? JSONSerialization.data(withJSONObject: raw) else { return nil }
        return try? JSONDecoder().decode(type, from: data)
    }
    func consume(_ data: Data) {
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 10) {
            let line = buffer.prefix(upTo: newline); buffer.removeSubrange(...newline)
            guard let event = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
            switch event["event"] as? String {
            case "ready": connected = true
            case "catalogue":
                models = decode([ChatRoute].self, event["models"]) ?? []; recentFolders = event["folders"] as? [String] ?? []
                contextLimit = selectedRoute?.context; notice = ""
            case "chats": chats = decode([ChatSummary].self, event["chats"]) ?? []
            case "selected":
                let id = event["id"] as? String
                if id != selectedID {
                    if let selectedID { drafts[selectedID] = draft; attachmentDrafts[selectedID] = attachments }
                    draft = id.flatMap { drafts[$0] } ?? ""; attachments = id.flatMap { attachmentDrafts[$0] } ?? []
                }
                selectedID = id; entries = decode([ChatEntry].self, event["entries"]) ?? []
                tokenUsage = event["usage"] as? Int; contextLimit = selectedRoute?.context
            case "entry":
                guard event["chat"] as? String == selectedID, let entry = decode(ChatEntry.self, event["entry"]) else { continue }
                if entry.kind == "user", let pendingSend, pendingSend.chat == selectedID, pendingSend.text == entry.text {
                    self.pendingSend = nil
                }
                if let index = entries.firstIndex(where: { $0.id == entry.id }) { entries[index] = entry } else { entries.append(entry) }
            case "delta":
                guard event["chat"] as? String == selectedID, let id = event["id"] as? String,
                      let index = entries.firstIndex(where: { $0.id == id }) else { continue }
                entries[index].text += event["text"] as? String ?? ""
            case "approval": approval = decode(ChatApproval.self, event["approval"]); status = "Needs approval"
            case "state":
                setBusy(event["busy"] as? Bool ?? false); status = event["status"] as? String ?? "Ready"
                tokenUsage = event["usage"] as? Int ?? tokenUsage
                if !busy { approval = nil }
            case "error": restorePendingSend(); notice = event["message"] as? String ?? "Chat failed."; setBusy(false); approval = nil
            case "notice": notice = event["message"] as? String ?? ""
            default: break
            }
        }
    }
}
