import AppKit
import SwiftUI

// A deliberately small block layer over the transcript's existing inline
// Markdown. No HTML, WebView, external renderer, or changes to saved text.
enum ChatTableAlignment: Equatable {
    case leading, center, trailing
    var frame: Alignment { self == .trailing ? .topTrailing : self == .center ? .top : .topLeading }
    var text: TextAlignment { self == .trailing ? .trailing : self == .center ? .center : .leading }
}

struct ChatMarkdownTable: Equatable {
    var header: [String]
    var rows: [[String]]
    var alignments: [ChatTableAlignment]
    var source: String
}

/// A fenced code block. `body` drops the fences and the fence's own
/// indentation; `source` keeps every original character for copy.
struct ChatMarkdownCode: Equatable {
    var language: String
    var body: String
    var indent: Int
    var closed: Bool
    var source: String
}

struct ChatTextBlock: Equatable, Identifiable {
    enum Content: Equatable { case text(String), table(ChatMarkdownTable), code(ChatMarkdownCode) }
    let id: Int                         // source line, stable while appending
    var content: Content
    var source: String {
        switch content {
        case .text(let value): return value
        case .table(let table): return table.source
        case .code(let code): return code.source
        }
    }
}

enum ChatTableParser {
    static let maxMessageBytes = 256 * 1024
    static let maxTableBytes = 64 * 1024
    static let maxColumns = 12
    static let maxRows = 128
    static let maxCells = 2048
    static let maxTables = 16

    private struct Line {
        let text: String
        let source: String
        let terminated: Bool
    }

    /// Complete header + delimiter lines opt into a table. The unfinished
    /// streaming line remains literal below it until the next newline (or
    /// turn completion). Earlier rows never disappear for a partial cell.
    /// A fence opens a code block once its opening line is complete; while
    /// streaming, an unclosed block grows and hides a half-typed closing fence.
    static func parse(_ source: String, streaming: Bool) -> [ChatTextBlock] {
        guard source.utf8.count <= maxMessageBytes, source.contains("|") || source.contains("```") || source.contains("~~~") else {
            return [ChatTextBlock(id: 0, content: .text(source))]
        }
        let pieces = source.components(separatedBy: "\n")
        let lines = pieces.enumerated().map { index, piece in
            Line(text: piece.hasSuffix("\r") ? String(piece.dropLast()) : piece,
                 source: piece + (index < pieces.count - 1 ? "\n" : ""), terminated: index < pieces.count - 1)
        }
        var blocks: [ChatTextBlock] = []
        var index = 0, textStart = 0, cells = 0, tables = 0
        var text = ""
        var code: (start: Int, fence: Character, length: Int, indent: Int, language: String, body: [Line], raw: String)?
        func flushText() {
            if !text.isEmpty { blocks.append(ChatTextBlock(id: textStart, content: .text(text))); text = "" }
        }
        func flushCode(closed: Bool) {
            guard let open = code else { return }
            var body = open.body
            if !closed, let last = body.last, !last.terminated,
               last.text.isEmpty || (streaming && last.text.trimmingCharacters(in: .whitespaces).allSatisfy { $0 == open.fence }) {
                body.removeLast()
            }
            let lines = body.map { line in String(line.text.dropFirst(min(open.indent, line.text.prefix { $0 == " " }.count))) }
            blocks.append(ChatTextBlock(id: open.start, content: .code(ChatMarkdownCode(
                language: open.language, body: lines.joined(separator: "\n"), indent: open.indent, closed: closed, source: open.raw))))
            code = nil
        }
        func appendText(_ line: Line, at index: Int) {
            if text.isEmpty { textStart = index }
            text += line.source
        }
        while index < lines.count {
            let line = lines[index]
            if let open = code {
                code?.raw += line.source
                if let end = fenceMarker(line.text), end.0 == open.fence, end.1 >= open.length, end.2.isEmpty {
                    flushCode(closed: true)
                } else {
                    code?.body.append(line)
                }
                index += 1
                continue
            }
            // A backtick info string cannot hold backticks: ```x``` is inline code.
            if let start = fenceMarker(line.text), start.0 == "~" || !start.2.contains("`"), !streaming || line.terminated {
                flushText()
                code = (index, start.0, start.1, line.text.prefix { $0 == " " }.count,
                        String(start.2.split(whereSeparator: { $0 == " " || $0 == "\t" }).first ?? ""), [], line.source)
                index += 1
                continue
            }
            guard tables < maxTables, index + 1 < lines.count,
                  !streaming || (line.terminated && lines[index + 1].terminated),
                  let header = row(line.text), header.count <= maxColumns,
                  let separators = row(lines[index + 1].text), separators.count == header.count,
                  let alignments = delimiter(separators) else {
                appendText(line, at: index); index += 1
                continue
            }
            var end = index + 2
            var rows: [[String]] = []
            var raw = line.source + lines[index + 1].source
            var count = 0
            while end < lines.count, !streaming || lines[end].terminated,
                  let columns = row(lines[end].text), columns.count <= header.count {
                count += 1
                if count <= maxRows { rows.append(columns + Array(repeating: "", count: header.count - columns.count)) }
                raw += lines[end].source
                end += 1
            }
            let cost = (count + 1) * header.count
            if count > maxRows || cells + cost > maxCells || raw.utf8.count > maxTableBytes {
                // Never truncate data or build thousands of native cells.
                // Oversized tables retain their complete original text.
                if text.isEmpty { textStart = index }
                text += raw
            } else {
                flushText()
                blocks.append(ChatTextBlock(id: index, content: .table(ChatMarkdownTable(
                    header: header, rows: rows, alignments: alignments, source: raw))))
                tables += 1; cells += cost
            }
            index = end
        }
        flushText()
        flushCode(closed: false)
        return blocks.isEmpty ? [ChatTextBlock(id: 0, content: .text(source))] : blocks
    }

    private static func unindented(_ line: String) -> Substring? {
        // Four-space indentation is code; quoted/nested block constructs are
        // intentionally outside this small table grammar.
        let spaces = line.prefix(while: { $0 == " " }).count
        guard spaces <= 3, !line.hasPrefix("\t") else { return nil }
        let value = line.dropFirst(spaces)
        guard !value.hasPrefix(">"), !value.hasPrefix("\t") else { return nil }
        return value
    }

    private static func fenceMarker(_ line: String) -> (Character, Int, String)? {
        guard let value = unindented(line), let first = value.first, first == "`" || first == "~" else { return nil }
        let count = value.prefix(while: { $0 == first }).count
        guard count >= 3 else { return nil }
        return (first, count, value.dropFirst(count).trimmingCharacters(in: .whitespaces))
    }

    /// Unescaped pipes outside matching backtick runs split columns. Keep
    /// cell Markdown intact for the same inline renderer used by prose.
    private static func row(_ line: String) -> [String]? {
        guard let value = unindented(line) else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespaces)
        let characters = Array(trimmed)
        var parts: [String] = [], part = "", codeRun: Int?
        var index = 0, pipe = false, lastWasPipe = false
        while index < characters.count {
            let character = characters[index]
            if character == "\\", index + 1 < characters.count {
                part.append(character); part.append(characters[index + 1]); index += 2; lastWasPipe = false
            } else if character == "`" {
                var end = index + 1
                while end < characters.count, characters[end] == "`" { end += 1 }
                let count = end - index
                if codeRun == count { codeRun = nil } else if codeRun == nil { codeRun = count }
                part += String(repeating: "`", count: count); index = end; lastWasPipe = false
            } else if character == "|", codeRun == nil {
                parts.append(part.trimmingCharacters(in: .whitespaces)); part = ""
                pipe = true; lastWasPipe = true; index += 1
            } else {
                part.append(character); index += 1; lastWasPipe = false
            }
        }
        guard pipe, codeRun == nil else { return nil }
        parts.append(part.trimmingCharacters(in: .whitespaces))
        if trimmed.hasPrefix("|") { parts.removeFirst() }
        if lastWasPipe { parts.removeLast() }
        return parts.isEmpty ? nil : parts
    }

    private static func delimiter(_ cells: [String]) -> [ChatTableAlignment]? {
        var result: [ChatTableAlignment] = []
        for cell in cells {
            var dashes = cell[...]
            let leading = dashes.first == ":", trailing = dashes.last == ":"
            if leading { dashes = dashes.dropFirst() }
            if trailing, !dashes.isEmpty { dashes = dashes.dropLast() }
            guard dashes.count >= 3, dashes.allSatisfy({ $0 == "-" }) else { return nil }
            result.append(leading && trailing ? .center : trailing ? .trailing : .leading)
        }
        return result
    }
}

struct ChatRenderedCell {
    let source: String
    let formatted: AttributedString
    var plain: String { String(formatted.characters) }
}

struct ChatRenderedTable {
    let source: ChatMarkdownTable
    let cells: [[ChatRenderedCell]]
    /// Headers and a small row sample guide column proportions; long values
    /// wrap instead of measuring every character on each layout pass.
    var columnHints: [CGFloat] {
        source.header.indices.map { column in
            CGFloat(cells.prefix(9).map { $0[column].formatted.characters.prefix(64).count }.max() ?? 8)
        }
    }
    var tsv: String {
        cells.map { row in
            row.map { $0.plain.replacingOccurrences(of: "\t", with: " ")
                .replacingOccurrences(of: "\n", with: " ").replacingOccurrences(of: "\r", with: " ") }.joined(separator: "\t")
        }.joined(separator: "\n")
    }
}

struct ChatRenderedBlock: Identifiable {
    enum Content { case text(AttributedString), table(ChatRenderedTable), code(ChatMarkdownCode) }
    let parsed: ChatTextBlock
    let content: Content
    var id: Int { parsed.id }
}

/// Owned by one lazy transcript row, never a global cache of chat history.
/// Parent updates don't reparse old replies. During streaming, unchanged
/// blocks and cells retain their inline formatting; only changed text parses.
final class ChatTextPresentation: ObservableObject {
    @Published private(set) var blocks: [ChatRenderedBlock] = []
    private var source: String?
    private var wasStreaming = false

    init(text: String, streaming: Bool) { update(text: text, streaming: streaming) }

    func update(text: String, streaming: Bool) {
        guard text != source || streaming != wasStreaming else { return }
        source = text; wasStreaming = streaming
        let old = Dictionary(uniqueKeysWithValues: blocks.map { ($0.id, $0) })
        let previouslyHadBlocks = blocks.contains { if case .text = $0.content { return false }; return true }
        let parsed = ChatTableParser.parse(text, streaming: streaming)
        let hasBlocks = parsed.contains { if case .text = $0.content { return false }; return true }
        blocks = parsed.compactMap { block in
            switch block.content {
            case .text(let raw):
                // VStack supplies spacing around tables and code. Preserve
                // whitespace in source/copy, but don't add boundary newlines twice.
                let displayed = hasBlocks ? raw.trimmingCharacters(in: .newlines) : raw
                guard !displayed.isEmpty else { return nil }
                if let prior = old[block.id], prior.parsed == block, previouslyHadBlocks == hasBlocks { return prior }
                return ChatRenderedBlock(parsed: block, content: .text(ChatMarkdownBlocks.format(displayed)))
            case .code(let code):
                if let prior = old[block.id], prior.parsed == block { return prior }
                return ChatRenderedBlock(parsed: block, content: .code(code))
            case .table(let table):
                if let prior = old[block.id], prior.parsed == block { return prior }
                var oldCells: [[ChatRenderedCell]] = []
                if case .table(let previous) = old[block.id]?.content { oldCells = previous.cells }
                let cells = ([table.header] + table.rows).enumerated().map { row, values in
                    values.enumerated().map { column, value -> ChatRenderedCell in
                        if row < oldCells.count, column < oldCells[row].count, oldCells[row][column].source == value {
                            return oldCells[row][column]
                        }
                        return ChatRenderedCell(source: value, formatted: ChatMarkdownBlocks.inline(value))
                    }
                }
                return ChatRenderedBlock(parsed: block, content: .table(ChatRenderedTable(source: table, cells: cells)))
            }
        }
    }
}

/// How one displayed line of prose is laid out. Indents are in ems of the
/// reading font, so they follow the text size and reading zoom.
struct ChatBlockStyle: Hashable {
    enum Kind: Hashable { case heading(Int), item, continuation, quote, rule }
    var kind: Kind
    /// The first line's indent, a list marker's trailing edge, and the
    /// column that item content and its wrapped lines hang from.
    var first = 0.0, marker = 0.0, content = 0.0
}

enum ChatBlockStyleAttribute: AttributedStringKey {
    typealias Value = ChatBlockStyle
    static let name = "ProviderHub.ChatBlockStyle"
}

/// Block Markdown inside one prose block: ATX headings, bullet and numbered
/// lists with their nesting, quotes and rules. Each source line stays one
/// displayed paragraph. Markers are stripped before the inline parse, so
/// emphasis and code inside an item still work, and drawn as `\tmarker\t`
/// against tab stops so wrapped lines hang under the item's text. Authors'
/// numbers are kept as written.
enum ChatMarkdownBlocks {
    static let orderedWidth = 1.9, bulletWidth = 1.4, markerGap = 0.45

    private struct Level { let indent: Int; let content: Double; let bullet: Bool }
    private struct Line { var style: ChatBlockStyle?; var marker: String?; var content: String }

    static func format(_ text: String) -> AttributedString {
        guard text.utf8.count <= ChatTableParser.maxMessageBytes else { return AttributedString(text) }
        var stack: [Level] = []
        let lines = text.components(separatedBy: "\n").map { classify($0.hasSuffix("\r") ? String($0.dropLast()) : $0, &stack) }
        guard lines.contains(where: { $0.style != nil }) else { return inline(text) }
        // One inline parse keeps spans that cross lines; if one does, the
        // line count no longer matches and each line parses alone.
        let contents = lines.map(\.content)
        var parsed = split(inline(contents.joined(separator: "\n")))
        if parsed.count != lines.count { parsed = contents.map(inline) }
        var result = AttributedString()
        for (index, line) in lines.enumerated() {
            var piece = AttributedString(line.marker.map { "\t\($0)\t" } ?? "")
            piece.append(parsed[index])
            if index < lines.count - 1 { piece.append(AttributedString("\n")) }
            if let style = line.style { piece[ChatBlockStyleAttribute.self] = style }
            result.append(piece)
        }
        return result
    }

    static func inline(_ text: String) -> AttributedString {
        guard text.utf8.count <= ChatTableParser.maxMessageBytes else { return AttributedString(text) }
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }

    private static func split(_ text: AttributedString) -> [AttributedString] {
        var pieces: [AttributedString] = []
        var start = text.startIndex, index = text.startIndex
        while index < text.endIndex {
            let next = text.characters.index(after: index)
            if text.characters[index] == "\n" { pieces.append(AttributedString(text[start..<index])); start = next }
            index = next
        }
        pieces.append(AttributedString(text[start..<text.endIndex]))
        return pieces
    }

    /// Blank lines keep a list open; an unindented paragraph closes it.
    /// Nesting follows leading indentation, a tab counting to the next four.
    private static func classify(_ line: String, _ stack: inout [Level]) -> Line {
        var indent = 0
        for character in line {
            if character == " " { indent += 1 } else if character == "\t" { indent += 4 - indent % 4 } else { break }
        }
        let value = line.drop { $0 == " " || $0 == "\t" }
        guard !value.isEmpty else { return Line(content: line) }
        if indent <= 3 {
            let hashes = value.prefix { $0 == "#" }.count
            if (1...6).contains(hashes), value.dropFirst(hashes).first.map({ $0 == " " || $0 == "\t" }) ?? true {
                stack = []
                var title = value.dropFirst(hashes).trimmingCharacters(in: .whitespaces)
                if title.allSatisfy({ $0 == "#" }) { title = "" }
                else if let close = title.range(of: #"\s+#+$"#, options: .regularExpression) { title.removeSubrange(close) }
                return Line(style: ChatBlockStyle(kind: .heading(hashes)), content: title)
            }
            let compact = value.filter { $0 != " " && $0 != "\t" }
            if compact.count >= 3, let mark = compact.first, "-*_".contains(mark), compact.allSatisfy({ $0 == mark }) {
                stack = []
                return Line(style: ChatBlockStyle(kind: .rule), content: String(value))
            }
            if value.first == ">" {
                stack = []
                var quoted = value.dropFirst()
                if quoted.first == " " { quoted = quoted.dropFirst() }
                return Line(style: ChatBlockStyle(kind: .quote), content: String(quoted))
            }
        }
        if let (marker, ordered, rest) = listMarker(value) {
            while let last = stack.last, last.indent >= indent { stack.removeLast() }
            let parent = stack.last?.content ?? 0
            let width = ordered ? orderedWidth : bulletWidth
            // System fonts draw ◦ filled at text size; a dash reads as nested.
            let symbol = ordered ? marker : stack.contains { $0.bullet } ? "–" : "•"
            stack.append(Level(indent: indent, content: parent + width, bullet: !ordered))
            return Line(style: ChatBlockStyle(kind: .item, first: parent, marker: parent + width - markerGap, content: parent + width),
                        marker: symbol, content: rest)
        }
        if indent > 0 {
            while let last = stack.last, last.indent >= indent { stack.removeLast() }
            if let owner = stack.last {
                return Line(style: ChatBlockStyle(kind: .continuation, first: owner.content, content: owner.content), content: String(value))
            }
        } else {
            stack = []
        }
        return Line(content: line)
    }

    private static func listMarker(_ value: Substring) -> (String, Bool, String)? {
        func spaced(_ rest: Substring) -> String? {
            guard let first = rest.first, first == " " || first == "\t" else { return nil }
            return String(rest.drop { $0 == " " || $0 == "\t" })
        }
        if let first = value.first, "-*+".contains(first) {
            return spaced(value.dropFirst()).map { (String(first), false, $0) }
        }
        let digits = value.prefix { $0.isASCII && $0.isNumber }
        guard (1...9).contains(digits.count) else { return nil }
        let after = value.dropFirst(digits.count)
        guard let delimiter = after.first, delimiter == "." || delimiter == ")" else { return nil }
        return spaced(after.dropFirst()).map { (digits + String(delimiter), true, $0) }
    }
}

struct ChatTranscriptText: View {
    let text: String
    let streaming: Bool
    /// The whole entry, for the text's **Copy Message** menu item.
    var message: String?
    @StateObject private var presentation: ChatTextPresentation
    @Environment(\.chatTextStyle) private var textStyle

    init(text: String, streaming: Bool, message: String? = nil) {
        self.text = text; self.streaming = streaming; self.message = message
        _presentation = StateObject(wrappedValue: ChatTextPresentation(text: text, streaming: streaming))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(presentation.blocks) { block in
                switch block.content {
                case .text(let content):
                    ChatSelectableText(text: content, font: textStyle.nsFont, color: HubTheme.Semantic.nsInk, message: message)
                        .frame(maxWidth: .infinity, alignment: .leading)
                case .table(let table): ChatTranscriptTable(table: table)
                case .code(let code): ChatTranscriptCode(code: code, message: message)
                }
            }
        }
        .onChange(of: text) { _, value in presentation.update(text: value, streaming: streaming) }
        .onChange(of: streaming) { _, value in presentation.update(text: text, streaming: value) }
    }
}

/// A fenced code block: a contained, selectable monospaced box whose long
/// lines scroll sideways instead of wrapping. The language sits quietly at
/// the top; Copy appears on hover and copies the code without its fences.
struct ChatTranscriptCode: View {
    let code: ChatMarkdownCode
    var message: String?
    @Environment(\.chatTextStyle) private var textStyle
    @State private var hovering = false
    @State private var copied = false

    var body: some View {
        let size = textStyle.nsFont.pointSize
        let shape = RoundedRectangle(cornerRadius: textStyle.scaled(7))
        VStack(alignment: .leading, spacing: 0) {
            if !code.language.isEmpty {
                HStack(spacing: 6) {
                    Text(code.language).font(textStyle.system(10.5)).foregroundStyle(HubTheme.Semantic.secondaryInk)
                        .lineLimit(1)
                    Spacer(minLength: 0)
                    copyButton
                }
                .padding(.leading, textStyle.scaled(12)).padding(.trailing, textStyle.scaled(6)).padding(.top, textStyle.scaled(5))
                .frame(minHeight: textStyle.scaled(22))
            }
            ViewThatFits(in: .horizontal) {
                lines(size)
                ScrollView(.horizontal) { lines(size) }.fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(shape.fill(HubTheme.Semantic.raisedSurface))
        .clipShape(shape)
        .overlay(shape.strokeBorder(HubTheme.Semantic.hairline, lineWidth: 1))
        .overlay(alignment: .topTrailing) {
            if code.language.isEmpty { copyButton.padding(textStyle.scaled(5)) }
        }
        .padding(.leading, code.indent > 0 ? size * Double(min(code.indent, 8)) * 0.55 : 0)
        .onHover { hovering = $0 }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(code.language.isEmpty ? "Code block" : "Code block, \(code.language)")
    }

    private func lines(_ size: CGFloat) -> some View {
        ChatSelectableText(text: AttributedString(code.body), font: .monospacedSystemFont(ofSize: size * 0.92, weight: .regular),
                           color: HubTheme.Semantic.nsInk, message: message)
            .fixedSize()
            .padding(.horizontal, textStyle.scaled(12))
            .padding(.top, textStyle.scaled(code.language.isEmpty ? 9 : 3)).padding(.bottom, textStyle.scaled(9))
    }

    private var copyButton: some View {
        Button {
            ChatTranscriptTable.copy(code.body)
            copied = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { copied = false }
        } label: {
            Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
                .font(textStyle.system(10.5, weight: .medium))
                .foregroundStyle(HubTheme.Semantic.secondaryInk)
                .padding(.horizontal, textStyle.scaled(7)).padding(.vertical, textStyle.scaled(3))
                .background(RoundedRectangle(cornerRadius: textStyle.scaled(5)).fill(HubTheme.Semantic.surface))
                .overlay(RoundedRectangle(cornerRadius: textStyle.scaled(5)).strokeBorder(HubTheme.Semantic.hairline, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .opacity(hovering || copied ? 1 : 0)
        .allowsHitTesting(hovering || copied)
        .help("Copy code")
        .accessibilityLabel("Copy code")
    }
}

/// A bounded native table layout, measured only for its proposed width.
/// The ideal width is the readable column minimum: ViewThatFits then uses
/// horizontal scrolling only when even those minima cannot fit the pane.
struct ChatTableLayout: Layout {
    var hints: [CGFloat]
    var minimum: CGFloat
    struct Cache {
        var width: CGFloat?
        var widths: [CGFloat] = []
        var heights: [CGFloat] = []
    }
    func makeCache(subviews: Subviews) -> Cache { Cache() }
    func updateCache(_ cache: inout Cache, subviews: Subviews) { cache = Cache() }

    static func columnWidths(available: CGFloat?, hints: [CGFloat], minimum: CGFloat) -> [CGFloat] {
        guard !hints.isEmpty else { return [] }
        let floor = minimum * CGFloat(hints.count)
        let width = max(floor, available?.isFinite == true ? available! : floor)
        let weights = hints.map { max(1, min(64, $0)) }
        let sum = weights.reduce(0, +)
        return weights.map { minimum + (width - floor) * $0 / sum }
    }
    private func measure(_ proposal: ProposedViewSize, _ subviews: Subviews, _ cache: inout Cache) {
        let widths = Self.columnWidths(available: proposal.width, hints: hints, minimum: minimum)
        let width = widths.reduce(0, +)
        guard cache.width != width || cache.widths != widths else { return }
        cache.width = width; cache.widths = widths; cache.heights = []
        guard !widths.isEmpty else { return }
        for index in subviews.indices {
            let row = index / widths.count
            if row == cache.heights.count { cache.heights.append(0) }
            cache.heights[row] = max(cache.heights[row], ceil(subviews[index].sizeThatFits(
                ProposedViewSize(width: widths[index % widths.count], height: nil)).height))
        }
    }
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout Cache) -> CGSize {
        measure(proposal, subviews, &cache)
        return CGSize(width: cache.width ?? 0, height: cache.heights.reduce(0, +))
    }
    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout Cache) {
        measure(ProposedViewSize(width: bounds.width, height: nil), subviews, &cache)
        guard !cache.widths.isEmpty else { return }
        var x = bounds.minX, y = bounds.minY
        for index in subviews.indices {
            let column = index % cache.widths.count, row = index / cache.widths.count
            if column == 0 { x = bounds.minX }
            subviews[index].place(at: CGPoint(x: x, y: y), anchor: .topLeading,
                proposal: ProposedViewSize(width: cache.widths[column], height: cache.heights[row]))
            x += cache.widths[column]
            if column == cache.widths.count - 1 { y += cache.heights[row] }
        }
    }
}

struct ChatTranscriptTable: View {
    let table: ChatRenderedTable
    @Environment(\.chatTextStyle) private var textStyle
    private var columns: Int { table.source.header.count }

    var body: some View {
        ViewThatFits(in: .horizontal) {
            grid
            VStack(alignment: .leading, spacing: 0) {
                ScrollView(.horizontal) { grid.fixedSize(horizontal: true, vertical: true) }
                    .fixedSize(horizontal: false, vertical: true)
                Label("Scroll for more columns", systemImage: "arrow.left.and.right")
                    .font(.system(size: 10.5)).foregroundStyle(.secondary)
                    .padding(.horizontal, 10).padding(.vertical, 5)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 7))
        .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(HubTheme.Semantic.hairline, lineWidth: 1))
        .contextMenu {
            Button("Copy table (TSV)") { Self.copy(table.tsv) }
            Button("Copy table as Markdown") { Self.copy(table.source.source) }
        }
        .help("Right-click to copy this table as TSV or Markdown")
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Table, \(columns) columns, \(table.source.rows.count) rows")
    }

    private var grid: some View {
        let nativeFont = textStyle.nsFont
        return ChatTableLayout(hints: table.columnHints, minimum: max(80, nativeFont.pointSize * 6)) {
            ForEach(0..<(table.cells.count * columns), id: \.self) { index in
                let row = index / columns, column = index % columns
                let cell = table.cells[row][column]
                let alignment = table.source.alignments[column]
                Text(cell.formatted).font(Font(nativeFont)).fontWeight(row == 0 ? .semibold : .regular)
                    .foregroundStyle(HubTheme.Semantic.ink).textSelection(.enabled)
                    .multilineTextAlignment(alignment.text).fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 10).padding(.vertical, 8)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: alignment.frame)
                    .background(row == 0 ? HubTheme.Semantic.selection : row.isMultiple(of: 2) ? Color.primary.opacity(0.025) : Color.clear)
                    .overlay(alignment: .bottom) {
                        if row < table.cells.count - 1 { Rectangle().fill(HubTheme.Semantic.hairline).frame(height: 0.5) }
                    }
                    .overlay(alignment: .trailing) {
                        if column < columns - 1 { Rectangle().fill(HubTheme.Semantic.hairline).frame(width: 0.5) }
                    }
                    .accessibilityLabel(row == 0 ? cell.plain : "\(table.cells[0][column].plain): \(cell.plain)")
                    .accessibilityAddTraits(row == 0 ? .isHeader : [])
            }
        }
    }

    static func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}
