import Foundation

/// Pure recursive multiview layout model. Foundation only: no SwiftUI/AppKit.
/// A layout is a binary tree of splits with stable UUID pane/split IDs.
/// Leaves carry an optional assignment: a chat id or a browser surface id.
public enum ChatPaneAssignment: Codable, Hashable, Equatable {
    case chat(id: String)
    case browser(surface: String)
    enum CodingKeys: String, CodingKey { case kind, id, surface }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        switch try c.decode(String.self, forKey: .kind) {
        case "chat": self = .chat(id: try c.decode(String.self, forKey: .id))
        case "browser": self = .browser(surface: try c.decode(String.self, forKey: .surface))
        default: throw DecodingError.dataCorruptedError(forKey: .kind, in: c, debugDescription: "unknown assignment kind")
        }
    }
    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .chat(let id): try c.encode("chat", forKey: .kind); try c.encode(id, forKey: .id)
        case .browser(let s): try c.encode("browser", forKey: .kind); try c.encode(s, forKey: .surface)
        }
    }
}

public struct ChatWorkspacePane: Codable, Hashable, Equatable, Identifiable {
    public var id: UUID
    public var assignment: ChatPaneAssignment?
    public init(id: UUID = UUID(), assignment: ChatPaneAssignment? = nil) {
        self.id = id; self.assignment = assignment
    }
}

public enum ChatWorkspaceAxis: String, Codable, Hashable { case horizontal, vertical }

/// Edge a new pane appears on when splitting a leaf. Right/bottom append after
/// the targeted leaf; left/top prepend before it.
public enum ChatWorkspaceEdge { case left, right, top, bottom }

public struct ChatWorkspaceSplit: Codable, Hashable, Equatable, Identifiable {
    public var id: UUID
    public var axis: ChatWorkspaceAxis
    public var ratio: Double
    public var first: ChatWorkspaceNode
    public var second: ChatWorkspaceNode
    public init(id: UUID = UUID(), axis: ChatWorkspaceAxis, ratio: Double = 0.5, first: ChatWorkspaceNode, second: ChatWorkspaceNode) {
        self.id = id; self.axis = axis; self.ratio = Self.clampRatio(ratio); self.first = first; self.second = second
    }
    public static func clampRatio(_ v: Double) -> Double { min(max(v.isFinite ? v : 0.5, 0.2), 0.8) }
}

public indirect enum ChatWorkspaceNode: Codable, Hashable, Equatable {
    case leaf(ChatWorkspacePane)
    case split(ChatWorkspaceSplit)
    enum CodingKeys: String, CodingKey { case kind, leaf, split }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        switch try c.decode(String.self, forKey: .kind) {
        case "leaf": self = .leaf(try c.decode(ChatWorkspacePane.self, forKey: .leaf))
        case "split": self = .split(try c.decode(ChatWorkspaceSplit.self, forKey: .split))
        default: throw DecodingError.dataCorruptedError(forKey: .kind, in: c, debugDescription: "unknown node kind")
        }
    }
    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .leaf(let p): try c.encode("leaf", forKey: .kind); try c.encode(p, forKey: .leaf)
        case .split(let s): try c.encode("split", forKey: .kind); try c.encode(s, forKey: .split)
        }
    }
}

public struct ChatWorkspaceLayout: Codable, Equatable {
    public var root: ChatWorkspaceNode
    public var focusedPaneID: UUID
    public static let minimumPaneSize: Double = 300
    public static let dividerSize: Double = 8
    public init(root: ChatWorkspaceNode, focusedPaneID: UUID) { self.root = root; self.focusedPaneID = focusedPaneID }
    public static func initial(chatID: String? = nil) -> ChatWorkspaceLayout {
        let pane = ChatWorkspacePane(assignment: chatID.map { .chat(id: $0) })
        return ChatWorkspaceLayout(root: .leaf(pane), focusedPaneID: pane.id)
    }
    public var panes: [(id: UUID, assignment: ChatPaneAssignment?)] {
        var out: [(UUID, ChatPaneAssignment?)] = []
        func walk(_ n: ChatWorkspaceNode) { switch n { case .leaf(let p): out.append((p.id, p.assignment)); case .split(let s): walk(s.first); walk(s.second) } }
        walk(root); return out
    }
    public func assignment(for paneID: UUID) -> ChatPaneAssignment?? {
        func walk(_ n: ChatWorkspaceNode) -> ChatPaneAssignment?? {
            switch n { case .leaf(let p): return p.id == paneID ? .some(p.assignment) : nil; case .split(let s): return walk(s.first) ?? walk(s.second) }
        }
        return walk(root)
    }
    public func contains(paneID: UUID) -> Bool { panes.contains { $0.id == paneID } }
    /// The pane showing a chat, so a chat is focused where it is rather than shown twice.
    public func pane(showing chatID: String) -> UUID? { panes.first { $0.assignment == .chat(id: chatID) }?.id }
    /// The pane `step` places after the focused one in reading order, wrapping
    /// at either end, for moving focus between panes from the keyboard.
    public func adjacentPane(_ step: Int) -> UUID {
        let ids = panes.map(\.id)
        guard let index = ids.firstIndex(of: focusedPaneID) else { return focusedPaneID }
        return ids[((index + step) % ids.count + ids.count) % ids.count]
    }
    public mutating func focus(paneID: UUID) { if contains(paneID: paneID) { focusedPaneID = paneID } }
    public mutating func assignChat(paneID: UUID, chatID: String) { assign(paneID: paneID, value: .chat(id: chatID)) }
    public mutating func assignBrowser(paneID: UUID, surface: String) { assign(paneID: paneID, value: .browser(surface: surface)) }
    private mutating func assign(paneID: UUID, value: ChatPaneAssignment) {
        func walk(_ n: ChatWorkspaceNode) -> ChatWorkspaceNode {
            switch n { case .leaf(var p): if p.id == paneID { p.assignment = value }; return .leaf(p); case .split(var s): s.first = walk(s.first); s.second = walk(s.second); return .split(s) }
        }
        root = walk(root)
    }
    public mutating func splitRight(_ paneID: UUID, newPaneID: UUID = UUID(), assignment: ChatPaneAssignment? = nil) { split(paneID, axis: .horizontal, newPaneID: newPaneID, assignment: assignment) }
    public mutating func splitBelow(_ paneID: UUID, newPaneID: UUID = UUID(), assignment: ChatPaneAssignment? = nil) { split(paneID, axis: .vertical, newPaneID: newPaneID, assignment: assignment) }
    /// Split a leaf so the new pane lands on `edge` of it; focus moves to it.
    public mutating func split(_ paneID: UUID, edge: ChatWorkspaceEdge, newPaneID: UUID = UUID(), assignment: ChatPaneAssignment? = nil) {
        split(paneID, axis: edge == .left || edge == .right ? .horizontal : .vertical, newPaneID: newPaneID, assignment: assignment)
        guard edge == .left || edge == .top else { return }
        func lead(_ node: ChatWorkspaceNode) -> ChatWorkspaceNode {
            guard case .split(var split) = node else { return node }
            if case .leaf(let child) = split.second, child.id == newPaneID { swap(&split.first, &split.second) }
            else { split.first = lead(split.first); split.second = lead(split.second) }
            return .split(split)
        }
        root = lead(root)
    }
    private mutating func split(_ paneID: UUID, axis: ChatWorkspaceAxis, newPaneID: UUID, assignment: ChatPaneAssignment?) {
        func walk(_ n: ChatWorkspaceNode) -> ChatWorkspaceNode {
            switch n {
            case .leaf(let p):
                guard p.id == paneID else { return n }
                let s = ChatWorkspaceSplit(axis: axis, first: .leaf(p), second: .leaf(ChatWorkspacePane(id: newPaneID, assignment: assignment)))
                return .split(s)
            case .split(var s): s.first = walk(s.first); s.second = walk(s.second); return .split(s)
            }
        }
        let before = panes.count
        root = walk(root)
        if panes.count != before { focusedPaneID = newPaneID }
    }
    @discardableResult public mutating func close(paneID: UUID) -> Bool {
        guard contains(paneID: paneID) else { return false }
        if case .leaf(var p) = root, p.id == paneID {
            if panes.count == 1 { p.assignment = nil; root = .leaf(p); return true }
        }
        func walk(_ n: ChatWorkspaceNode) -> ChatWorkspaceNode? {
            switch n {
            case .leaf(let p): return p.id == paneID ? nil : n
            case .split(let s):
                let a = walk(s.first), b = walk(s.second)
                switch (a, b) { case (nil, let s2?): return s2; case (let s1?, nil): return s1; case (let s1?, let s2?): var c = s; c.first = s1; c.second = s2; return .split(c); default: return n }
            }
        }
        guard let next = walk(root) else { return false }
        root = next
        if !contains(paneID: focusedPaneID) { focusedPaneID = panes.first?.id ?? focusedPaneID }
        return true
    }
    public mutating func setRatio(splitID: UUID, ratio: Double) {
        func walk(_ n: ChatWorkspaceNode) -> ChatWorkspaceNode {
            switch n { case .leaf: return n; case .split(var s): if s.id == splitID { s.ratio = ChatWorkspaceSplit.clampRatio(ratio) }; s.first = walk(s.first); s.second = walk(s.second); return .split(s) }
        }
        root = walk(root)
    }
    public static func maxPanes(total: Double) -> Int { max(1, Int((total + dividerSize) / (minimumPaneSize + dividerSize))) }
    public func save() throws -> Data { try JSONEncoder().encode(self) }
    public static func load(from data: Data) throws -> ChatWorkspaceLayout {
        guard data.count <= 1_000_000 else { throw LayoutError.invalid }
        // Inspect JSON before recursive Codable decoding to bound malformed depth.
        let json = try JSONSerialization.jsonObject(with: data)
        func depth(_ value: Any, _ level: Int) throws {
            guard level <= 128 else { throw LayoutError.invalid }
            if let object = value as? [String: Any] { for child in object.values { try depth(child, level + 1) } }
            if let array = value as? [Any] { for child in array { try depth(child, level + 1) } }
        }
        try depth(json, 0)
        let result = try JSONDecoder().decode(ChatWorkspaceLayout.self, from: data)
        var ids = Set<UUID>()
        func check(_ node: ChatWorkspaceNode, _ level: Int) throws {
            guard level <= 32 else { throw LayoutError.invalid }
            switch node {
            case .leaf(let pane): guard ids.insert(pane.id).inserted else { throw LayoutError.invalid }
            case .split(let split):
                guard ids.insert(split.id).inserted, split.ratio.isFinite, (0.2...0.8).contains(split.ratio) else { throw LayoutError.invalid }
                try check(split.first, level + 1); try check(split.second, level + 1)
            }
        }
        try check(result.root, 0)
        guard result.contains(paneID: result.focusedPaneID) else { throw LayoutError.invalid }
        return result
    }
    public enum LayoutError: Error { case invalid }
}
