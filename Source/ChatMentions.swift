import AppKit
import SwiftUI

/// A Team member an `@Name` tag can address, with the accent its tag wears.
struct ChatMentionTarget: Identifiable, Equatable {
    var id: String
    var name: String
    var route: String
    var accent: NSColor = .secondaryLabelColor
    /// The accent follows the route, so identity is enough.
    static func == (lhs: Self, rhs: Self) -> Bool { lhs.id == rhs.id && lhs.name == rhs.name && lhs.route == rhs.route }
}

/// One tag the worker resolved and routed, in UTF-16 units of the sent text.
/// Name and route are kept as sent, so a later rename or model change never
/// recolours an earlier message. A malformed record decodes as one that marks
/// nothing, so it can never cost the transcript its other entries.
struct ChatMention: Decodable, Equatable {
    var id = "", name = "", route = ""
    var start = -1, length = 0

    init(id: String, name: String, route: String, start: Int, length: Int) {
        self.id = id; self.name = name; self.route = route; self.start = start; self.length = length
    }

    private enum CodingKeys: String, CodingKey { case id, name, route, start, length }
    init(from decoder: Decoder) throws {
        guard let row = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        id = (try? row.decode(String.self, forKey: .id)) ?? ""
        name = (try? row.decode(String.self, forKey: .name)) ?? ""
        route = (try? row.decode(String.self, forKey: .route)) ?? ""
        start = (try? row.decode(Int.self, forKey: .start)) ?? -1
        length = (try? row.decode(Int.self, forKey: .length)) ?? 0
    }
}

/// A provider accent as plain sRGB components, so an attributed string that
/// carries one compares equal across renders and the measured size is reused.
struct ChatMentionTint: Hashable {
    var red: Double, green: Double, blue: Double
    init(_ color: NSColor) {
        let rgb = color.usingColorSpace(.sRGB) ?? .secondaryLabelColor.usingColorSpace(.sRGB) ?? .gray
        red = Double(rgb.redComponent); green = Double(rgb.greenComponent); blue = Double(rgb.blueComponent)
    }
    var color: NSColor { NSColor(srgbRed: red, green: green, blue: blue, alpha: 1) }
}

/// Marks a recorded `@Name` tag in transcript text with its member's accent.
enum ChatMentionAttribute: AttributedStringKey {
    typealias Value = ChatMentionTint
    static let name = "ProviderHub.ChatMention"
}

/// `@Name` tags in a Team message, for the composer's tint and its list.
///
/// chat_team.py's resolver is the authority: it routes the message and records
/// the ranges the transcript draws. The composer sends the members it tinted
/// and the worker refuses a message that would reach anyone else, so a tinted
/// tag is always the member the message reaches. Both follow the same rules.
/// A tag starts the text or follows whitespace, an opening bracket or quote, a
/// comma, semicolon or asterisk, so an email address or a URL path never
/// addresses anyone. It names a member whole and case-insensitively, longest
/// name first, and ends before a letter, digit, mark or underscore. A name two
/// members share, and anything in a fenced block or a one-line backtick span,
/// addresses nobody. Offsets are UTF-16, as NSString and the worker count them.
enum ChatMentions {
    struct Match: Equatable {
        var target: ChatMentionTarget
        var range: NSRange
    }

    /// A long message records its first 64 tags plus each member's first tag.
    static let recorded = 64
    private static let openers = CharacterSet(charactersIn: "\t([{\"'\u{201C}\u{2018},;*")

    static func resolve(_ text: String, members: [ChatMentionTarget]) -> [Match] {
        let string = text as NSString
        guard string.range(of: "@").location != NSNotFound else { return [] }
        let roster = addressable(members).sorted { ($0.name as NSString).length > ($1.name as NSString).length }
        let code = codeRanges(string)
        var matches: [Match] = [], seen = Set<String>(), next = 0, c = 0
        while next < string.length {
            let at = string.range(of: "@", options: .literal, range: NSRange(location: next, length: string.length - next)).location
            guard at != NSNotFound else { break }
            next = at + 1
            while c < code.count, NSMaxRange(code[c]) <= at { c += 1 }
            guard at == 0 || opens(scalar(endingAt: at, in: string)), !(c < code.count && code[c].location <= at) else { continue }
            for member in roster {
                let length = (member.name as NSString).length, end = at + 1 + length
                guard end <= string.length,
                      fold(string.substring(with: NSRange(location: at + 1, length: length))) == fold(member.name),
                      end == string.length || !continuesName(scalar(startingAt: end, in: string)) else { continue }
                if matches.count < recorded || !seen.contains(member.id) {
                    matches.append(Match(target: member, range: NSRange(location: at, length: length + 1)))
                    seen.insert(member.id)
                }
                next = end
                break
            }
        }
        return matches
    }

    /// Members the matches address, first tag first; empty means the whole Team.
    static func addressees(_ matches: [Match]) -> [String] {
        var ids: [String] = []
        for match in matches where !ids.contains(match.target.id) { ids.append(match.target.id) }
        return ids
    }

    /// A sent message as the transcript draws it, each recorded tag marked
    /// with its member's accent. A tag is marked only where the text still
    /// reads `@` and the name it was recorded with.
    static func marked(_ text: String, mentions: [ChatMention], accent: (String) -> Color) -> AttributedString {
        var marked = AttributedString(text)
        let string = text as NSString
        for mention in mentions {
            let range = NSRange(location: mention.start, length: mention.length)
            guard mention.start >= 0, mention.length > 1, NSMaxRange(range) <= string.length,
                  fold(string.substring(with: range)) == fold("@" + mention.name),
                  let bounds = Range(range, in: text), let span = Range(bounds, in: marked) else { continue }
            marked[span][ChatMentionAttribute.self] = ChatMentionTint(NSColor(accent(mention.route)))
        }
        return marked
    }

    /// The tag being typed just before the caret: the `@` offset and the
    /// partial name after it, while no whitespace has been typed, the `@`
    /// follows an opener and it is not in code. The list closes at a space.
    static func typing(_ text: String, caret: Int) -> (start: Int, query: String)? {
        let string = text as NSString
        guard caret > 0, caret <= string.length else { return nil }
        var index = caret
        while index > 0, caret - index <= 60 {
            let unit = string.character(at: index - 1)
            if unit == 0x40 {
                let at = index - 1
                guard at == 0 || opens(scalar(endingAt: at, in: string)),
                      !codeRanges(string).contains(where: { NSLocationInRange(at, $0) }) else { return nil }
                return (at, string.substring(with: NSRange(location: index, length: caret - index)))
            }
            if let scalar = Unicode.Scalar(unit), CharacterSet.whitespacesAndNewlines.contains(scalar) { return nil }
            index -= 1
        }
        return nil
    }

    /// Members whose names start with the query, then those containing it.
    static func candidates(_ query: String, members: [ChatMentionTarget]) -> [ChatMentionTarget] {
        let named = addressable(members), key = fold(query)
        guard !key.isEmpty else { return named }
        let leading = named.filter { fold($0.name).hasPrefix(key) }
        return leading + named.filter { !leading.contains($0) && fold($0.name).contains(key) }
    }

    /// Fenced blocks (``` or ~~~) and backtick spans on one line, in order.
    static func codeRanges(_ string: NSString) -> [NSRange] {
        var ranges: [NSRange] = [], fence: (mark: unichar, run: Int, start: Int)?, start = 0
        while true {
            let newline = string.range(of: "\n", options: .literal, range: NSRange(location: start, length: string.length - start)).location
            let end = newline == NSNotFound ? string.length : newline
            let lineEnd = newline == NSNotFound ? string.length : newline + 1
            let mark = fenceMark(string, from: start, to: end)
            if let open = fence {
                if let mark, mark.mark == open.mark, mark.run >= open.run {
                    ranges.append(NSRange(location: open.start, length: lineEnd - open.start)); fence = nil
                }
            } else if let mark {
                fence = (mark.mark, mark.run, start)
            } else {
                ranges += inlineCode(string, from: start, to: end)
            }
            if newline == NSNotFound { break }
            start = lineEnd
        }
        if let open = fence { ranges.append(NSRange(location: open.start, length: string.length - open.start)) }
        return ranges
    }

    private static func fenceMark(_ string: NSString, from start: Int, to end: Int) -> (mark: unichar, run: Int)? {
        var index = start
        while index < end, string.character(at: index) == 0x20 { index += 1 }
        guard index < end, [0x60, 0x7E].contains(string.character(at: index)) else { return nil }
        let mark = string.character(at: index)
        var run = index
        while run < end, string.character(at: run) == mark { run += 1 }
        return run - index >= 3 ? (mark, run - index) : nil
    }

    private static func inlineCode(_ string: NSString, from start: Int, to end: Int) -> [NSRange] {
        var runs: [(at: Int, length: Int)] = [], index = start
        while index < end {
            guard string.character(at: index) == 0x60 else { index += 1; continue }
            var run = index
            while run < end, string.character(at: run) == 0x60 { run += 1 }
            runs.append((index, run - index)); index = run
        }
        var spans: [NSRange] = [], k = 0
        while k < runs.count {
            guard let close = runs.indices.dropFirst(k + 1).first(where: { runs[$0].length == runs[k].length }) else { k += 1; continue }
            spans.append(NSRange(location: runs[k].at, length: runs[close].at + runs[close].length - runs[k].at)); k = close + 1
        }
        return spans
    }

    /// Members whose names no other member shares, in roster order.
    private static func addressable(_ members: [ChatMentionTarget]) -> [ChatMentionTarget] {
        var counts: [String: Int] = [:]
        for member in members { counts[fold(member.name), default: 0] += 1 }
        return members.filter { counts[fold($0.name)] == 1 }
    }

    private static func fold(_ text: String) -> String { text.folding(options: .caseInsensitive, locale: nil) }

    private static func opens(_ scalar: Unicode.Scalar?) -> Bool {
        guard let scalar else { return false }
        return CharacterSet.whitespacesAndNewlines.contains(scalar) || openers.contains(scalar)
    }

    private static func continuesName(_ scalar: Unicode.Scalar?) -> Bool {
        guard let scalar else { return false }
        return CharacterSet.alphanumerics.contains(scalar) || scalar == "_"
    }

    private static func scalar(endingAt end: Int, in string: NSString) -> Unicode.Scalar? {
        let unit = string.character(at: end - 1)
        if UTF16.isTrailSurrogate(unit), end >= 2, UTF16.isLeadSurrogate(string.character(at: end - 2)) {
            return combined(string.character(at: end - 2), unit)
        }
        return Unicode.Scalar(unit)
    }

    private static func scalar(startingAt start: Int, in string: NSString) -> Unicode.Scalar? {
        let unit = string.character(at: start)
        if UTF16.isLeadSurrogate(unit), start + 1 < string.length, UTF16.isTrailSurrogate(string.character(at: start + 1)) {
            return combined(unit, string.character(at: start + 1))
        }
        return Unicode.Scalar(unit)
    }

    private static func combined(_ lead: unichar, _ trail: unichar) -> Unicode.Scalar? {
        Unicode.Scalar(0x10000 + ((UInt32(lead) - 0xD800) << 10) + (UInt32(trail) - 0xDC00))
    }
}

/// The `@` list above the composer. The text view keeps the keys (↑ ↓ move,
/// Return or Tab choose, Escape closes) and the list takes clicks; both act
/// through here, so neither has to reach into the other.
final class ChatMentionMenu: ObservableObject {
    struct State: Equatable {
        var start: Int
        var candidates: [ChatMentionTarget]
        var selected: Int
    }
    @Published private(set) var state: State?
    /// Inserts a member at the typed tag; the text view sets it.
    var choose: ((ChatMentionTarget) -> Void)?
    /// The tag the user closed or completed stays closed while the caret is in it.
    private var closedAt: Int?

    var current: ChatMentionTarget? { state.map { $0.candidates[$0.selected] } }

    func update(text: String, caret: Int?, members: [ChatMentionTarget]) {
        guard !members.isEmpty, let caret, let typing = ChatMentions.typing(text, caret: caret) else {
            closedAt = nil; publish(nil); return
        }
        guard typing.start != closedAt else { publish(nil); return }
        closedAt = nil
        let candidates = ChatMentions.candidates(typing.query, members: members)
        guard !candidates.isEmpty else { publish(nil); return }
        let kept = state.flatMap { old in candidates.firstIndex { $0.id == old.candidates[old.selected].id } } ?? 0
        publish(State(start: typing.start, candidates: candidates, selected: kept))
    }

    func move(_ step: Int) {
        guard var next = state else { return }
        next.selected = (next.selected + step + next.candidates.count) % next.candidates.count
        publish(next)
    }

    func select(_ index: Int) {
        guard var next = state, next.candidates.indices.contains(index) else { return }
        next.selected = index; publish(next)
    }

    func close() {
        closedAt = state?.start ?? closedAt
        publish(nil)
    }

    private func publish(_ next: State?) { if next != state { state = next } }
}
