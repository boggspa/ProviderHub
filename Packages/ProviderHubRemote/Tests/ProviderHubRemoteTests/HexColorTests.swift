import Foundation
import SwiftUI
import XCTest
@testable import ProviderHubRemote

/// Colour codes as Codex Desktop finds them, and swatches that leave the
/// text itself untouched.
final class HexColorTests: XCTestCase {
    private func codes(_ text: String, skippingFences: Bool = false) -> [String] {
        ChatHexColor.find(in: text, skippingFences: skippingFences).map { (text as NSString).substring(with: $0.range) }
    }

    func testStandaloneSixDigitCodesAreColours() {
        XCTAssertEqual(ChatHexColor.find(in: "#121212"), [ChatHexColor(range: NSRange(location: 0, length: 7), rgb: 0x121212)])
        XCTAssertEqual(codes("Use #ff0000, then #00FF00.\n#0a0B0c!"), ["#ff0000", "#00FF00", "#0a0B0c"])
        XCTAssertEqual(codes("tab\t#abcdef and\u{00A0}#fedcba"), ["#abcdef", "#fedcba"])
        // UTF-16 ranges, past a surrogate pair.
        XCTAssertEqual(ChatHexColor.find(in: "🎨 #a1b2c3").first?.range, NSRange(location: 3, length: 7))
        XCTAssertEqual(ChatHexColor.find(in: "#A1B2C3").first?.rgb, 0xA1B2C3)
    }

    func testCodesInsideWordsUrlsAndOtherFormsAreNot() {
        for text in ["a#ff0000", "(#ff0000)", "\"#ff0000\"", "https://example.com/page#abcdef", "x/#abcdef",
                     "#ff0000ff", "#ff00001", "#ff0000_", "#ff0000x", "#ff000g", "#fff", "#bad", "#123", "#12345", "##ff0000",
                     "# Title", "## Colours", ""] {
            XCTAssertEqual(codes(text), [], text)
        }
        // A heading's own text may still name a colour.
        XCTAssertEqual(codes("## Accent #ff6600"), ["#ff6600"])
    }

    func testIssueAndPullRequestReferencesAreNotColours() {
        for text in ["issue #123456", "Issue: #abcdef", "PR #abcdef", "see pr  #a1b2c3", "PR\t#123456", "#123456 landed in PR 12",
                     "the prs #123456"] {
            XCTAssertEqual(codes(text), [], text)
        }
        XCTAssertEqual(codes("reissue #abcdef"), ["#abcdef"])
        XCTAssertEqual(codes("PRs #abcdef"), ["#abcdef"])
        XCTAssertEqual(codes("#12345a landed in PR 12"), ["#12345a"], "only an all-digit code reads as a number")
        XCTAssertEqual(codes("PR 12 is merged.\nThe border is #123456"), ["#123456"], "the PR is on another line")
        XCTAssertEqual(codes("PR\n#abcdef"), ["#abcdef"])
    }

    func testDraftsSkipFencedCode() {
        let draft = "```css\n.a { color: #ff0000 }\n```\nThen #00ff00\n~~~~\n#0000ff\n~~~\n#ffffff\n~~~~\nlast #121212"
        XCTAssertEqual(codes(draft, skippingFences: true), ["#00ff00", "#121212"])
        XCTAssertEqual(codes(draft), ["#ff0000", "#00ff00", "#0000ff", "#ffffff", "#121212"])
        XCTAssertEqual(codes("Open\n```\n#ff0000", skippingFences: true), [], "an unclosed fence runs to the end")
        XCTAssertEqual(codes("    ```\n#ff0000", skippingFences: true), ["#ff0000"], "four spaces is not a fence")
        XCTAssertEqual(codes("```x```\n#ff0000", skippingFences: true), ["#ff0000"], "inline code, not a fence")
    }

    func testRenderedMarkdownSkipsLinksAndCodeThatHoldsMore() throws {
        let markdown = "Use `#ff0000` and `color: #00ff00` and [#0000ff](https://example.com) and **#ffffff** and color:`#121212`."
        let text = try AttributedString(markdown: markdown, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))
        let plain = String(text.characters) as NSString
        XCTAssertEqual(ChatHexColor.find(in: text).map { plain.substring(with: $0.range) }, ["#ff0000", "#ffffff"])
    }

    func testSwatchMetricsAreWholePoints() {
        for size: CGFloat in [10, 11.5, 13, 14, 17, 22, 34] {
            let parts = [ChatHexSwatch.lead(size), ChatHexSwatch.side(size), ChatHexSwatch.trail(size), ChatHexSwatch.drop(size)]
            XCTAssertEqual(parts, parts.map { $0.rounded() }, "\(size)")
            XCTAssertEqual(ChatHexSwatch.advance(size), parts[0] + parts[1] + parts[2])
        }
        XCTAssertEqual(ChatHexSwatch.side(14), 11)
        let image = ChatHexSwatch.image(ChatHexColor(range: NSRange(location: 0, length: 7), rgb: 0x121212), size: 14, dark: true)
        XCTAssertEqual(image.size, CGSize(width: ChatHexSwatch.advance(14), height: ChatHexSwatch.side(14)))
    }

    func testKerningLeavesTheCharactersAsWritten() throws {
        let source = "**User:** pick #121212 or `#f4f4f6`, not #fff"
        let text = try AttributedString(markdown: source, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))
        let colors = ChatHexColor.find(in: text)
        XCTAssertEqual(colors.count, 2)
        let kerned = ChatSharedHexText.kerned(text, colors, size: 15)
        XCTAssertEqual(String(kerned.characters), String(text.characters))
        let kerns = kerned.runs.compactMap { run in
            run[AttributeScopes.SwiftUIAttributes.KerningAttribute.self].map { (String(kerned[run.range].characters), $0) }
        }
        XCTAssertEqual(kerns.map(\.0), ["2", "6"], "only each code's last character is kerned")
        XCTAssertEqual(kerns.map(\.1), [ChatHexSwatch.advance(15), ChatHexSwatch.advance(15)])
        // Plain text keeps its exact characters whether or not it has codes.
        let draft = AttributedString("Draft with #00ff00 and \u{1F3A8}")
        XCTAssertEqual(String(ChatSharedHexText.kerned(draft, ChatHexColor.find(in: draft), size: 13).characters),
                       "Draft with #00ff00 and \u{1F3A8}")
    }

    func testRepeatedLookupsAgree() {
        let text = String(repeating: "Body #0a0a0a text. ", count: 200)
        let first = ChatHexColor.find(in: text)
        XCTAssertEqual(first.count, 200)
        XCTAssertEqual(ChatHexColor.find(in: text), first)
        XCTAssertEqual(ChatHexColor.find(in: text, skippingFences: true), first)
    }
}
