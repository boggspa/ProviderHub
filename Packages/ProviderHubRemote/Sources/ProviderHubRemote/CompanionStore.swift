import Foundation

public struct RemoteChat: Codable, Equatable, Identifiable {
    public var id: String
    public var title: String
    public var status: String
    public var busy: Bool
    public var pendingApproval: Bool
    public init(id: String, title: String, status: String, busy: Bool, pendingApproval: Bool) {
        self.id = id
        self.title = title
        self.status = status
        self.busy = busy
        self.pendingApproval = pendingApproval
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
    public private(set) var transcript = ""
    private var committedTranscript = ""
    private var liveText = ""

    public init() {}

    public mutating func receive(_ message: [String: Any]) throws {
        guard let method = message["method"] as? String,
              let params = message["params"] as? [String: Any] else { throw RemoteProtocolError.rejected }
        func decode<T: Decodable>(_ type: T.Type, _ value: Any) throws -> T {
            try JSONDecoder().decode(type, from: JSONSerialization.data(withJSONObject: value))
        }
        if method == "session.snapshot" {
            cursor = params["cursor"] as? Int ?? 0
            chats = try decode([RemoteChat].self, params["chats"] ?? [])
            approval = try (params["approval"] as? [String: Any]).map { try decode(RemoteApproval.self, $0) }
            let selected = params["selected"] as? [String: Any]
            selectedID = selected?["id"] as? String
            committedTranscript = Self.text(selected?["entries"])
            liveText = selected?["live"] as? String ?? ""
            transcript = committedTranscript + liveText
            needsSnapshot = false
        } else if method == "session.delta" {
            guard let next = params["cursor"] as? Int, next > cursor else { return }
            guard next == cursor + 1 else { needsSnapshot = true; return }
            guard let event = params["event"] as? [String: Any], let kind = event["event"] as? String else { throw RemoteProtocolError.rejected }
            cursor = next
            switch kind {
            case "chats": chats = try decode([RemoteChat].self, event["chats"] ?? [])
            case "approval":
                var value = event["approval"] as? [String: Any]
                value?["chat"] = event["chat"]
                approval = try value.map { try decode(RemoteApproval.self, $0) }
            case "selected", "snapshot":
                selectedID = (event["id"] ?? event["chat"]) as? String
                committedTranscript = Self.text(event["entries"])
                liveText = ""
                transcript = committedTranscript
            case "delta":
                if event["chat"] as? String == selectedID {
                    liveText += event["text"] as? String ?? ""
                    transcript = committedTranscript + liveText
                }
            case "entry":
                if event["chat"] as? String == selectedID, let entry = event["entry"] as? [String: Any] {
                    committedTranscript += Self.text([entry])
                    if entry["role"] as? String == "assistant" { liveText = "" }
                    transcript = committedTranscript + liveText
                }
            default:
                if let text = event["message"] as? String { notices.append(text) }
            }
        } else if method == "session.ack", let id = params["requestId"] as? String {
            acks[id] = params["ok"] as? Bool ?? false
            if let error = params["error"] as? String { notices.append(error) }
        }
    }

    private static func text(_ value: Any?) -> String {
        (value as? [[String: Any]] ?? []).map { "\($0["role"] as? String ?? "note"): \($0["text"] as? String ?? "")\n\n" }.joined()
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
