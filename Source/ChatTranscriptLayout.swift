import Foundation

// The transcript as it is drawn, kept free of views and of ChatModel so the
// native test harness can exercise it with its own records. Nothing here
// changes saved text: copy, recall and the runtime still see every entry.

/// What the outline needs from a recorded entry. ChatEntry conforms in the
/// app; tests supply a small record of their own.
protocol ChatTranscriptItem {
    var id: String { get }
    var kind: String { get }
    var text: String { get }
    var route: String { get }
    var tool: String? { get }
    var summary: String? { get }
    var detail: String? { get }
    var memberID: String? { get }
    var memberName: String? { get }
    var agentID: String? { get }
    var agentIDs: [String]? { get }
}

/// Who is speaking, then what they did, the way Claude and Codex Desktop read.
/// Consecutive replies and tool calls from one speaker share one header; an
/// assistant record with no text (the gap between tool rounds) draws nothing
/// unless it is the live reply; a run of local tool rows folds into one
/// "Worked" disclosure; and routine notices that only described housekeeping
/// stay out of the way.
struct ChatTranscriptSegment<Item: ChatTranscriptItem>: Identifiable {
    enum Content {
        /// The header for a speaker block; the item is the block's first record.
        case speaker(Item)
        case entry(Item)
        case fold([Item])
    }
    /// The space above a row: wide between speakers, tight inside a block.
    enum Spacing: Equatable { case none, speaker, header, item }

    let content: Content
    let spacing: Spacing
    /// The speaker block this row follows, so a notice can say whose
    /// checkpoint it records. Nil when no speaker came before it.
    let previousSpeaker: Item?

    var id: String {
        switch content {
        case .speaker(let item): return "speaker-" + item.id
        case .entry(let item): return item.id
        case .fold(let items): return Self.foldID(items)
        }
    }
    var isSpeaker: Bool { if case .speaker = content { return true }; return false }

    static var minimumFold: Int { 3 }
    static func foldID(_ items: [Item]) -> String { "fold-" + (items.first?.id ?? "") }

    static func outline(_ items: [Item], isLive: (Item) -> Bool) -> [ChatTranscriptSegment] {
        var result: [ChatTranscriptSegment] = []
        var speaker: String?
        var lastSpeaker: Item?
        var opened = false
        var run: [Item] = []

        func push(_ content: Content, _ spacing: Spacing) {
            result.append(ChatTranscriptSegment(content: content, spacing: result.isEmpty ? .none : spacing,
                                                previousSpeaker: lastSpeaker))
        }
        func pushInBlock(_ content: Content) {
            push(content, opened ? .header : .item)
            opened = false
        }
        func flush() {
            guard !run.isEmpty else { return }
            if run.count >= minimumFold { pushInBlock(.fold(run)) } else { run.forEach { pushInBlock(.entry($0)) } }
            run.removeAll()
        }

        for item in items {
            switch item.kind {
            case "assistant", "tool":
                if item.kind == "assistant", ChatReplySources.isBlank(item.text), !isLive(item) { continue }
                let key = (item.memberID ?? "") + "\u{1F}" + item.route
                if key != speaker {
                    flush()
                    speaker = key
                    push(.speaker(item), .speaker)
                    lastSpeaker = item; opened = true
                }
                if item.kind == "tool", item.agentID == nil, item.agentIDs?.isEmpty != false {
                    run.append(item)
                } else {
                    flush(); pushInBlock(.entry(item))
                }
            default:
                if item.kind == "notice", ChatNotice.style(item.text) == .hidden { continue }
                flush()
                speaker = nil; opened = false
                push(.entry(item), .speaker)
            }
        }
        flush()
        return result
    }
}

/// How a tool call reads in its row: a past-tense verb ("Ran", "Edited"),
/// what it acted on, and any quieter context. The runtime's summaries are
/// written for approval prompts ("Run in /path:\ncommand"), so the row
/// leads with the command instead of the folder every call shares.
struct ChatToolDisplay: Equatable {
    var verb: String
    var subject: String = ""
    /// Commands and search patterns draw monospaced.
    var code = false
    var context: String = ""
    /// The complete command for a shell row, shown above its output.
    var command: String?

    static func describe(tool: String?, summary: String?, live: Bool = false) -> ChatToolDisplay {
        let name = tool ?? "tool"
        let summary = summary ?? ""
        func verb(_ done: String, _ doing: String) -> String { live ? doing : done }
        func after(_ prefix: String) -> String? {
            summary.hasPrefix(prefix) ? String(summary.dropFirst(prefix.count)).trimmingCharacters(in: .whitespacesAndNewlines) : nil
        }
        switch name {
        case "run_shell":
            if summary.hasPrefix("Run in "), let newline = summary.firstIndex(of: "\n") {
                let command = String(summary[summary.index(after: newline)...])
                let lines = command.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
                let first = lines.first ?? ""
                return ChatToolDisplay(verb: verb("Ran", "Running"), subject: lines.count > 1 ? first + " …" : first,
                                       code: true, command: command)
            }
            return fallback(verb("Ran a command", "Running a command"), name, summary)
        case "apply_patch":
            guard summary.hasPrefix("Apply patch:") else { return fallback(verb("Edited files", "Editing files"), name, summary) }
            var kinds = Set<String>(), files: [String] = []
            for line in summary.split(separator: "\n").dropFirst() {
                let parts = line.split(separator: ":", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
                guard parts.count == 2 else { continue }
                kinds.insert(parts[0])
                let target = parts[1].components(separatedBy: " → Move to: ").last ?? parts[1]
                files.append((target as NSString).lastPathComponent)
            }
            let shown = files.prefix(3).joined(separator: ", ") + (files.count > 3 ? " +\(files.count - 3) more" : "")
            if kinds == ["Add"] { return ChatToolDisplay(verb: verb("Created", "Creating"), subject: shown) }
            if kinds == ["Delete"] { return ChatToolDisplay(verb: verb("Deleted", "Deleting"), subject: shown) }
            return ChatToolDisplay(verb: verb("Edited", "Editing"), subject: shown)
        case "read_file":
            if let match = summary.firstMatch(of: #/^Read (.+) \(line (\d+), limit (\d+)\)$/#),
               let line = Int(match.2), let limit = Int(match.3) {
                return ChatToolDisplay(verb: verb("Read", "Reading"), subject: String(match.1),
                                       context: line > 1 ? "lines \(line)–\(line + max(0, limit - 1))" : "")
            }
            return fallback(verb("Read a file", "Reading a file"), name, summary)
        case "search_files":
            if let match = summary.firstMatch(of: #/^Search (.+) for (.+) \(glob (.+)\)$/#) {
                var pattern = String(match.2)
                if pattern.count >= 2, let quote = pattern.first, quote == "'" || quote == "\"", pattern.last == quote {
                    pattern = String(pattern.dropFirst().dropLast())
                }
                let path = String(match.1), glob = String(match.3)
                var context = path == "." ? "" : "in " + path
                if glob != "*" { context += (context.isEmpty ? "" : " · ") + glob }
                return ChatToolDisplay(verb: verb("Searched for", "Searching for"), subject: pattern, code: true, context: context)
            }
            return fallback(verb("Searched files", "Searching files"), name, summary)
        case "web_search":
            return ChatToolDisplay(verb: verb("Searched the web", "Searching the web"), subject: after("Web search:") ?? "")
        case "search_history": return ChatToolDisplay(verb: verb("Searched chat history", "Searching chat history"))
        case "read_history": return ChatToolDisplay(verb: verb("Read chat history", "Reading chat history"))
        case "record_decision": return ChatToolDisplay(verb: verb("Saved a decision note", "Saving a decision note"))
        case "forget_decision": return ChatToolDisplay(verb: verb("Forgot a decision note", "Forgetting a decision note"))
        case "delegate":
            return ChatToolDisplay(verb: verb("Delegated", "Delegating"), subject: after("Delegate:") ?? summary)
        case "team_status":
            return ChatToolDisplay(verb: "Team outcome", subject: after("Team:") ?? summary)
        default:
            return fallback(name, name, summary)
        }
    }

    /// A summary the parser does not recognise (a validation failure, say)
    /// is still shown, never dropped; only a bare repeat of the tool is.
    private static func fallback(_ verb: String, _ name: String, _ summary: String) -> ChatToolDisplay {
        let first = summary.split(separator: "\n").first.map(String.init) ?? ""
        return ChatToolDisplay(verb: verb, subject: first == name || first.hasPrefix(name + " ") ? "" : first)
    }

    /// The glyph a tool draws in its row and in a fold's header.
    static func glyph(_ tool: String?) -> String {
        switch tool {
        case "run_shell": return "run_shell"
        case "apply_patch": return "apply_patch"
        case "search_files": return "search_files"
        case "web_search": return "web_search"
        case "search_history", "read_history", "record_decision", "forget_decision": return "memory"
        case "delegate": return "delegate"
        case "team_status": return "team_status"
        default: return "read_file"
        }
    }

    /// "4 commands, 2 edits, 3 reads": what a folded run did, by kind, in the
    /// order kinds first appear.
    static func activity(_ tools: [String?]) -> String {
        let nouns: [String: (String, String)] = [
            "run_shell": ("command", "commands"), "apply_patch": ("edit", "edits"), "read_file": ("read", "reads"),
            "search_files": ("search", "searches"), "web_search": ("web search", "web searches"),
            "search_history": ("recall", "recalls"), "read_history": ("recall", "recalls"),
            "record_decision": ("note", "notes"), "forget_decision": ("note", "notes"), "team_status": ("outcome", "outcomes")]
        var order: [String] = [], counts: [String: Int] = [:], plural: [String: String] = [:]
        for tool in tools {
            let noun = nouns[tool ?? ""] ?? ("step", "steps")
            if counts[noun.0] == nil { order.append(noun.0); plural[noun.0] = noun.1 }
            counts[noun.0, default: 0] += 1
        }
        return order.map { "\(counts[$0]!) " + (counts[$0]! == 1 ? $0 : plural[$0]!) }.joined(separator: ", ")
    }
}

/// Source links the runtime appends to a reply as a final "Sources:" line.
/// The row draws them as a compact disclosure; the saved and copied text
/// keeps the line exactly as written.
enum ChatReplySources {
    struct Link: Equatable, Identifiable {
        var title: String
        var url: String
        var host: String
        var id: String { url }
    }

    static let marker = "Sources: "

    static func isBlank(_ text: String) -> Bool { text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    /// The reply body and its links. Anything that is not exactly the
    /// runtime's link list stays in the body, so prose that happens to say
    /// "Sources:" is never hidden.
    static func split(_ text: String) -> (body: String, links: [Link]) {
        let range: Range<String.Index>
        if let found = text.range(of: "\n\n" + marker, options: .backwards) { range = found }
        else if text.hasPrefix(marker) { range = text.startIndex..<text.index(text.startIndex, offsetBy: marker.count) }
        else { return (text, []) }
        let list = text[range.upperBound...]
        var links: [Link] = []
        var rest = Substring(list)
        let pattern = #/\[((?:\\.|[^\]\\])*)\]\(<([^<>\s]+)>\)/#
        while !rest.isEmpty {
            guard let match = rest.prefixMatch(of: pattern) else { return (text, []) }
            let url = String(match.2)
            guard let parsed = URL(string: url), let host = parsed.host, ["http", "https"].contains(parsed.scheme ?? "") else { return (text, []) }
            let title = String(match.1).replacingOccurrences(of: #"\\([\\\[\]])"#, with: "$1", options: .regularExpression)
            links.append(Link(title: title, url: url, host: host.hasPrefix("www.") ? String(host.dropFirst(4)) : host))
            rest = rest[match.range.upperBound...]
            if rest.hasPrefix(", ") { rest = rest.dropFirst(2) } else if !rest.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return (text, []) } else { break }
        }
        guard !links.isEmpty else { return (text, []) }
        return (String(text[..<range.lowerBound]), links)
    }
}

/// Transcript notices: most read as a quiet divider; a Team or turn
/// checkpoint becomes a handoff line naming the speaker; a few routine ones
/// are hidden.
enum ChatNotice {
    enum Style: Equatable { case hidden, checkpoint, divider, failure }

    /// Saved by earlier builds on nearly every round of a long turn. The
    /// runtime no longer writes it; older chats still contain it.
    static let contextTrimmed = "Older model context trimmed. The full visible transcript is kept."
    static let checkpointPrefix = "Team contribution checkpoint reached."
    static let turnCheckpointPrefix = "Turn checkpoint reached."
    static let failurePrefix = "Team member stopped after "

    static func style(_ text: String) -> Style {
        if text.hasPrefix(failurePrefix) { return .failure }
        if text == contextTrimmed { return .hidden }
        if text.hasPrefix(checkpointPrefix) || text.hasPrefix(turnCheckpointPrefix) { return .checkpoint }
        return .divider
    }

    static func failureLine(_ text: String, member: String?) -> String {
        guard text.hasPrefix(failurePrefix), let member else { return text }
        return member + " stopped after " + text.dropFirst(failurePrefix.count)
    }

    /// "Sol reached a checkpoint · round budget used · continues after the
    /// other members". Known runtime wording maps to short phrases; anything
    /// else is kept as written so a new outcome is never lost.
    static func checkpointLine(_ text: String, member: String?) -> String {
        let prefix = [checkpointPrefix, turnCheckpointPrefix].first { text.hasPrefix($0) } ?? ""
        let rest = text.dropFirst(prefix.count).trimmingCharacters(in: .whitespaces)
        var parts: [String] = []
        if rest.contains("budget was used up") { parts.append("round budget used") }
        if rest.contains("resume after other members") { parts.append("continues after the other members") }
        else if rest.contains("finished its contribution") { parts.append("contribution finished") }
        else if rest.contains("needs your answer") { parts.append("needs your answer") }
        else if rest.contains("send a message") { parts.append("send a message to continue") }
        else if parts.isEmpty, !rest.isEmpty { parts.append(rest.hasSuffix(".") ? String(rest.dropLast()) : rest) }
        let head = (member.map { $0 + " reached a checkpoint" } ?? "Checkpoint reached")
        return ([head] + parts).joined(separator: " · ")
    }
}
