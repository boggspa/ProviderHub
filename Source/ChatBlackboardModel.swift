import AppKit
import SwiftUI

/// One pinned Blackboard post. `author` is a Team member ID or "user"; the
/// name and route are captured when it was written, so a post keeps its chip
/// after the member leaves the roster.
struct ChatBlackboardPost: Decodable, Identifiable, Equatable {
    struct Quote: Decodable, Equatable {
        var entryID: String; var kind: String; var author: String; var excerpt: String; var tool: String?
    }
    var id: String; var author: String; var authorName: String; var route: String
    var key: String; var body: String; var category: String; var created: String; var updated: String
    var quote: Quote?
    var byUser: Bool { author == "user" }
    var updatedDate: Date? { ChatBlackboardSnapshot.date(updated) }
    private enum CodingKeys: String, CodingKey { case id, author, authorName, route, key, body, category, created, updated, quote }
    init(id: String, author: String, authorName: String, route: String = "", key: String, body: String,
         category: String = "note", created: String = "", updated: String = "", quote: Quote? = nil) {
        self.id = id; self.author = author; self.authorName = authorName; self.route = route; self.key = key
        self.body = body; self.category = category; self.created = created; self.updated = updated; self.quote = quote
    }
    init(from decoder: Decoder) throws {
        let row = try decoder.container(keyedBy: CodingKeys.self)
        id = try row.decode(String.self, forKey: .id); author = try row.decode(String.self, forKey: .author)
        authorName = try row.decode(String.self, forKey: .authorName); key = try row.decode(String.self, forKey: .key)
        body = try row.decode(String.self, forKey: .body); category = try row.decode(String.self, forKey: .category)
        route = (try? row.decodeIfPresent(String.self, forKey: .route)) ?? ""
        created = (try? row.decodeIfPresent(String.self, forKey: .created)) ?? ""
        updated = (try? row.decodeIfPresent(String.self, forKey: .updated)) ?? created
        quote = try? row.decodeIfPresent(Quote.self, forKey: .quote)
    }
}

/// A file or link on the board: added to it directly (`source == "board"`,
/// removable) or attached to one of the user's messages (`"message"`).
struct ChatBlackboardFile: Decodable, Identifiable, Equatable {
    var id: String; var name: String; var kind: String; var path: String?; var url: String?
    var size: Int; var source: String; var entryID: String?
    /// Who added a board item: a member ID or "user". Message files are the user's.
    var author: String?; var authorName: String?; var route: String?
    /// The user may remove any added item; message files leave with their message.
    var removable: Bool { source == "board" }
    var byUser: Bool { source == "message" || (author ?? "user") == "user" }
    private enum CodingKeys: String, CodingKey { case id, name, kind, path, url, size, source, entryID, author, authorName, route }
    init(id: String, name: String, kind: String, path: String? = nil, url: String? = nil, size: Int = 0, source: String = "board", entryID: String? = nil,
         author: String? = nil, authorName: String? = nil, route: String? = nil) {
        self.id = id; self.name = name; self.kind = kind; self.path = path; self.url = url
        self.size = size; self.source = source; self.entryID = entryID
        self.author = author; self.authorName = authorName; self.route = route
    }
    init(from decoder: Decoder) throws {
        let row = try decoder.container(keyedBy: CodingKeys.self)
        id = try row.decode(String.self, forKey: .id); name = try row.decode(String.self, forKey: .name)
        kind = try row.decode(String.self, forKey: .kind)
        path = try? row.decodeIfPresent(String.self, forKey: .path); url = try? row.decodeIfPresent(String.self, forKey: .url)
        size = (try? row.decodeIfPresent(Int.self, forKey: .size)) ?? 0
        source = (try? row.decodeIfPresent(String.self, forKey: .source)) ?? "board"
        entryID = try? row.decodeIfPresent(String.self, forKey: .entryID)
        author = try? row.decodeIfPresent(String.self, forKey: .author)
        authorName = try? row.decodeIfPresent(String.self, forKey: .authorName)
        route = try? row.decodeIfPresent(String.self, forKey: .route)
    }
}

struct ChatBlackboardSnapshot: Decodable, Equatable {
    struct Limits: Decodable, Equatable {
        var posts: Int; var bodyBytes: Int; var boardBytes: Int; var usedBytes: Int; var attachments: Int; var attachmentBytes: Int
    }
    /// The worker's CATEGORIES (chat_blackboard.py), in display order.
    static let categories = ["decision", "fact", "risk", "do-not-repeat", "note"]
    /// How many posts the inspector shows before "Open Blackboard".
    static let inspectorPosts = 8
    var posts: [ChatBlackboardPost]; var attachments: [ChatBlackboardFile]
    var omittedAttachments: Int; var thumbnails: String; var limits: Limits
    private enum CodingKeys: String, CodingKey { case posts, attachments, omittedAttachments, thumbnails, limits }
    init(from decoder: Decoder) throws {
        let row = try decoder.container(keyedBy: CodingKeys.self)
        // Row by row: one malformed post or file must not hide the rest.
        posts = (try row.decode([Lossy<ChatBlackboardPost>].self, forKey: .posts)).compactMap(\.value)
        attachments = ((try? row.decodeIfPresent([Lossy<ChatBlackboardFile>].self, forKey: .attachments)) ?? []).compactMap(\.value)
        omittedAttachments = (try? row.decodeIfPresent(Int.self, forKey: .omittedAttachments)) ?? 0
        thumbnails = (try? row.decodeIfPresent(String.self, forKey: .thumbnails)) ?? ""
        limits = try row.decode(Limits.self, forKey: .limits)
    }
    /// Newest first by last update; ties keep the worker's order.
    var newest: [ChatBlackboardPost] {
        posts.enumerated().sorted { ($0.element.updated, $0.offset) > ($1.element.updated, $1.offset) }.map(\.element)
    }
    var latest: [ChatBlackboardPost] { Array(newest.prefix(Self.inspectorPosts)) }
    var groups: [(category: String, posts: [ChatBlackboardPost])] {
        let ordered = newest
        let known = Self.categories.map { category in (category, ordered.filter { $0.category == category }) }
        let other = ordered.filter { !Self.categories.contains($0.category) }
        return (known + [("other", other)]).filter { !$0.1.isEmpty }.map { (category: $0.0, posts: $0.1) }
    }
    var full: Bool { posts.count >= limits.posts }
    static func date(_ text: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: text) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: text)
    }
    static func title(_ category: String) -> String {
        switch category {
        case "decision": "Decisions"; case "fact": "Facts"; case "risk": "Risks"
        case "do-not-repeat": "Do not repeat"; case "note": "Notes"; default: "Other"
        }
    }
    static func label(_ category: String) -> String { category == "do-not-repeat" ? "Do not repeat" : category.capitalized }
    /// A worker key from what the user typed, or from the body when blank.
    static func key(_ typed: String, body: String) -> String {
        let source = typed.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? body.split(whereSeparator: \.isWhitespace).prefix(4).joined(separator: "-") : typed
        var key = ""
        for scalar in source.lowercased().unicodeScalars {
            if CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyz0123456789_.-").contains(scalar) { key.unicodeScalars.append(scalar) }
            else if CharacterSet.whitespaces.contains(scalar) || scalar == "/", key.last != "-" { key.append("-") }
        }
        while let first = key.first, !(first.isLetter || first.isNumber) { key.removeFirst() }
        key = String(key.prefix(64))
        return key.isEmpty ? "note" : key
    }
}

private struct Lossy<T: Decodable>: Decodable {
    var value: T?
    init(from decoder: Decoder) throws { value = try? T(from: decoder) }
}

struct ChatBlackboardState: Equatable {
    var snapshot: ChatBlackboardSnapshot?
    var notice = ""
    /// The outstanding user command, if any; a reply carrying it settles it.
    var request: String?
    /// The last settled request and whether it succeeded, for clearing drafts.
    var settled: String?
    var settledOK = false
}

/// Per-chat Blackboard state. Kept apart from ChatSessionState so the board
/// never counts as active work, and so a Blackboard window can show a chat
/// that is not selected in the main window.
@MainActor
final class ChatBlackboardModel: ObservableObject {
    @Published private(set) var states: [String: ChatBlackboardState] = [:]
    func state(_ chat: String?) -> ChatBlackboardState { chat.flatMap { states[$0] } ?? ChatBlackboardState() }
    func reset(_ chat: String) { states[chat] = nil }
    func begin(_ chat: String, request: String) {
        var state = state(chat); state.request = request; state.notice = ""; states[chat] = state
    }
    func fail(_ chat: String, _ notice: String) {
        var state = state(chat); state.request = nil; state.notice = notice; states[chat] = state
    }
    /// A `blackboard` event: a snapshot, a notice, or both. Snapshots are whole
    /// boards in worker order, so the newest event always wins; a request ID
    /// only settles the matching command and its notice.
    func consume(chat: String, event: [String: Any], snapshot decode: () -> ChatBlackboardSnapshot?) {
        var state = state(chat)
        let request = event["request"] as? String
        let notice = event["notice"] as? String
        if event.keys.contains("blackboard") {
            if event["blackboard"] is NSNull { state.snapshot = nil }
            else if let snapshot = decode() { state.snapshot = snapshot }
            else { state.notice = "The Blackboard could not be read. Reopen the chat to reload it." }
        }
        if let request, request == state.request {
            state.request = nil; state.settled = request; state.settledOK = notice == nil
            state.notice = notice ?? ""
        } else if request == nil, let notice {
            state.notice = notice
        }
        if states[chat] != state { states[chat] = state }
    }
}

extension ChatModel {
    /// Ask the worker for the current board; replies arrive as `blackboard`.
    func refreshBlackboard(_ chat: String? = nil) {
        guard connected, let owner = chat ?? stateChatID else { return }
        inspectorCommand(["command": "blackboard", "id": owner])
    }
    @discardableResult func blackboardCommand(_ chat: String?, _ command: [String: Any]) -> String? {
        guard let owner = chat ?? stateChatID else { return nil }
        guard connected else { blackboard.fail(owner, "Chat is disconnected."); return nil }
        let request = UUID().uuidString
        var command = command; command["id"] = owner; command["request"] = request
        blackboard.begin(owner, request: request)
        if !inspectorCommand(command) { blackboard.fail(owner, "Chat is disconnected."); return nil }
        return request
    }
    @discardableResult func postToBlackboard(_ chat: String? = nil, key: String, body: String, category: String, quote: String? = nil) -> String? {
        let text = body.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        var command: [String: Any] = ["command": "blackboard_post", "key": ChatBlackboardSnapshot.key(key, body: text),
                                      "body": text, "category": category]
        if let quote { command["quote"] = quote }
        return blackboardCommand(chat, command)
    }
    func removeBlackboardPost(_ chat: String? = nil, post: String) {
        blackboardCommand(chat, ["command": "blackboard_remove", "post": post])
    }
    func attachToBlackboard(_ chat: String? = nil, urls: [URL]) {
        let files = urls.filter(\.isFileURL).map(\.path)
        guard !files.isEmpty else { return }
        blackboardCommand(chat, ["command": "blackboard_attach", "paths": files])
    }
    @discardableResult func linkToBlackboard(_ chat: String? = nil, url: String, title: String = "") -> String? {
        let link = url.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !link.isEmpty else { return nil }
        var command: [String: Any] = ["command": "blackboard_attach", "url": link]
        let name = title.trimmingCharacters(in: .whitespacesAndNewlines)
        if !name.isEmpty { command["title"] = name }
        return blackboardCommand(chat, command)
    }
    func detachFromBlackboard(_ chat: String? = nil, attachment: String) {
        blackboardCommand(chat, ["command": "blackboard_detach", "attachment": attachment])
    }
    func chooseBlackboardFiles(_ chat: String? = nil) {
        guard let owner = chat ?? stateChatID else { return }
        let picker = NSOpenPanel(); picker.canChooseFiles = true; picker.canChooseDirectories = false
        picker.allowsMultipleSelection = true; picker.prompt = "Add to Blackboard"
        if picker.runModal() == .OK { attachToBlackboard(owner, urls: picker.urls) }
    }
}
