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
        static let hairline = Color.primary.opacity(0.12)
        static let selection = Color.primary.opacity(0.065)
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
        let started = Date()
        for _ in 0..<50 { _ = ChatTableParser.parse(tooManyRows, streaming: true) }
        print("50 bounded table parses: \(Date().timeIntervalSince(started))s")
        print("Transcript parser, streaming, copy and layout checks passed")
    }
}
'''


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "Native transcript tests need macOS")
class ChatTranscriptTests(unittest.TestCase):
    def test_parser_streaming_copy_budgets_and_column_layout(self):
        source = Path(__file__).parent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Stubs.swift").write_text(STUBS)
            (root / "Cases.swift").write_text(CASES)
            binary = root / "transcript-tests"
            compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-target", platform.machine() + "-apple-macosx14.0", "-module-cache-path", str(root / "cache"),
                str(root / "Stubs.swift"), str(source / "ChatFonts.swift"), str(source / "ChatTranscriptText.swift"),
                str(root / "Cases.swift"), "-framework", "AppKit", "-framework", "SwiftUI", "-o", str(binary)],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertIn("Transcript parser, streaming, copy and layout checks passed", ran.stdout)


if __name__ == "__main__":
    unittest.main()
