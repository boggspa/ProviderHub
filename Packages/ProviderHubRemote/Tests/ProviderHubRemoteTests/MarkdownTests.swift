import XCTest
@testable import ProviderHubRemote

/// The companion transcript's block grammar. Each case checks what lands in
/// every cell and code body, not only that a table or fence was found.
final class MarkdownTests: XCTestCase {
    func testEscapedPipesStayInsideTheirCell() {
        XCTAssertEqual(CompanionMarkdown.blocks("| Pattern | Meaning |\n| --- | --- |\n| a\\|b | Either a or b |"),
                       [.table(["Pattern", "Meaning"], [["a|b", "Either a or b"]])])
        XCTAssertEqual(CompanionMarkdown.blocks("| a\\|b | Kind |\n| --- | --- |\n| x | y |"),
                       [.table(["a|b", "Kind"], [["x", "y"]])])
        XCTAssertEqual(CompanionMarkdown.blocks("| Code | Note |\n| --- | --- |\n| `x\\|y` | escaped |\n| `p|q` | spanned |"),
                       [.table(["Code", "Note"], [["`x|y`", "escaped"], ["`p|q`", "spanned"]])])
        XCTAssertEqual(CompanionMarkdown.blocks("| A | B |\n| --- | --- |\n| ``a ` b | c`` | \\| |"),
                       [.table(["A", "B"], [["``a ` b | c``", "|"]])])
    }

    func testARowWiderThanTheHeaderKeepsItsText() {
        XCTAssertEqual(CompanionMarkdown.blocks("| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | x | y |"),
                       [.table(["A", "B"], [["1", "2"]]), .paragraph("| 3 | x | y |")])
        XCTAssertEqual(CompanionMarkdown.blocks("| A | B |\n| --- | --- |\n| value |"),
                       [.table(["A", "B"], [["value", ""]])])
    }

    func testOnlyAMatchingFenceClosesACodeBlock() {
        XCTAssertEqual(CompanionMarkdown.blocks("~~~~text\nalpha\n~~~ beta\ngamma\n~~~~\nAfter fence."),
                       [.code("text", "alpha\n~~~ beta\ngamma"), .paragraph("After fence.")])
        XCTAssertEqual(CompanionMarkdown.blocks("```swift\nlet a = 1\n``` not a close\n~~~\nlet b = 2\n```\nDone."),
                       [.code("swift", "let a = 1\n``` not a close\n~~~\nlet b = 2"), .paragraph("Done.")])
        XCTAssertEqual(CompanionMarkdown.blocks("```x```"), [.paragraph("```x```")])
        XCTAssertEqual(CompanionMarkdown.blocks("```sh\r\necho hi\r\n```\r\nok"), [.code("sh", "echo hi"), .paragraph("ok")])
    }

    func testAnUnfinishedFenceKeepsItsBodyWhileStreaming() {
        XCTAssertEqual(CompanionMarkdown.blocks("Intro\n\n```python\nprint(1)\n"),
                       [.paragraph("Intro"), .code("python", "print(1)")])
        XCTAssertEqual(CompanionMarkdown.blocks("Intro\n\n```python\nprint(1)\nprint(2)"),
                       [.paragraph("Intro"), .code("python", "print(1)\nprint(2)")])
    }
}
