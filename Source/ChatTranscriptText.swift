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

struct ChatTextBlock: Equatable, Identifiable {
    enum Content: Equatable { case text(String), table(ChatMarkdownTable) }
    let id: Int                         // source line, stable while appending
    var content: Content
    var source: String {
        switch content { case .text(let value): return value; case .table(let table): return table.source }
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
    static func parse(_ source: String, streaming: Bool) -> [ChatTextBlock] {
        guard source.utf8.count <= maxMessageBytes, source.contains("|") else {
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
        var fence: (Character, Int)?
        func flushText() {
            if !text.isEmpty { blocks.append(ChatTextBlock(id: textStart, content: .text(text))); text = "" }
        }
        func appendText(_ line: Line, at index: Int) {
            if text.isEmpty { textStart = index }
            text += line.source
        }
        while index < lines.count {
            let line = lines[index]
            if let active = fence {
                appendText(line, at: index)
                if let end = fenceMarker(line.text), end.0 == active.0, end.1 >= active.1, end.2.isEmpty { fence = nil }
                index += 1
                continue
            }
            if let start = fenceMarker(line.text) {
                fence = (start.0, start.1)
                appendText(line, at: index); index += 1
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
    enum Content { case text(AttributedString), table(ChatRenderedTable) }
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
        let previouslyHadTables = blocks.contains { if case .table = $0.content { return true }; return false }
        let parsed = ChatTableParser.parse(text, streaming: streaming)
        let hasTables = parsed.contains { if case .table = $0.content { return true }; return false }
        blocks = parsed.compactMap { block in
            switch block.content {
            case .text(let raw):
                // VStack supplies spacing around tables. Preserve whitespace
                // in source/copy, but don't add those boundary newlines twice.
                let displayed = hasTables ? raw.trimmingCharacters(in: .newlines) : raw
                guard !displayed.isEmpty else { return nil }
                if let prior = old[block.id], prior.parsed == block, previouslyHadTables == hasTables { return prior }
                return ChatRenderedBlock(parsed: block, content: .text(Self.inline(displayed)))
            case .table(let table):
                if let prior = old[block.id], prior.parsed == block { return prior }
                var oldCells: [[ChatRenderedCell]] = []
                if case .table(let previous) = old[block.id]?.content { oldCells = previous.cells }
                let cells = ([table.header] + table.rows).enumerated().map { row, values in
                    values.enumerated().map { column, value -> ChatRenderedCell in
                        if row < oldCells.count, column < oldCells[row].count, oldCells[row][column].source == value {
                            return oldCells[row][column]
                        }
                        return ChatRenderedCell(source: value, formatted: Self.inline(value))
                    }
                }
                return ChatRenderedBlock(parsed: block, content: .table(ChatRenderedTable(source: table, cells: cells)))
            }
        }
    }

    private static func inline(_ text: String) -> AttributedString {
        guard text.utf8.count <= ChatTableParser.maxMessageBytes else { return AttributedString(text) }
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }
}

struct ChatTranscriptText: View {
    let text: String
    let streaming: Bool
    @StateObject private var presentation: ChatTextPresentation
    @Environment(\.chatTextStyle) private var textStyle

    init(text: String, streaming: Bool) {
        self.text = text; self.streaming = streaming
        _presentation = StateObject(wrappedValue: ChatTextPresentation(text: text, streaming: streaming))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(presentation.blocks) { block in
                switch block.content {
                case .text(let content):
                    Text(content).font(textStyle.font).foregroundStyle(HubTheme.Semantic.ink)
                        .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                case .table(let table): ChatTranscriptTable(table: table)
                }
            }
        }
        .onChange(of: text) { _, value in presentation.update(text: value, streaming: streaming) }
        .onChange(of: streaming) { _, value in presentation.update(text: text, streaming: value) }
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
