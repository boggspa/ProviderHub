import Foundation
import SwiftUI

/// A Team member an `@Name` tag can address. The accent is the route's provider
/// colour, kept as hex so the composer and the transcript tint the same chip.
struct CompanionMentionTarget: Identifiable, Equatable {
    var id: String
    var name: String
    var route: String
    var label: String
    var accentHex: String
    /// Identity is the tag the worker checks, compared as UTF-16. A rename
    /// between two encodings of one name must repaint the composer.
    static func == (lhs: Self, rhs: Self) -> Bool {
        CompanionMentions.same(lhs.id, rhs.id) && CompanionMentions.same(lhs.name, rhs.name) && CompanionMentions.same(lhs.route, rhs.route)
    }
}

/// One tag the composer drew, in UTF-16 units of the sent text. Name and route
/// stay as sent, so a later rename never recolours an earlier message.
public struct CompanionMentionChip: Codable, Equatable, Hashable {
    public var id: String
    public var name: String
    public var route: String
    public var start: Int
    public var length: Int
    public init(id: String, name: String, route: String, start: Int, length: Int) {
        self.id = id; self.name = name; self.route = route; self.start = start; self.length = length
    }
}

/// `@Name` tags, matching ChatMentions.swift: the composer's tint, its list,
/// and the chips each send carries. The phone is a resolver of the same rules.
/// The worker never reads the text for tags; it keeps these chips or refuses them.
enum CompanionMentions {
    struct Match: Equatable {
        var target: CompanionMentionTarget
        var range: NSRange
        var wire: CompanionMentionChip {
            CompanionMentionChip(id: target.id, name: target.name, route: target.route, start: range.location, length: range.length)
        }
    }

    /// A long message records its first 64 tags plus each member's first tag.
    static let recorded = 64
    /// Besides space separators: the controls and punctuation chat_team.py
    /// lists, spelled out rather than taken from a Foundation set, whose
    /// whitespace includes U+200B where Python's categories do not.
    private static let openers = Set("\t\n\u{0B}\u{0C}\r\u{85}([{\"\'\u{201C}\u{2018},;*".unicodeScalars)

    static func resolve(_ text: String, members: [CompanionMentionTarget]) -> [Match] {
        let string = text as NSString
        guard string.range(of: "@").location != NSNotFound else { return [] }
        let roster = addressable(members).map { ($0, Array($0.name.utf16)) }.sorted { $0.1.count > $1.1.count }
        let code = codeRanges(string)
        var matches: [Match] = [], seen = Set<String>(), next = 0, c = 0
        while next < string.length {
            let at = string.range(of: "@", options: .literal, range: NSRange(location: next, length: string.length - next)).location
            guard at != NSNotFound else { break }
            next = at + 1
            while c < code.count, NSMaxRange(code[c]) <= at { c += 1 }
            guard at == 0 || opens(scalar(endingAt: at, in: string)), !(c < code.count && code[c].location <= at) else { continue }
            for (member, name) in roster {
                let end = at + 1 + name.count
                guard end <= string.length, reads(string, at: at + 1, name),
                      end == string.length || !continuesName(scalar(startingAt: end, in: string)) else { continue }
                if matches.count < recorded || !seen.contains(member.id) {
                    matches.append(Match(target: member, range: NSRange(location: at, length: name.count + 1)))
                    seen.insert(member.id)
                }
                next = end
                break
            }
        }
        return matches
    }

    /// A sent message as the transcript draws it. A tag is marked only where
    /// the text still reads `@` and the name it was recorded with.
    static func marked(_ text: String, mentions: [CompanionMentionChip], accent: (String) -> Color, size: CGFloat) -> AttributedString {
        var marked = AttributedString(text)
        marked.font = .system(size: size)
        let string = text as NSString
        for mention in mentions {
            let name = Array(mention.name.utf16)
            guard !name.isEmpty, mention.start >= 0, mention.length == name.count + 1, mention.start <= string.length,
                  mention.length <= string.length - mention.start, string.character(at: mention.start) == 0x40,
                  reads(string, at: mention.start + 1, name) else { continue }
            let range = NSRange(location: mention.start, length: mention.length)
            guard let bounds = Range(display(range, in: string), in: text), let span = Range(bounds, in: marked) else { continue }
            let color = accent(mention.route)
            marked[span].foregroundColor = CompanionMentionStyle.ink(color)
            marked[span].backgroundColor = CompanionMentionStyle.fill(color)
            marked[span].font = .system(size: size, weight: .semibold)
        }
        return marked
    }

    /// The user's bubble: a bold speaker label, then the message with its chips.
    static func labelled(title: String, text: String, mentions: [CompanionMentionChip], accent: (String) -> Color, size: CGFloat) -> AttributedString {
        var label = AttributedString(title == "You" ? "User:" : title + ":")
        label.inlinePresentationIntent = .stronglyEmphasized
        label.font = .system(size: size, weight: .bold)
        guard !text.isEmpty else { return label }
        return label + AttributedString(" ") + marked(text, mentions: mentions, accent: accent, size: size)
    }

    static func display(_ range: NSRange, in string: NSString) -> NSRange {
        string.rangeOfComposedCharacterSequences(for: range)
    }

    /// The tag being typed just before the caret. The list closes at a space.
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
    static func candidates(_ query: String, members: [CompanionMentionTarget]) -> [CompanionMentionTarget] {
        let named = addressable(members), key = fold(query)
        guard !key.isEmpty else { return named }
        let leading = named.filter { fold($0.name).hasPrefix(key) }
        return leading + named.filter { !leading.contains($0) && fold($0.name).contains(key) }
    }

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

    static func same(_ a: String, _ b: String) -> Bool { a.utf16.elementsEqual(b.utf16) }

    private static func reads(_ string: NSString, at location: Int, _ name: [UInt16]) -> Bool {
        for (offset, unit) in name.enumerated() where folded(string.character(at: location + offset)) != folded(unit) { return false }
        return true
    }

    private static func folded(_ unit: UInt16) -> UInt16 { (0x41...0x5A).contains(unit) ? unit + 0x20 : unit }

    private static func addressable(_ members: [CompanionMentionTarget]) -> [CompanionMentionTarget] {
        var counts: [[UInt16]: Int] = [:]
        for member in members { counts[member.name.utf16.map(folded), default: 0] += 1 }
        return members.filter { counts[$0.name.utf16.map(folded)] == 1 }
    }

    private static func fold(_ text: String) -> String { text.folding(options: .caseInsensitive, locale: nil) }

    private static func opens(_ scalar: Unicode.Scalar?) -> Bool {
        guard let scalar else { return false }
        switch scalar.properties.generalCategory {
        case .spaceSeparator, .lineSeparator, .paragraphSeparator: return true
        default: return openers.contains(scalar)
        }
    }

    private static func continuesName(_ scalar: Unicode.Scalar?) -> Bool {
        guard let scalar else { return false }
        switch scalar.properties.generalCategory {
        case .uppercaseLetter, .lowercaseLetter, .titlecaseLetter, .modifierLetter, .otherLetter,
             .nonspacingMark, .spacingMark, .enclosingMark, .decimalNumber, .letterNumber, .otherNumber: return true
        default: return scalar == "_"
        }
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

/// The same chip the desktop draws: semibold text in the member's accent,
/// pulled toward the ink, on a wash of that hue.
enum CompanionMentionStyle {
    static func ink(_ accent: Color) -> Color { accent }
    static func fill(_ accent: Color) -> Color { accent.opacity(0.18) }

    static func components(_ hex: String) -> (CGFloat, CGFloat, CGFloat)? {
        guard hex.count == 7, hex.hasPrefix("#"), let value = UInt32(hex.dropFirst(), radix: 16) else { return nil }
        return (CGFloat((value >> 16) & 255) / 255, CGFloat((value >> 8) & 255) / 255, CGFloat(value & 255) / 255)
    }

    static func blend(_ hex: String, toward: CGFloat, fraction: CGFloat) -> (CGFloat, CGFloat, CGFloat) {
        let rgb = components(hex) ?? (0.45, 0.45, 0.45)
        return (rgb.0 + (toward - rgb.0) * fraction, rgb.1 + (toward - rgb.1) * fraction, rgb.2 + (toward - rgb.2) * fraction)
    }
}

/// The `@` list above the composer. The text view keeps the keys and the list
/// takes taps; both act through here.
final class CompanionMentionMenu: ObservableObject {
    struct State: Equatable {
        var start: Int
        var caret: Int
        var candidates: [CompanionMentionTarget]
        var selected: Int
    }
    @Published private(set) var state: State?
    var choose: ((CompanionMentionTarget) -> Void)?
    private var closedAt: Int?

    var current: CompanionMentionTarget? { state.map { $0.candidates[$0.selected] } }

    func update(text: String, caret: Int?, members: [CompanionMentionTarget]) {
        guard !members.isEmpty, let caret, let typing = CompanionMentions.typing(text, caret: caret) else {
            closedAt = nil; publish(nil); return
        }
        guard typing.start != closedAt else { publish(nil); return }
        closedAt = nil
        let candidates = CompanionMentions.candidates(typing.query, members: members)
        guard !candidates.isEmpty else { publish(nil); return }
        let kept = state.flatMap { old in candidates.firstIndex { $0.id == old.candidates[old.selected].id } } ?? 0
        publish(State(start: typing.start, caret: caret, candidates: candidates, selected: kept))
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

/// The Team members `@` can address, floated above the composer while a tag is typed.
struct CompanionMentionList: View {
    @ObservedObject var menu: CompanionMentionMenu
    var models: [RemoteValue]
    var accent: Color

    var body: some View {
        if let state = menu.state {
            VStack(alignment: .leading, spacing: 1) {
                ForEach(Array(state.candidates.enumerated()), id: \.element.id) { index, target in
                    row(target, index: index, selected: index == state.selected)
                }
            }
            .padding(4)
            .frame(width: 270, alignment: .leading)
            .background {
                RoundedRectangle(cornerRadius: 10, style: .continuous).fill(CompanionTheme.surface)
                    .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(CompanionTheme.cardFill))
            }
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(accent.opacity(0.22), lineWidth: 0.5))
            .shadow(color: .black.opacity(0.18), radius: 14, y: 4)
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Team members to address")
        }
    }

    private func row(_ target: CompanionMentionTarget, index: Int, selected: Bool) -> some View {
        let brand = CompanionBranding.presentation(route: target.route, label: target.label, models: models)
        return Button { menu.choose?(target) } label: {
            HStack(spacing: 8) {
                CompanionProviderMark(presentation: brand, size: 15)
                Text(target.name).font(.system(size: 12.5, weight: .semibold)).foregroundStyle(CompanionTheme.ink)
                Text(brand.label.isEmpty ? target.route : brand.label).font(.system(size: 11.5)).foregroundStyle(CompanionTheme.secondary)
                Spacer(minLength: 8)
                if selected {
                    Image(systemName: "return").font(.system(size: 9.5, weight: .semibold)).foregroundStyle(CompanionTheme.secondary)
                }
            }
            .lineLimit(1)
            .padding(.horizontal, 8).frame(height: 28)
            .background(RoundedRectangle(cornerRadius: 7, style: .continuous)
                .fill(selected ? CompanionMentionStyle.fill(brand.accent) : .clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { if $0 { menu.select(index) } }
        .accessibilityLabel("Address " + target.name + ", " + (brand.label.isEmpty ? target.route : brand.label))
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

struct CompanionMentionText: View {
    var text: String
    var mentions: [CompanionMentionChip]
    var models: [RemoteValue]
    var size: CGFloat

    var body: some View {
        Text(CompanionMentions.labelled(title: "You", text: text, mentions: mentions, accent: accent, size: size))
            .textSelection(.enabled)
    }

    private func accent(_ route: String) -> Color {
        CompanionBranding.presentation(route: route, models: models).accent
    }
}

extension CompanionModel {
    /// Members an `@Name` tag can address. Older Mac hosts ignore chips, so the
    /// list stays closed until the host advertises mention routing.
    var mentionsAvailable: Bool { (store.controls["extensionVersion"]?.int ?? 0) >= 4 }
    var mentionTargets: [CompanionMentionTarget] {
        guard mentionsAvailable, team["enabled"].flag else { return [] }
        return members.compactMap { row in
            let id = row["id"].text, name = row["name"].text
            guard !id.isEmpty, !name.isEmpty else { return nil }
            let route = row["route"].text
            let brand = CompanionBranding.presentation(route: route, label: row["label"].text, models: models)
            return CompanionMentionTarget(id: id, name: name, route: route, label: brand.label, accentHex: brand.accentHex)
        }
    }

    func mentionChips(in text: String) -> [CompanionMentionChip] {
        guard mentionsAvailable else { return [] }
        return CompanionMentions.resolve(text, members: mentionTargets).map(\.wire)
    }
}
