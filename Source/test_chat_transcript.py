"""Exercise the real Swift table parser, streaming presentation and layout math."""
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest


STUBS = r'''
import SwiftUI
enum HubTheme {
    enum Semantic {
        static let ink = Color.primary
        static let nsInk = NSColor.labelColor
        static let hairline = Color.primary.opacity(0.12)
        static let selection = Color.primary.opacity(0.065)
        static let raisedSurface = Color.primary.opacity(0.045)
        static let secondaryInk = Color.secondary
        static let surface = Color.white
        static func dynamicNS(light: NSColor, dark: NSColor) -> NSColor {
            NSColor(name: nil) { $0.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light }
        }
    }
}
'''

CASES = r'''
import AppKit
import SwiftUI
import Combine

@main struct Cases {
    @MainActor static func main() throws {
        func check(_ condition: Bool, _ message: String) { if !condition { fatalError(message) } }
        func tables(_ blocks: [ChatTextBlock]) -> [ChatMarkdownTable] {
            blocks.compactMap { if case .table(let table) = $0.content { return table }; return nil }
        }
        func parse(_ source: String, streaming: Bool = false) -> [ChatTextBlock] {
            let result = ChatTableParser.parse(source, streaming: streaming)
            check(result.map(\.source).joined() == source, "Parser changed source text")
            return result
        }

        let header = "| Project | What survives |\n| :--- | ---: |\n"
        let body = "| `data_KMM` | 1,299 **models**, textures and compiled maps |\n"
        let markdown = "Here is the archive.\n\n" + header + body + "\nKeep the originals."
        let blocks = parse(markdown)
        check(blocks.count == 3 && tables(blocks).count == 1, "Prose/table boundaries lost")
        let table = tables(blocks)[0]
        check(table.header == ["Project", "What survives"], "Header split failed")
        check(table.alignments == [.leading, .trailing], "Delimiter alignment lost")
        check(table.rows == [["`data_KMM`", "1,299 **models**, textures and compiled maps"]], "Cell text changed")
        check(table.source == header + body, "Copy table source includes adjacent prose")
        check(tables(parse("Name | Count | State\n:--- | :---: | ---:\nOne | 10 | Good")).first?.alignments ==
              [.leading, .center, .trailing], "Optional outer pipes/alignments failed")
        check(tables(parse("| Only |\n| --- |\n| one |")).first?.rows == [["one"]], "Single-column table failed")
        check(tables(parse("| A | B |\n| --- | --- |\n| value |\n")).first?.rows == [["value", ""]], "Short row padding lost")

        let escaped = "| A | B |\n| --- | --- |\n| a\\|b | `x | y` |\n| ``a ` b | c`` | \\| |\n"
        let escapeTable = tables(parse(escaped))[0]
        check(escapeTable.rows[0] == ["a\\|b", "`x | y`"], "Escaped/code pipe split")
        check(escapeTable.rows[1] == ["``a ` b | c``", "\\|"], "Backtick run/escaped edge lost")
        let crlf = markdown.replacingOccurrences(of: "\n", with: "\r\n")
        check(tables(parse(crlf))[0].rows == table.rows, "CRLF content changed")
        check(tables(parse("| 城市 | 状态 |\n| --- | --- |\n| 東京 🦉 | 好 |"))[0].rows[0] == ["東京 🦉", "好"], "Unicode changed")

        for literal in ["a | b\nc | d", "| A | B |\n| -- | --- |\n| x | y |", "| A | B |\n| --- |\n| x | y |",
                        "    | A | B |\n    | --- | --- |", "  \t| A | B |\n  \t| --- | --- |", "> | A | B |\n> | --- | --- |",
                        "```markdown\n" + header + body + "```", "~~~~\n" + header + body + "~~~~"] {
            check(tables(parse(literal)).isEmpty, "Prose, code or malformed delimiter became a table")
        }
        let fenced = "````\n```\n" + header + body + "````\n" + header + body
        check(tables(parse(fenced)).count == 1, "Short fence closed a longer fence")
        let extraCells = header + "| one | two | three |\n"
        check(tables(parse(extraCells))[0].rows.isEmpty, "Extra cells silently discarded")
        check(tables(parse(header + "| `unfinished | second |\n"))[0].rows.isEmpty, "Unclosed code shifted table cells")

        // Every stream prefix keeps all original text and avoids consuming the
        // live row. A valid table persists while that row is being completed.
        let stream = header + body + "| other | unfinished |"
        for end in stream.indices {
            let value = String(stream[..<end])
            _ = parse(value, streaming: true)
        }
        check(tables(parse("| A | B |\n| --- | --- |", streaming: true)).isEmpty, "Partial separator committed too early")
        let live = tables(parse(stream, streaming: true))[0]
        check(live.rows.count == 1, "Unfinished streamed row became cells")
        check(tables(parse(stream))[0].rows.count == 2, "Final row missing at end of turn")
        let before = parse(header + body, streaming: true).first!.id
        check(parse(stream, streaming: true).first!.id == before, "Table identity changed while streaming")

        let tooManyRows = header + String(repeating: "| x | y |\n", count: ChatTableParser.maxRows + 1)
        check(tables(parse(tooManyRows)).isEmpty, "Oversized table created native rows")
        let wideHeader = "|" + Array(repeating: "a", count: 13).joined(separator: "|") + "|\n"
        let wideDelimiter = "|" + Array(repeating: "---", count: 13).joined(separator: "|") + "|\n"
        check(tables(parse(wideHeader + wideDelimiter)).isEmpty, "Column budget ignored")
        let huge = header + "| x | " + String(repeating: "z", count: ChatTableParser.maxTableBytes) + " |\n"
        check(tables(parse(huge)).isEmpty, "Table byte budget ignored")
        let hugeMessage = String(repeating: "z", count: ChatTableParser.maxMessageBytes) + header
        check(tables(parse(hugeMessage)).isEmpty, "Message parse budget ignored")
        let manyTables = (0..<30).map { "Table \($0)\n" + header + body + "\n" }.joined()
        check(tables(parse(manyTables)).count == ChatTableParser.maxTables, "Table count budget ignored")
        let cellTable = "|" + Array(repeating: "A", count: 12).joined(separator: "|") + "|\n" +
            "|" + Array(repeating: "---", count: 12).joined(separator: "|") + "|\n" +
            String(repeating: "|" + Array(repeating: "x", count: 12).joined(separator: "|") + "|\n", count: 100)
        check(tables(parse(cellTable + "\n" + cellTable)).count == 1, "Total native cell budget ignored")

        let presentation = ChatTextPresentation(text: escaped, streaming: false)
        guard case .table(let rendered) = presentation.blocks[0].content else { fatalError("Table missing") }
        check(rendered.tsv == "A\tB\na|b\tx | y\na ` b | c\t|", "TSV changed displayed cell values")
        let spaced = ChatTextPresentation(text: markdown, streaming: false)
        guard case .text(let above) = spaced.blocks.first?.content,
              case .text(let below) = spaced.blocks.last?.content else { fatalError("Prose missing") }
        check(String(above.characters) == "Here is the archive.", "Table spacing doubled above")
        check(String(below.characters) == "Keep the originals.", "Table spacing doubled below")
        check(spaced.blocks.map { $0.parsed.source }.joined() == markdown, "Presentation changed copied source")
        var published = 0
        let observation = presentation.objectWillChange.sink { published += 1 }
        presentation.update(text: escaped, streaming: false)
        check(published == 0, "Unrelated parent update rebuilt presentation")
        presentation.update(text: "Replacement prose", streaming: false)
        check(presentation.blocks.count == 1 && presentation.blocks[0].parsed.source == "Replacement prose", "Replacement kept stale cells")
        presentation.update(text: stream, streaming: true)
        presentation.update(text: stream, streaming: false)
        guard case .table(let completed) = presentation.blocks[0].content else { fatalError("Completed table missing") }
        check(completed.source.rows.count == 2, "Streaming flag did not commit the last row")
        _ = observation

        let widths = ChatTableLayout.columnWidths(available: 760, hints: [8, 64], minimum: 80)
        check(abs(widths.reduce(0, +) - 760) < 0.01 && widths[1] > widths[0], "Columns did not share available width")
        let narrow = ChatTableLayout.columnWidths(available: 200, hints: [10, 10, 10, 10], minimum: 80)
        check(narrow == [80, 80, 80, 80], "Narrow table crushed columns instead of overflowing")
        let ideal = ChatTableLayout.columnWidths(available: nil, hints: [8, 60], minimum: 80)
        check(ideal == [80, 80], "Ideal minimum prevents responsive layout")

        // Reading zoom: bounded steps, a stored preference, scaled reading
        // text, and a View menu that only acts on the Chat window.
        check(ChatZoom.larger(1) == 1.15 && ChatZoom.smaller(1) == 0.85, "Zoom steps")
        check(ChatZoom.larger(3) == 3 && ChatZoom.smaller(0.85) == 0.85, "Zoom escaped its bounds")
        check(ChatZoom.larger(1.2) == 1.3 && ChatZoom.smaller(1.2) == 1.15, "Off-step zoom did not snap")
        check(ChatZoom.clamp(.nan) == 1 && ChatZoom.clamp(40) == 3 && ChatZoom.percent(1.75) == "175%", "Zoom clamp or label")
        let zoomSuite = "chat-zoom-tests." + UUID().uuidString
        let zoomDefaults = UserDefaults(suiteName: zoomSuite)!
        defer { zoomDefaults.removePersistentDomain(forName: zoomSuite) }
        check(ChatZoom.stored(zoomDefaults) == 1, "Default zoom")
        ChatZoom.zoomIn(zoomDefaults); ChatZoom.zoomIn(zoomDefaults)
        check(ChatZoom.stored(zoomDefaults) == 1.3, "Zoom in")
        ChatZoom.zoomOut(zoomDefaults)
        check(ChatZoom.stored(zoomDefaults) == 1.15, "Zoom out")
        ChatZoom.reset(zoomDefaults)
        check(ChatZoom.stored(zoomDefaults) == 1, "Actual size")
        let zoomed = ChatTextStyle(size: 13, zoom: 2)
        check(zoomed.nsFont.pointSize == 26 && zoomed.editorFont.pointSize == 28 && zoomed.scaled(10) == 20, "Zoom did not scale reading text")
        check(ChatFonts.font("system", size: 400, scale: 2).pointSize == 64, "Zoom applied before the size bound")
        let viewMenu = ChatZoomCommands.shared.menu()
        check(viewMenu.items.map(\.keyEquivalent) == ["+", "=", "-", "0"] && viewMenu.items[1].isHidden
              && viewMenu.items[1].allowsKeyEquivalentWhenHidden, "View menu shortcuts")
        check(!ChatZoomCommands.shared.validateMenuItem(viewMenu.items[0]), "Zoom acted without the Chat window")

        // Selectable transcript text: inline Markdown becomes real fonts and
        // links, short text hugs its width, long text wraps, and the native
        // menu keeps the system Copy and adds Copy Message exactly once.
        let inline = try AttributedString(markdown: "Plain **bold** *it* `code` ~~gone~~ [link](https://example.com)",
                                          options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))
        let base = NSFont.systemFont(ofSize: 15)
        let native = ChatSelectableText.attributed(inline, font: base, color: .systemRed)
        check(native.string == "Plain bold it code gone link", "Selectable text changed characters: " + native.string)
        func at(_ word: String) -> Int { (native.string as NSString).range(of: word).location }
        func font(_ word: String) -> NSFont { native.attribute(.font, at: at(word), effectiveRange: nil) as! NSFont }
        check(font("Plain") == base, "Base font lost")
        check(font("bold").fontDescriptor.symbolicTraits.contains(.bold), "Strong text not bold")
        check(font("it").fontDescriptor.symbolicTraits.contains(.italic) || native.attribute(.obliqueness, at: at("it"), effectiveRange: nil) != nil,
              "Emphasis lost")
        check(font("code").isFixedPitch, "Inline code not monospaced")
        check(native.attribute(.strikethroughStyle, at: at("gone"), effectiveRange: nil) != nil, "Strikethrough lost")
        check((native.attribute(.link, at: at("link"), effectiveRange: nil) as? URL)?.absoluteString == "https://example.com", "Link lost")
        check(native.attribute(.foregroundColor, at: 0, effectiveRange: nil) as? NSColor == .systemRed, "Ink lost")

        let sizing = ChatSelectableText.Coordinator()
        _ = sizing.attributed(for: ChatSelectableText(text: AttributedString("Hi"), font: base, color: .systemRed))
        let short = sizing.size(width: 400)
        check(short.width < 60 && short.height > 10 && short.height < 30, "Short text size \(short)")
        _ = sizing.attributed(for: ChatSelectableText(text: AttributedString(String(repeating: "word ", count: 200)), font: base, color: .systemRed))
        let long = sizing.size(width: 300)
        check(long.width <= 300 && long.height > short.height * 5, "Long text did not wrap: \(long)")

        let textView = ChatSelectableText.TextView(usingTextLayoutManager: false)
        textView.isEditable = false
        textView.string = "Selected"
        textView.message = "Whole message"
        let board = NSPasteboard(name: NSPasteboard.Name("chat-selectable-tests." + UUID().uuidString))
        defer { board.releaseGlobally() }
        textView.pasteboard = board
        let click = NSEvent.mouseEvent(with: .rightMouseDown, location: .zero, modifierFlags: [], timestamp: 0, windowNumber: 0,
                                       context: nil, eventNumber: 0, clickCount: 1, pressure: 1)!
        _ = textView.menu(for: click)
        let menu = textView.menu(for: click)!
        check(menu.items.filter { $0.title == "Copy Message" }.count == 1, "Copy Message missing or repeated: \(menu.items.map(\.title))")
        check(menu.items.contains { $0.action == #selector(NSText.copy(_:)) }, "System Copy missing: \(menu.items.map(\.title))")
        textView.copyMessage(nil)
        check(board.string(forType: .string) == "Whole message", "Copy Message copied the wrong text")
        textView.message = nil
        check(!textView.menu(for: click)!.items.contains { $0.title == "Copy Message" }, "Copy Message shown without a message")

        // Block Markdown: fences become code blocks in the one parser, and
        // prose keeps its lines while headings, lists, quotes, rules and
        // inline code chips gain real layout.
        func codes(_ blocks: [ChatTextBlock]) -> [ChatMarkdownCode] {
            blocks.compactMap { if case .code(let code) = $0.content { return code }; return nil }
        }
        let reply = "### What Finished\n1. **Unified Step** (`Physics.swift`):\n  - Factored capsule movement\n    - Deeper detail\n\n" +
            "10. Tenth\n\n```swift\nlet x = 1\n" + header + "```\n\n" + header + body + "\n- Done\n   ~~~\n   indented\n   ~~~\n"
        let mixed = parse(reply)
        check(codes(mixed).count == 2 && tables(mixed).count == 1, "Fence/table split: \(mixed.map(\.content))")
        check(codes(mixed)[0].language == "swift" && codes(mixed)[0].body == "let x = 1\n" + header.dropLast() && codes(mixed)[0].closed,
              "Fence body or language changed")
        check(codes(mixed)[1].body == "indented" && codes(mixed)[1].indent == 3, "Indented fence kept its indentation")
        for end in reply.indices { _ = parse(String(reply[..<end]), streaming: true) }
        let growing = codes(parse("Intro\n```python\nprint(1)\n``", streaming: true))
        check(growing.count == 1 && growing[0].body == "print(1)" && !growing[0].closed, "Streaming fence lost or showed a partial close")
        check(codes(parse("Intro\n```python\nprint(1)\n", streaming: false)).first?.body == "print(1)", "Unclosed final fence")
        check(codes(parse("```swi", streaming: true)).isEmpty, "Unfinished opening fence committed early")
        check(codes(parse("Use ```inline``` here\n")).isEmpty, "Inline backticks became a fence")
        let fencedPresentation = ChatTextPresentation(text: reply, streaming: false)
        check(fencedPresentation.blocks.map(\.parsed) == mixed.filter { !$0.source.allSatisfy(\.isWhitespace) },
              "Code presentation dropped or changed a block")
        let streamed = ChatTextPresentation(text: "Intro\n```swift\nlet a", streaming: true)
        let introBlock = streamed.blocks[0]
        streamed.update(text: "Intro\n```swift\nlet a = 1\n", streaming: true)
        guard case .code(let liveCode) = streamed.blocks.last?.content, case .text = introBlock.content else { fatalError("Live code block missing") }
        check(liveCode.body == "let a = 1" && streamed.blocks[0].parsed == introBlock.parsed, "Streaming code block did not grow in place")

        let blocksText = ChatMarkdownBlocks.format("### What Finished ###\n1. **Unified** (`Physics.swift`):\n  - Factored capsule\n" +
            "    - Deeper\n      continued\n10) Tenth\n# Big\n> quoted\n---\nplain *it*\n`chip` first")
        check(String(blocksText.characters) == "What Finished\n\t1.\tUnified (Physics.swift):\n\t•\tFactored capsule\n\t–\tDeeper\n" +
              "continued\n\t10)\tTenth\nBig\nquoted\n---\nplain it\nchip first", "Block text: " + String(blocksText.characters))
        check(ChatMarkdownBlocks.format("No blocks, **bold** only") == ChatMarkdownBlocks.inline("No blocks, **bold** only"), "Plain prose changed")
        let blockNative = ChatSelectableText.attributed(blocksText, font: base, color: .systemRed)
        let blockString = blockNative.string as NSString
        func spot(_ word: String) -> Int { blockString.range(of: word).location }
        func blockFont(_ word: String) -> NSFont { blockNative.attribute(.font, at: spot(word), effectiveRange: nil) as! NSFont }
        func paragraph(_ word: String) -> NSParagraphStyle? { blockNative.attribute(.paragraphStyle, at: spot(word), effectiveRange: nil) as? NSParagraphStyle }
        let weight = { (font: NSFont) in NSFontManager.shared.weight(of: font) }
        check(blockFont("What").pointSize == 15 && weight(blockFont("What")) > weight(base), "h3 not semibold body size")
        check(abs(blockFont("Big").pointSize - 18.75) < 0.01, "h1 not larger")
        check(blockFont("Unified").fontDescriptor.symbolicTraits.contains(.bold), "Bold lost inside a list item")
        check(blockNative.attribute(.chatCodeChip, at: spot("Physics"), effectiveRange: nil) != nil && blockFont("Physics").isFixedPitch,
              "Inline code is not a chip")
        check(blockNative.attribute(.kern, at: spot("(Physics") , effectiveRange: nil) != nil &&
              blockNative.attribute(.kern, at: spot("swift)"), effectiveRange: nil) == nil &&
              blockNative.attribute(.kern, at: spot("swift)") + 4, effectiveRange: nil) != nil, "Chip padding kern misplaced")
        let nested = paragraph("Factored")!
        check(abs(nested.firstLineHeadIndent - 1.9 * 15) < 0.01 && abs(nested.headIndent - (1.9 + 1.4) * 15) < 0.01,
              "Nested bullet hanging indent: \(nested.firstLineHeadIndent) \(nested.headIndent)")
        check(abs(paragraph("Deeper")!.headIndent - (1.9 + 1.4 + 1.4) * 15) < 0.01, "Third level indent")
        check(abs(paragraph("continued")!.firstLineHeadIndent - paragraph("Deeper")!.headIndent) < 0.01, "Continuation not under its item")
        let tenth = paragraph("Tenth")!
        check(tenth.tabStops.first?.alignment == .right && abs(tenth.tabStops[0].location - (1.9 - 0.45) * 15) < 0.01
              && abs(tenth.headIndent - 1.9 * 15) < 0.01, "Numbers are not right-aligned in their column")
        check(paragraph("What")!.paragraphSpacingBefore == 0 && paragraph("Big")!.paragraphSpacingBefore > 0, "Heading spacing")
        check(blockNative.attribute(.chatQuoteBar, at: spot("quoted"), effectiveRange: nil) != nil, "Quote bar missing")
        check(blockNative.attribute(.chatRule, at: spot("---"), effectiveRange: nil) != nil, "Rule missing")
        check(paragraph("plain") == nil, "Plain paragraph gained a style")
        check((paragraph("chip first")?.firstLineHeadIndent ?? 0) > 0, "Leading chip has no room for its fill")

        // The real view stack: chip, quote and rule drawing across wraps,
        // and the measuring stack using the same layout class.
        let drawn = ChatSelectableText.makeTextView()
        check(drawn.layoutManager is ChatLayoutManager, "Text view does not draw chips")
        drawn.frame = NSRect(x: 0, y: 0, width: 90, height: 600)
        drawn.textStorage?.setAttributedString(blockNative)
        let drawnRep = drawn.bitmapImageRepForCachingDisplay(in: drawn.bounds)!
        drawn.cacheDisplay(in: drawn.bounds, to: drawnRep)
        let blockSizing = ChatSelectableText.Coordinator()
        _ = blockSizing.attributed(for: ChatSelectableText(text: blocksText, font: base, color: .systemRed))
        drawn.layoutManager!.ensureLayout(for: drawn.textContainer!)
        check(abs(blockSizing.size(width: 90).height - ceil(drawn.layoutManager!.usedRect(for: drawn.textContainer!).height)) < 0.5,
              "Measured height differs from drawn height")

        // A recorded @tag is a semibold chip in its member's accent; nothing else in the message changes.
        let tagged = ChatSelectableText.attributed(ChatMentions.marked("Ship it, @Sol.", mentions: [
            ChatMention(id: "s", name: "Sol", route: "r", start: 9, length: 4)], accent: { _ in .purple }), font: base, color: .systemRed)
        let tagAt = (tagged.string as NSString).range(of: "@Sol").location
        check(tagged.string == "Ship it, @Sol." && tagged.attribute(.chatCodeChip, at: tagAt, effectiveRange: nil) != nil &&
              weight(tagged.attribute(.font, at: tagAt, effectiveRange: nil) as! NSFont) > weight(base) &&
              tagged.attribute(.foregroundColor, at: tagAt, effectiveRange: nil) as? NSColor != .systemRed &&
              tagged.attribute(.chatCodeChip, at: 0, effectiveRange: nil) == nil &&
              tagged.attribute(.kern, at: tagAt - 1, effectiveRange: nil) != nil, "A recorded tag is not an accent chip")
        let leading = ChatSelectableText.attributed(ChatMentions.marked("@Sol hi", mentions: [
            ChatMention(id: "s", name: "Sol", route: "r", start: 0, length: 4)], accent: { _ in .purple }), font: base, color: .systemRed)
        check(((leading.attribute(.paragraphStyle, at: 0, effectiveRange: nil) as? NSParagraphStyle)?.firstLineHeadIndent ?? 0) > 0,
              "A leading tag's chip would be clipped")

        let started = Date()
        for _ in 0..<50 { _ = ChatTableParser.parse(tooManyRows, streaming: true) }
        print("50 bounded table parses: \(Date().timeIntervalSince(started))s")
        print("Transcript parser, streaming, copy and layout checks passed")
    }
}
'''

LAYOUT_CASES = r'''
import SwiftUI

struct Item: ChatTranscriptItem {
    var id: String; var kind: String; var text = ""; var route = "codex/sol"
    var tool: String? = nil; var summary: String? = nil; var detail: String? = nil
    var memberID: String? = nil; var memberName: String? = nil
    var agentID: String? = nil; var agentIDs: [String]? = nil
    var changedFiles: [String] = []
}

@main struct Cases {
    static func main() {
        func check(_ condition: Bool, _ message: String) { if !condition { fatalError(message) } }
        let sol = (id: "m1", name: "Sol"), opus = (id: "m2", name: "Opus")
        func tool(_ id: String, _ name: String = "run_shell", member: (id: String, name: String) = sol, route: String = "codex/sol",
                  agent: String? = nil) -> Item {
            Item(id: id, kind: "tool", route: route, tool: name, summary: "Run in /w:\nls", memberID: member.id, memberName: member.name, agentID: agent)
        }
        func reply(_ id: String, _ text: String, member: (id: String, name: String) = sol, route: String = "codex/sol") -> Item {
            Item(id: id, kind: "assistant", text: text, route: route, memberID: member.id, memberName: member.name)
        }
        typealias Segment = ChatTranscriptSegment<Item>

        // One header per speaker block; empty rounds vanish; tool runs fold
        // across them and across hidden notices; a checkpoint ends the block.
        let items = [
            Item(id: "u1", kind: "user", text: "Go"),
            reply("a1", "  \n"), tool("t1"), reply("a1b", ""), tool("t2", "read_file"), tool("t3", "apply_patch"),
            reply("a2", "Found it."), tool("t4"),
            Item(id: "trim", kind: "notice", text: ChatNotice.contextTrimmed),
            Item(id: "routine", kind: "notice", text: ChatNotice.checkpointPrefix + " Your requested continuation will resume after other members."),
            Item(id: "n1", kind: "notice", text: ChatNotice.checkpointPrefix + " This member did not request continuation; send a message to continue."),
            reply("a3", "", member: opus, route: "claude/opus"), tool("t5", member: opus, route: "claude/opus"),
            tool("d1", "delegate", member: opus, route: "claude/opus", agent: "child"), tool("t6", member: opus, route: "claude/opus"),
        ]
        let segments = Segment.outline(items) { $0.id == "a3" }
        check(segments.map(\.id) == ["u1", "speaker-t1", "fold-t1", "a2", "t4", "n1", "speaker-a3", "a3", "t5", "d1", "t6"],
              "Outline order: " + segments.map(\.id).joined(separator: ","))
        check(segments.map(\.spacing) == [.none, .speaker, .header, .item, .item, .speaker, .speaker, .header, .item, .item, .item],
              "Speaker and in-block spacing changed")
        if case .fold(let folded) = segments[2].content { check(folded.map(\.id) == ["t1", "t2", "t3"], "Fold lost a step") }
        else { fatalError("Three tool rows across an empty round did not fold") }
        check(segments[5].previousSpeaker?.memberName == "Sol", "Checkpoint does not know whose block it ends")
        check(Segment.outline(items) { _ in false }.contains { $0.id == "a3" } == false, "Empty finished reply still drawn")
        let sameRouteOtherMember = [reply("x1", "One"), reply("x2", "Two", member: opus)]
        check(Segment.outline(sameRouteOtherMember) { _ in false }.filter(\.isSpeaker).count == 2, "Members on one route shared a header")
        let continued = [reply("y1", "One"), tool("y2"), reply("y3", "Two")]
        check(Segment.outline(continued) { _ in false }.filter(\.isSpeaker).count == 1, "One speaker repeated its header")

        // A Team turn that changed files closes with one table of them, after
        // the turn ends: at the next request, or once the Team stops working.
        let diffA = "Applied patch:\nA.swift\nB.swift\n\n--- a/A.swift\n+++ b/A.swift\n@@ -1 +1,2 @@\n-old\n+new\n+more\n"
            + "--- /dev/null\n+++ b/B.swift\n@@ -0,0 +1 @@\n+b\n"
        var p1 = Item(id: "p1", kind: "tool", tool: "apply_patch", summary: "Apply patch:\nUpdate: A.swift\nAdd: B.swift", detail: diffA,
                      memberID: sol.id, memberName: sol.name)
        p1.changedFiles = ["A.swift", "B.swift"]
        var p2 = Item(id: "p2", kind: "tool", route: "claude/opus", tool: "apply_patch", summary: "Apply patch:\nUpdate: A.swift",
                      detail: "Applied patch:\nA.swift\n\n--- a/A.swift\n+++ b/A.swift\n@@ -2 +2 @@\n-more\n+most\n",
                      memberID: opus.id, memberName: opus.name)
        p2.changedFiles = ["A.swift"]
        let teamTurn = [Item(id: "u", kind: "user", text: "Go"), p1, reply("r1", "Done"), p2, Item(id: "u2", kind: "user", text: "Next")]
        check(Segment.outline(teamTurn) { _ in false }.map(\.id) == ["u", "speaker-p1", "p1", "r1", "speaker-p2", "p2", "changes-p1", "u2"],
              "Close-out placement: " + Segment.outline(teamTurn) { _ in false }.map(\.id).joined(separator: ","))
        let working = Array(teamTurn.dropLast())
        check(!Segment.outline(working, open: true) { _ in false }.contains { $0.id == "changes-p1" }, "Close-out drawn mid-turn")
        check(Segment.outline(working) { _ in false }.last?.id == "changes-p1", "Finished turn lost its close-out")
        var solo = p1; solo.memberID = nil; solo.memberName = nil
        check(!Segment.outline([teamTurn[0], solo]) { _ in false }.contains { $0.id.hasPrefix("changes-") }, "Solo turn grew a Team close-out")
        let changes = ChatTurnChanges.collect([p1, p2])
        check(changes.files.map(\.path) == ["A.swift", "B.swift"], "Changed files: \(changes.files.map(\.path))")
        check(changes.files[0].members == ["Sol", "Opus"], "Members per file")
        check(changes.files[0].added == 3 && changes.files[0].removed == 2, "A.swift counts: \(changes.files[0])")
        check(changes.files[1].added == 1 && changes.files[1].removed == 0, "Created file counts")
        check(changes.added == 4 && changes.removed == 2, "Turn totals")
        check(changes.files[0].diff.contains("+most") && !changes.files[0].diff.contains("+b"), "Diff sections crossed files")

        // Tool rows lead with what was done, not the shared folder.
        let shell = ChatToolDisplay.describe(tool: "run_shell", summary: "Run in /Users/x/BF2:\ngit status --short")
        check(shell == ChatToolDisplay(verb: "Ran", subject: "git status --short", code: true, command: "git status --short"), "Shell row")
        check(ChatToolDisplay.describe(tool: "run_shell", summary: "Run in /w:\ncd a\nmake", live: true).subject == "cd a …", "Multi-line command")
        check(ChatToolDisplay.describe(tool: "run_shell", summary: "Run in /w:\nls", live: true).verb == "Running", "Live verb")
        let edit = ChatToolDisplay.describe(tool: "apply_patch", summary: "Apply patch:\nUpdate: Source/A.swift\nAdd: Source/B.swift → Move to: Source/C.swift")
        check(edit.verb == "Edited" && edit.subject == "A.swift, C.swift", "Patch row: \(edit)")
        check(ChatToolDisplay.describe(tool: "apply_patch", summary: "Apply patch:\nAdd: a\nAdd: b\nAdd: c\nAdd: d\nAdd: e").subject == "a, b, c +2 more", "Many files")
        check(ChatToolDisplay.describe(tool: "apply_patch", summary: "Apply patch:\nAdd: new.py").verb == "Created", "Created")
        check(ChatToolDisplay.describe(tool: "apply_patch", summary: "apply_patch failed") == ChatToolDisplay(verb: "Edited files"), "Failed patch")
        let read = ChatToolDisplay.describe(tool: "read_file", summary: "Read tools/k.py (line 85, limit 175)")
        check(read.subject == "tools/k.py" && read.context == "lines 85–259", "Read range")
        check(ChatToolDisplay.describe(tool: "read_file", summary: "Read k.py (line 1, limit 500)").context == "", "Whole read")
        let search = ChatToolDisplay.describe(tool: "search_files", summary: "Search BF2 for 'water_height' (glob *.py)")
        check(search.subject == "water_height" && search.code && search.context == "in BF2 · *.py", "Search: \(search)")
        check(ChatToolDisplay.describe(tool: "web_search", summary: "Web search: msh format").subject == "msh format", "Web query")
        check(ChatToolDisplay.describe(tool: "web_search", summary: "Web search").subject == "", "Bare web search")
        check(ChatToolDisplay.describe(tool: "mystery", summary: "mystery failed").subject == "", "Unknown tool repeated its name")
        check(ChatToolDisplay.describe(tool: "mystery", summary: "Did a thing\nmore").subject == "Did a thing", "Unknown summary dropped")
        check(ChatToolDisplay.activity(["run_shell", "run_shell", "apply_patch", "read_file", nil, "search_history", "read_history"])
              == "2 commands, 1 edit, 1 read, 1 step, 2 recalls", "Fold activity")
        check(ChatToolDisplay.describe(tool: "blackboard_post", summary: "Pinned schema (risk)")
              == ChatToolDisplay(verb: "Pinned to the Blackboard", subject: "schema (risk)", code: true), "Blackboard post phrasing")
        check(ChatToolDisplay.describe(tool: "blackboard_remove", summary: "Removed schema", live: true).verb == "Removing a Blackboard post", "Live Blackboard removal")
        check(ChatToolDisplay.activity(["blackboard_post", "blackboard_post", "blackboard_read"]) == "2 pins, 1 board read", "Blackboard fold activity")
        check(ChatToolDisplay.describe(tool: "blackboard_attach", summary: "Added notes.txt, example.com/spec")
              == ChatToolDisplay(verb: "Added to the Blackboard", subject: "notes.txt, example.com/spec"), "Blackboard attach phrasing")

        // Source links collapse only when the line is exactly the runtime's list.
        let cited = ChatReplySources.split("Body\n\nSources: [A \\[x\\]](<https://www.a.com/p>), [B](<https://b.org/q?x=1>)")
        check(cited.body == "Body" && cited.links.map(\.title) == ["A [x]", "B"] && cited.links.map(\.host) == ["a.com", "b.org"], "Sources split")
        check(ChatReplySources.split("\n\nSources: [A](<https://a.com>)").body == "", "Sources-only reply")
        check(ChatReplySources.split("Sources: [A](<https://a.com>)").links.count == 1, "Leading sources line")
        for literal in ["Body\n\nSources: see the appendix", "Body\n\nSources: [A](<javascript:alert(1)>)",
                        "Inline Sources: [A](<https://a.com>) here", "Body\n\nSources: [A](<https://a.com>) and more"] {
            let kept = ChatReplySources.split(literal)
            check(kept.body == literal && kept.links.isEmpty, "Prose hidden as sources: " + literal)
        }

        // Notices.
        check(ChatNotice.style(ChatNotice.contextTrimmed) == .hidden, "Trim notice shown")
        check(ChatNotice.style("Team stopped. Recorded results are kept.") == .divider, "Plain notice")
        check(ChatNotice.checkpointLine(ChatNotice.checkpointPrefix + " Your requested continuation will resume after other members.", member: "Sol")
              == "Sol reached a checkpoint · continues after the other members", "Checkpoint line")
        check(ChatNotice.checkpointLine(ChatNotice.checkpointPrefix + " This member did not request continuation; send a message to continue.", member: nil)
              == "Checkpoint reached · send a message to continue", "Checkpoint without member")
        // The wording finish_checkpoint writes when the round budget runs out.
        let budget = " The 24-round tool budget was used up. "
        // Continuing later or finishing as planned is routine; the sign-off
        // says so. A pause or a question still marks the transcript.
        check(ChatNotice.style(ChatNotice.checkpointPrefix + budget + "This member's requested continuation will resume after other members.") == .hidden,
              "Routine continuation checkpoint shown")
        check(ChatNotice.style(ChatNotice.checkpointPrefix + budget + "This member finished its contribution.") == .hidden, "Routine finish shown")
        check(ChatNotice.style(ChatNotice.checkpointPrefix + budget + "This member needs your answer before continuing.") == .checkpoint,
              "Question checkpoint hidden")
        check(ChatNotice.style(ChatNotice.checkpointPrefix + budget + "This member paused; send a message to continue from the recorded results.")
              == .checkpoint, "Pause checkpoint hidden")
        check(ChatNotice.checkpointLine(ChatNotice.checkpointPrefix + budget + "This member's requested continuation will resume after other members.", member: "Sol")
              == "Sol reached a checkpoint · round budget used · continues after the other members", "Budget continuation")
        check(ChatNotice.checkpointLine(ChatNotice.checkpointPrefix + budget + "This member finished its contribution.", member: "Opus")
              == "Opus reached a checkpoint · round budget used · contribution finished", "Budget finished")
        check(ChatNotice.checkpointLine(ChatNotice.checkpointPrefix + budget + "This member needs your answer before continuing.", member: "Opus")
              == "Opus reached a checkpoint · round budget used · needs your answer", "Budget question")
        check(ChatNotice.checkpointLine(ChatNotice.checkpointPrefix + budget + "This member paused; send a message to continue from the recorded results.", member: "Muse")
              == "Muse reached a checkpoint · round budget used · send a message to continue", "Budget pause")
        let turn = ChatNotice.turnCheckpointPrefix + budget + "Recorded results are kept; send a message to continue if work remains."
        check(ChatNotice.style(turn) == .checkpoint, "Solo turn checkpoint drawn as a plain divider")
        check(ChatNotice.checkpointLine(turn, member: "GPT 6.1 Sol")
              == "GPT 6.1 Sol reached a checkpoint · round budget used · send a message to continue", "Turn checkpoint")
        check(ChatNotice.checkpointLine(ChatNotice.checkpointPrefix + " Something new happened.", member: "Sol")
              == "Sol reached a checkpoint · Something new happened", "Unknown checkpoint wording lost")

        let failure = ChatNotice.failurePrefix + "5 attempts · connection reset"
        check(ChatNotice.style(failure) == .failure, "Member failure warning hidden")
        check(ChatNotice.failureLine(failure, member: "Opus") == "Opus stopped after 5 attempts · connection reset", "Member failure attribution")
        check(ChatNotice.failureLine(failure, member: nil) == failure, "Missing member lost the warning")
        let failedRows = Segment.outline([Item(id: "failed", kind: "notice", text: failure)], isLive: { _ in false })
        check(failedRows.count == 1, "Member failure lost from outline")

        // Every glyph a row can ask for exists, and the path parser reads the
        // catalogue's curve syntax, including relative curves.
        for name in ["run_shell", "apply_patch", "read_file", "search_files", "web_search", "search_history", "record_decision",
                     "delegate", "team_status", "blackboard_post", "blackboard_read", nil] {
            check(ChatToolGlyph.has(ChatToolDisplay.glyph(name)), "Missing glyph for \(name ?? "nil")")
        }
        check(ChatToolGlyph.has("handoff"), "Missing handoff glyph")
        let box = ChatToolGlyph.svg(["M4.9 5.5 19.2 5.2 19 18.6 5.2 18.9Z"]).boundingRect
        check(abs(box.minX - 4.9) < 0.01 && abs(box.maxX - 19.2) < 0.01 && abs(box.maxY - 18.9) < 0.01, "Polyline parsed wrong: \(box)")
        let arc = ChatToolGlyph.svg(["M4.5 16.2C5 9.7 8.2 5.7 12.1 5.7c4.1 0 7.1 4 7.5 10.5"]).boundingRect
        check(abs(arc.maxX - 19.6) < 0.01 && abs(arc.maxY - 16.2) < 0.01 && arc.minY >= 5.6, "Relative curve parsed wrong: \(arc)")
        check(ChatToolGlyph.svg(["M12 8.3V9.9"]).boundingRect.height > 1.5, "Vertical line lost")
        check(ChatToolGlyph.svg(["M1 2Z 3 4"]).isEmpty, "Malformed path data drew a shape")
        print("Transcript outline, tool phrasing, sources, notices and glyph checks passed")
    }
}
'''


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "Native transcript tests need macOS")
class ChatTranscriptTests(unittest.TestCase):
    def run_native(self, files, cases, marker, stubs=None):
        source = Path(__file__).parent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extra = []
            if stubs is not None:
                (root / "Stubs.swift").write_text(stubs)
                extra.append(str(root / "Stubs.swift"))
            (root / "Cases.swift").write_text(cases)
            binary = root / "transcript-tests"
            compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-target", platform.machine() + "-apple-macosx14.0", "-module-cache-path", str(root / "cache"),
                *extra, *(str(source / name) for name in files),
                str(root / "Cases.swift"), "-framework", "AppKit", "-framework", "SwiftUI", "-o", str(binary)],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertIn(marker, ran.stdout)

    def test_parser_streaming_copy_budgets_and_column_layout(self):
        self.run_native(["ChatFonts.swift", "ChatMentions.swift", "ChatSelectableText.swift", "ChatTranscriptText.swift"], CASES,
                        "Transcript parser, streaming, copy and layout checks passed", stubs=STUBS)

    def test_outline_tool_phrasing_sources_notices_and_glyphs(self):
        self.run_native(["ChatTranscriptLayout.swift", "ChatToolGlyph.swift"], LAYOUT_CASES,
                        "Transcript outline, tool phrasing, sources, notices and glyph checks passed")


if __name__ == "__main__":
    unittest.main()
