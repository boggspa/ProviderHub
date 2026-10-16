import Foundation

public struct RemoteChat: Codable, Equatable, Identifiable {
    public var id: String
    public var title: String
    public var status: String
    public var busy: Bool
    public var pendingApproval: Bool
    public var modelLabel: String?
    public var providerLabel: String?
    public var accent: String?
    public var approvalMode: String?
    public var route: String?
    public init(id: String, title: String, status: String, busy: Bool, pendingApproval: Bool) {
        self.id = id
        self.title = title
        self.status = status
        self.busy = busy
        self.pendingApproval = pendingApproval
    }
}

public struct RemoteEntry: Equatable, Identifiable {
    public var id: String
    public var role: String
    public var text: String
    public var summary: String
    public var detail: String
    public var tool: String
    public var memberName: String
    public var isError: Bool
    public var isLive: Bool = false
    public var question: RemoteQuestion? = nil
    public var route: String = ""
    public var memberID: String = ""
    public var agentID: String = ""
    public var time: Double? = nil
    public var changedFiles: [String] = []

    static func decode(_ row: [String: Any], fallback: String) -> RemoteEntry {
        let identifier = row["id"] as? String
        var decoded = RemoteEntry(id: identifier == nil || identifier == "legacy" || identifier == "" ? fallback : identifier!, role: row["role"] as? String ?? "note",
                    text: row["text"] as? String ?? "", summary: row["summary"] as? String ?? "",
                    detail: row["detail"] as? String ?? "", tool: row["tool"] as? String ?? "",
                    memberName: row["memberName"] as? String ?? "", isError: row["isError"] as? Bool ?? false,
                    isLive: row["isLive"] as? Bool ?? false)
        if let question = row["question"], let data = try? JSONSerialization.data(withJSONObject: question) {
            decoded.question = try? JSONDecoder().decode(RemoteQuestion.self, from: data)
        }
        decoded.route = row["route"] as? String ?? ""
        decoded.memberID = row["memberID"] as? String ?? ""
        decoded.agentID = row["agentID"] as? String ?? ""
        decoded.time = row["time"] as? Double
        decoded.changedFiles = row["changedFiles"] as? [String] ?? []
        return decoded
    }
}

public struct RemoteApproval: Codable, Equatable {
    public var chat: String?
    public var id: String
    public var summary: String
    public var detail: String
    public init(id: String, summary: String, detail: String) {
        self.id = id
        self.summary = summary
        self.detail = detail
    }
}

public struct RemoteSnapshot: Codable, Equatable {
    public var cursor: Int
    public var chats: [RemoteChat]
    public var approval: RemoteApproval?
    public var selectedID: String?
}

public struct RemoteDelta: Codable, Equatable {
    public var cursor: Int
    public var event: String
    public var message: String?
    public var chats: [RemoteChat]?
    public var approval: RemoteApproval?
}

/// Cursor rules shared with the Mac bridge: contiguous deltas apply once;
/// a hole is not guessed. The phone never holds provider credentials.
public struct CompanionStore: Equatable {
    public private(set) var cursor = 0
    public private(set) var chats: [RemoteChat] = []
    public private(set) var approval: RemoteApproval?
    public private(set) var selectedID: String?
    public private(set) var notices: [String] = []
    public private(set) var needsSnapshot = false
    public private(set) var acks: [String: Bool] = [:]
    public private(set) var entries: [RemoteEntry] = []
    public private(set) var controls: [String: RemoteValue] = [:]
    public var transcript: String { entries.map { "\($0.role): \($0.text)\n\n" }.joined() }

    public init() {}

    public mutating func receive(_ message: [String: Any]) throws {
        guard let method = message["method"] as? String,
              let params = message["params"] as? [String: Any] else { throw RemoteProtocolError.rejected }
        func decode<T: Decodable>(_ type: T.Type, _ value: Any) throws -> T {
            try JSONDecoder().decode(type, from: JSONSerialization.data(withJSONObject: value, options: [.fragmentsAllowed]))
        }
        if method == "session.snapshot" {
            cursor = params["cursor"] as? Int ?? 0
            chats = try decode([RemoteChat].self, params["chats"] ?? [])
            approval = try (params["approval"] as? [String: Any]).map { try decode(RemoteApproval.self, $0) }
            let selected = params["selected"] as? [String: Any]
            selectedID = selected?["id"] as? String
            controls = try decode([String: RemoteValue].self, params["controls"] ?? [:])
            setEntries(selected?["entries"])
            if let live = selected?["live"] as? String, !live.isEmpty {
                let id = selected?["liveID"] as? String ?? "live"
                if let index = entries.firstIndex(where: { $0.id == id }) {
                    entries[index].text = live; entries[index].isLive = true
                } else {
                    var row = RemoteEntry.decode(["role":"assistant", "text":live], fallback:id)
                    row.isLive = true; entries.append(row)
                }
            }
            trimEntries()
            needsSnapshot = false
        } else if method == "session.delta" {
            guard let next = params["cursor"] as? Int, next > cursor else { return }
            guard next == cursor + 1 else { needsSnapshot = true; return }
            guard let event = params["event"] as? [String: Any], let kind = event["event"] as? String else { throw RemoteProtocolError.rejected }
            cursor = next
            switch kind {
            case "side_delta":
                if event["chat"] as? String == selectedID,
                   case .object(var side) = controls["side"], side["id"]?.text == event["side"] as? String {
                    var rows = side["entries"]?.rows ?? []
                    let id = event["id"] as? String ?? "side-live"
                    let index = rows.firstIndex { $0["id"].text == id } ?? rows.count
                    if index == rows.count { rows.append(.object(["id": .string(id), "role": .string("assistant"), "text": .string("")])) }
                    var row = rows[index].object
                    let current = row["text"]?.text ?? ""
                    let offset = event["offset"] as? Int ?? 0
                    if offset <= current.unicodeScalars.count, offset >= 0 {
                        let addition = (event["text"] as? String ?? "").unicodeScalars.dropFirst(current.unicodeScalars.count - offset)
                        row["text"] = .string(String(String.UnicodeScalarView((current + String(String.UnicodeScalarView(addition))).unicodeScalars.prefix(4000))))
                        rows[index] = .object(row); side["entries"] = .array(Array(rows.suffix(12))); controls["side"] = .object(side)
                    }
                }
            case "control":
                let owner = event["chat"] as? String
                if owner == nil || owner == selectedID, let panel = event["panel"] as? String {
                    controls[panel] = try decode(RemoteValue.self, event["value"] ?? NSNull())
                    controls["version"] = .number(1)
                    if let notice = event["notice"] as? String, !notice.isEmpty { notices.append(notice) }
                }
            case "chats": chats = try decode([RemoteChat].self, event["chats"] ?? [])
            case "approval":
                var value = event["approval"] as? [String: Any]
                value?["chat"] = event["chat"]
                approval = try value.map { try decode(RemoteApproval.self, $0) }
            case "selected", "snapshot":
                let next = (event["id"] ?? event["chat"]) as? String
                if next != selectedID { controls = controls.filter { ["version", "models", "browserEnabled"].contains($0.key) } }
                selectedID = next
                setEntries(event["entries"])
            case "delta":
                if event["chat"] as? String == selectedID {
                    let id = event["id"] as? String ?? "live"
                    let addition = event["text"] as? String ?? ""
                    if let index = entries.firstIndex(where: { $0.id == id }) {
                        let current = entries[index].text
                        if let offset = event["offset"] as? Int {
                            let overlap = current.unicodeScalars.count - offset
                            guard overlap >= 0 else { needsSnapshot = true; return }
                            entries[index].text += String(String.UnicodeScalarView(addition.unicodeScalars.dropFirst(overlap)))
                        } else { entries[index].text += addition }
                        entries[index].isLive = true
                    } else {
                        guard (event["offset"] as? Int ?? 0) == 0 else { needsSnapshot = true; return }
                        var entry = RemoteEntry.decode(["role":"assistant", "text":addition], fallback:id)
                        entry.isLive = true; entries.append(entry)
                    }
                    trimEntries()
                }
            case "entry":
                if event["chat"] as? String == selectedID, let entry = event["entry"] as? [String: Any] {
                    let row = RemoteEntry.decode(entry, fallback: "legacy-" + UUID().uuidString)
                    if let index = entries.firstIndex(where: { $0.id == row.id && row.id != "legacy" }) {
                        entries[index] = row
                    } else if row.role == "assistant", let index = entries.firstIndex(where: { $0.id == "live" }) {
                        entries[index] = row
                    } else { entries.append(row) }
                    trimEntries()
                }
            default:
                if let text = event["message"] as? String { notices.append(text) }
            }
        } else if method == "session.ack", let id = params["requestId"] as? String {
            acks[id] = params["ok"] as? Bool ?? false
            if let error = params["error"] as? String { notices.append(error) }
        }
    }

    private mutating func setEntries(_ value: Any?) {
        entries = (value as? [[String: Any]] ?? []).enumerated().map { RemoteEntry.decode($0.element, fallback: "snapshot-\($0.offset)") }
        trimEntries()
    }
    private mutating func trimEntries() {
        if entries.count > 30 { entries.removeFirst(entries.count - 30) }
        for index in entries.indices where entries[index].text.unicodeScalars.count > 8000 {
            entries[index].text = String(String.UnicodeScalarView(entries[index].text.unicodeScalars.prefix(8000)))
        }
    }

    public mutating func apply(method: String, snapshot: RemoteSnapshot? = nil, delta: RemoteDelta? = nil, requestId: String? = nil, ok: Bool? = nil) {
        switch method {
        case "session.snapshot":
            guard let snapshot else { return }
            cursor = snapshot.cursor
            chats = snapshot.chats
            approval = snapshot.approval
            selectedID = snapshot.selectedID
            needsSnapshot = false
        case "session.delta":
            guard let delta else { return }
            if delta.cursor <= cursor { return }
            if delta.cursor != cursor + 1 {
                needsSnapshot = true
                return
            }
            cursor = delta.cursor
            if let chats = delta.chats { self.chats = chats }
            if delta.event == "approval" { approval = delta.approval }
            if let message = delta.message { notices.append(message) }
        case "session.ack":
            if let requestId, let ok { acks[requestId] = ok }
        default:
            break
        }
    }
}

public struct RemoteCommand: Codable, Equatable {
    public var v: Int
    public var proto: String
    public var method: String
    public var requestId: String
    public var chat: String?
    public var text: String?
    public var approvalId: String?
    public var allow: Bool?
    public var questionId: String?
    public var answers: [String: [String]]?
    public var dismiss: Bool?
    public var action: String?
    public var arguments: [String: RemoteValue]?

    public init(method: String, requestId: String, chat: String? = nil, text: String? = nil, approvalId: String? = nil, allow: Bool? = nil) {
        self.v = 1
        self.proto = "providerhub-remote-app-v1"
        self.method = method
        self.requestId = requestId
        self.chat = chat
        self.text = text
        self.approvalId = approvalId
        self.allow = allow
    }
}
