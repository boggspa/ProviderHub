import Foundation

/// JSON values for the versioned, presentation-only inspector protocol.
public enum RemoteValue: Codable, Equatable, Hashable {
    case object([String: RemoteValue]), array([RemoteValue]), string(String), number(Double), bool(Bool), null
    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode([RemoteValue].self) { self = .array(v) }
        else { self = .object(try c.decode([String: RemoteValue].self)) }
    }
    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .object(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .string(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }
    public subscript(_ key: String) -> RemoteValue { object[key] ?? .null }
    public var object: [String: RemoteValue] { if case .object(let v) = self { return v }; return [:] }
    public var rows: [RemoteValue] { if case .array(let v) = self { return v }; return [] }
    public var text: String { if case .string(let v) = self { return v }; return "" }
    public var flag: Bool { if case .bool(let v) = self { return v }; return false }
    public var int: Int { if case .number(let v) = self, v.isFinite, abs(v) < Double(Int.max) { return Int(v) }; return 0 }
    var optionalInt: Int? { if case .number(let v) = self, v.isFinite, abs(v) < Double(Int.max) { return Int(v) }; return nil }
}

public enum RemoteInspectorTab: String, CaseIterable, Identifiable {
    case changes, agents, side, processes, browser
    public var id: String { rawValue }
    public var title: String {
        switch self { case .changes: return "Changes"; case .agents: return "Team"; case .side: return "Side Chat"; case .processes: return "Processes"; case .browser: return "Browser" }
    }
    public var icon: String {
        switch self { case .changes: return "filemenu.and.selection"; case .agents: return "person.2"; case .side: return "bubble.left.and.bubble.right"; case .processes: return "terminal"; case .browser: return "globe" }
    }
}

extension CompanionModel {
    public var configuration: RemoteValue { store.controls["configuration"] ?? .null }
    public var team: RemoteValue { store.controls["team"] ?? .null }
    public var members: [RemoteValue] { team["members"].rows }
    public var models: [RemoteValue] { fullModelCatalogue.isEmpty ? store.controls["models"]?.rows ?? [] : fullModelCatalogue }
    public var controlSurfaceAvailable: Bool { (store.controls["version"]?.int ?? 0) >= 1 }
    @discardableResult
    public func control(_ action: String, _ arguments: [String: RemoteValue] = [:]) -> String? {
        guard isConnected, let chat = selectedChatID, store.selectedID == chat else { return nil }
        var command = RemoteCommand(method: "chat.control", requestId: UUID().uuidString, chat: chat)
        command.action = action; command.arguments = arguments
        link?.send(command)
        return command.requestId
    }
    public func refreshInspector(_ tab: RemoteInspectorTab) {
        switch tab {
        case .changes: control("inspect_git")
        case .processes: control("processes")
        case .browser: control("remote_browser", ["tool": .string("browser_list"), "arguments": .object([:])])
        default: control("remote_inspect")
        }
    }
    public func configure(_ values: [String: RemoteValue]) {
        guard !configuration["busy"].flag else { composeNotice = "Stop this chat before changing its settings, as on your Mac."; return }
        guard !configuration["workspace"].text.isEmpty else { composeNotice = "Refresh this chat’s Mac connection before changing settings."; return }
        let owner = selectedChatID
        let arguments = values.merging(["expectedWorkspace": configuration["workspace"]]) { _, new in new }
        Task { @MainActor in
            do {
                _ = try await performControl("configure", arguments, chat: owner)
                _ = try await performControl("remote_inspect", chat: owner)
                composeNotice = ""
            } catch { composeNotice = error.localizedDescription }
        }
    }
    public func configureMembers(_ rows: [RemoteValue], enabled: Bool = true, execution: RemoteValue? = nil) {
        var arguments: [String: RemoteValue] = ["enabled": .bool(enabled), "members": .array(rows.map { row in
            .object(row.object.filter { ["id", "name", "choice", "effort", "responsibility"].contains($0.key) })
        })]
        if let execution { arguments["execution"] = execution }
        else if case .object = team["execution"] { arguments["execution"] = team["execution"] }
        control("configure_team", arguments)
    }
}
