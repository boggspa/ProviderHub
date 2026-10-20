"""Colour-code swatches: one detector shared by the Mac and the companion, drawn
beside the text without ever changing what is sent, selected or copied."""
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).parent
COMPANION = SOURCE.parent / "Packages" / "ProviderHubRemote" / "Sources" / "ProviderHubRemote"


CASES = r'''
import AppKit
import SwiftUI

@main struct Cases {
    static func main() throws {
        func check(_ condition: Bool, _ message: String) { if !condition { fatalError(message) } }
        func codes(_ text: String, fences: Bool = false) -> [String] {
            ChatHexColor.find(in: text, skippingFences: fences).map { (text as NSString).substring(with: $0.range) }
        }
        check(codes("Rim #121212, fill #F4F4F6.\n#0a0B0c") == ["#121212", "#F4F4F6", "#0a0B0c"], "Standalone codes missed")
        for text in ["a#ff0000", "(#ff0000)", "https://example.com/#abcdef", "#ff0000ff", "#fff", "#123", "#ff0000x",
                     "issue #123456", "PR #abcdef", "#123456 landed in PR 12", "# Heading"] {
            check(codes(text).isEmpty, "Not a colour: " + text)
        }
        check(codes("#12345a landed in PR 12") == ["#12345a"], "Only an all-digit code is a PR number")
        check(codes("```css\n#ff0000\n```\nThen #00ff00", fences: true) == ["#00ff00"], "A fenced code took a swatch")
        let markdown = try AttributedString(markdown: "`#ff0000`, `color: #00ff00`, [#0000ff](https://example.com) and #ffffff",
                                            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))
        let plain = String(markdown.characters) as NSString
        check(ChatHexColor.find(in: markdown).map { plain.substring(with: $0.range) } == ["#ff0000", "#ffffff"],
              "Rendered Markdown: links and longer code spans take no swatch")
        let kerned = ChatSharedHexText.kerned(markdown, ChatHexColor.find(in: markdown), size: 13)
        check(String(kerned.characters) == String(markdown.characters), "Kerning changed the characters")
        print("Hex colour detection checks passed")
    }
}
'''


class HexColorSourceTests(unittest.TestCase):
    def read(self, path):
        return Path(path).read_text()

    def test_mac_compiles_the_package_detector_through_a_symlink(self):
        # The Mac app cannot import the package; like the other ChatShared
        # files, its copy is a link to the package's, so they cannot drift.
        link = SOURCE / "ChatSharedHexColor.swift"
        self.assertTrue(link.is_symlink(), "Source/ChatSharedHexColor.swift must link to the package copy")
        self.assertEqual(link.resolve(), (COMPANION / "ChatSharedHexColor.swift").resolve())
        self.assertIn('"$SOURCE_DIR/ChatSharedHexColor.swift"', self.read(SOURCE / "build.sh"))

    def test_detection_is_precompiled_cached_and_matches_codex(self):
        shared = self.read(COMPANION / "ChatSharedHexColor.swift")
        self.assertIn('private static let code = try! NSRegularExpression(pattern: #"(?<![^\\s])#[0-9A-Fa-f]{6}(?![A-Za-z0-9_])"#)', shared)
        self.assertIn("NSCache<NSString, Found>", shared)
        self.assertIn('guard text.utf8.contains(UInt8(ascii: "#")) else { return [] }', shared)
        self.assertNotIn("#available", shared)

    def test_mac_swatches_are_attributes_never_characters(self):
        selectable = self.read(SOURCE / "ChatSelectableText.swift")
        self.assertIn("if swatches { ChatLayoutManager.markSwatches(result, ChatHexColor.find(in: text)) }", selectable)
        self.assertIn("override func drawGlyphs(forGlyphRange glyphsToShow: NSRange, at origin: NSPoint)", selectable)
        composer = self.read(SOURCE / "ChatComposerText.swift")
        self.assertIn("let colors = ChatHexColor.find(in: view.string, skippingFences: true)", composer)
        self.assertIn("for key in [NSAttributedString.Key.chatCodeChip, .kern, .chatHexSwatch]", composer)
        self.assertIn("ChatLayoutManager.markSwatches(storage, colors)", composer)
        transcript = self.read(SOURCE / "ChatTranscriptText.swift")
        # Fenced code is source, not a colour to show.
        self.assertIn("message: message, swatches: false)", transcript)
        self.assertIn("ChatSharedHexText(text: cell.formatted, size: nativeFont.pointSize)", transcript)
        for path in [SOURCE / "ChatSelectableText.swift", SOURCE / "ChatComposerText.swift",
                     COMPANION / "ChatSharedHexColor.swift", COMPANION / "CompanionMentionField.swift"]:
            self.assertNotIn("NSTextAttachment", self.read(path), path.name)

    def test_companion_transcript_and_composer_draw_swatches(self):
        transcript = self.read(COMPANION / "CompanionTranscript.swift")
        self.assertIn("ChatSharedHexText(text: (try? AttributedString(markdown: source", transcript)
        self.assertFalse("LazyVStack" in transcript, "the companion transcript went lazy again")
        mentions = self.read(COMPANION / "CompanionMentions.swift")
        self.assertIn("ChatSharedHexText(text: CompanionMentions.labelled(", mentions)
        field = self.read(COMPANION / "CompanionMentionField.swift")
        self.assertIn("storage.addAttribute(.kern, value: ChatHexSwatch.advance(font.pointSize), range: last)", field)
        self.assertIn("view.swatches = (view.text, colors)", field)
        # Text input geometry, never `layoutManager`, which would drop the
        # composer to TextKit 1.
        self.assertIn("let line = firstRect(for: range)", field)
        self.assertNotIn("layoutManager", field.split("final class MentionUITextView", 1)[1].split("#else", 1)[0])
        self.assertNotIn("#available", field)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "Native detection checks need macOS")
    def test_detection_cases_natively(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Cases.swift").write_text(CASES)
            binary = root / "hex-tests"
            compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-target", platform.machine() + "-apple-macosx14.0", "-module-cache-path", str(root / "cache"),
                str(SOURCE / "ChatSharedHexColor.swift"), str(root / "Cases.swift"),
                "-framework", "AppKit", "-framework", "SwiftUI", "-o", str(binary)], capture_output=True, text=True, timeout=120)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertIn("Hex colour detection checks passed", ran.stdout)


if __name__ == "__main__": unittest.main()
