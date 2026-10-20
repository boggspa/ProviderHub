"""Blackboard attachments in the inspectors' Team tab: two rows, same tiles.

The Mac inspector and the iPhone companion share ChatSharedBlackboardTiles.
The layout test compiles that view verbatim with plain fixed-size tiles and
measures it offscreen; the source checks pin how both inspectors use it.
"""
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).parent
COMPANION = SOURCE.parent / "Packages" / "ProviderHubRemote" / "Sources" / "ProviderHubRemote"


def shared_tiles():
    """The shared view and its layout enum, cut from ChatSharedInspector.swift."""
    text = (COMPANION / "ChatSharedInspector.swift").read_text()
    start = text.index("struct ChatSharedBlackboardTiles<")
    layout = text.index("enum ChatSharedBlackboardTileLayout {", start)
    return text[start:text.index("\n}\n", layout) + 3]


CASES = r'''
import AppKit
import SwiftUI

struct Frames: PreferenceKey {
    static let defaultValue: [Int: CGRect] = [:]
    static func reduce(value: inout [Int: CGRect], nextValue: () -> [Int: CGRect]) { value.merge(nextValue()) { $1 } }
}

@MainActor var frames: [Int: CGRect] = [:]

@MainActor func measure(_ count: Int, width: CGFloat) -> (size: CGSize, document: CGFloat?) {
    frames = [:]
    let view = ChatSharedBlackboardTiles(items: Array(0..<count), id: \.self) { index in
        Color.gray.frame(width: 92, height: 85)
            .background(GeometryReader { proxy in Color.clear.preference(key: Frames.self, value: [index: proxy.frame(in: .named("tiles"))]) })
    }
    .coordinateSpace(name: "tiles")
    .onPreferenceChange(Frames.self) { value in MainActor.assumeIsolated { frames = value } }
    .frame(width: width, alignment: .leading)
    let hosting = NSHostingView(rootView: view)
    let window = NSWindow(contentRect: NSRect(x: -20000, y: -20000, width: width, height: 400), styleMask: [.borderless], backing: .buffered, defer: false)
    window.contentView = hosting
    hosting.layoutSubtreeIfNeeded()
    RunLoop.main.run(until: Date().addingTimeInterval(0.2))
    let size = hosting.fittingSize
    func scrollers(_ view: NSView) -> [NSScrollView] { ((view as? NSScrollView).map { [$0] } ?? []) + view.subviews.flatMap(scrollers) }
    let document = scrollers(hosting).first?.documentView?.frame.width
    window.contentView = nil
    return (size, document)
}

@main struct Cases {
    @MainActor static func main() {
        func check(_ yes: Bool, _ text: @autoclosure () -> String) { if !yes { fatalError(text()) } }
        _ = NSApplication.shared
        let one: CGFloat = 85 + 4, two: CGFloat = 85 * 2 + 8 + 4
        // A lone tile, and as many as fit, stay on one row without a scroller.
        var result = measure(1, width: 300)
        check(result.size.height == one && result.document == nil, "one file: \(result)")
        result = measure(3, width: 300)
        check(result.size.height == one && result.document == nil, "three files that fit: \(result)")
        check(frames[2]!.minY == frames[0]!.minY && frames[2]!.minX == 200, "fitting files left one row: \(frames)")
        // A lone tile too wide for the pane scrolls on one row, never an empty second one.
        result = measure(1, width: 60)
        check(result.size.height == one, "narrow single file: \(result)")
        // Past one row: two rows, filled top to bottom then left to right.
        result = measure(5, width: 300)
        check(result.size.height == two && result.document == 92 * 3 + 8 * 2, "five files: \(result)")
        check(frames[1]!.minX == frames[0]!.minX && frames[1]!.minY == frames[0]!.maxY + 8, "second file is not under the first: \(frames)")
        check(frames[2]!.minX == frames[0]!.maxX + 8 && frames[2]!.minY == frames[0]!.minY, "third file does not start the next column: \(frames)")
        check(frames[4]!.minY == frames[0]!.minY, "odd last file is not at the top of its column: \(frames)")
        // Every tile keeps its own size.
        check(Set(frames.values.map(\.size)) == [CGSize(width: 92, height: 85)], "tiles were resized: \(frames)")
        // The inspector lists the first 32: sixteen columns of two.
        result = measure(40, width: 300)
        check(result.size.height == two && result.document == 92 * 16 + 8 * 15, "forty files: \(result)")
        check(ChatSharedBlackboardTileLayout.limit == 32 && ChatSharedBlackboardTileLayout.rows == 2, "limit or rows changed")
        print("Blackboard tile rows passed")
    }
}
'''


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "SwiftUI layout test needs macOS")
class BlackboardTileLayoutTests(unittest.TestCase):
    def test_shared_tiles_use_one_row_until_full_then_two_rows_column_by_column(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Cases.swift").write_text("import SwiftUI\n" + shared_tiles() + CASES)
            binary = root / "tile-tests"
            compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-target", platform.machine() + "-apple-macosx14.0", "-module-cache-path", str(root / "cache"),
                str(root / "Cases.swift"), "-framework", "AppKit", "-framework", "SwiftUI", "-o", str(binary)],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stderr + ran.stdout)
            self.assertIn("Blackboard tile rows passed", ran.stdout)


class BlackboardTileSourceTests(unittest.TestCase):
    def test_shared_view_has_no_fixed_empty_rows_or_lazy_grid(self):
        tiles = shared_tiles()
        self.assertIn("ViewThatFits(in: .horizontal)", tiles)
        self.assertIn("ScrollView(.horizontal, showsIndicators: false)", tiles)
        self.assertIn("ChatSharedBlackboardTileLayout.columns(shown)", tiles)
        self.assertIn("items.prefix(ChatSharedBlackboardTileLayout.limit)", tiles)
        # Fixed grid rows would reserve an empty second row for a single file.
        self.assertNotIn("LazyHGrid", tiles)
        self.assertNotIn("GridItem", tiles)

    def test_mac_inspector_uses_two_rows_with_the_same_compact_tiles(self):
        text = (SOURCE / "ChatBlackboard.swift").read_text()
        attachments = text[text.index("struct ChatBlackboardAttachments: View {"):text.index("private struct ChatBlackboardTile: View {")]
        compact, window = attachments.split("} else {", 1)
        self.assertIn("ChatSharedBlackboardTiles(items: snapshot.attachments, id: \\.id)", compact)
        self.assertIn("compact: true", compact)
        self.assertNotIn("prefix(24)", text)
        self.assertNotIn("ScrollView(.horizontal", compact)
        # Links and every file kind share the rows: nothing is filtered by kind.
        self.assertNotIn(".filter", compact)
        # The window keeps its adaptive grid of large tiles.
        self.assertIn("LazyVGrid(columns: [GridItem(.adaptive(minimum: 150)", window)
        self.assertIn("compact: false", window)
        # Thumbnail size is unchanged: 92 x 54 in the inspector, 150 x 92 in the window.
        self.assertIn("private var width: CGFloat { compact ? 92 : 150 }", text)
        self.assertIn("private var height: CGFloat { compact ? 54 : 92 }", text)
        # Click, hover help and the context menu stay on the tile.
        tile = text[text.index("private struct ChatBlackboardTile: View {"):]
        for needle in ["Button(action: open) { preview }", ".help(file.url ?? file.name)", ".contextMenu {", 'Button("Open")',
                       'Button("Show in Finder")', 'Button("Copy Link")', 'Button("Remove from Blackboard", role: .destructive)']:
            self.assertIn(needle, tile)

    def test_mac_inspector_counts_attachments_and_opens_the_board_past_the_limit(self):
        section = (SOURCE / "ChatBlackboard.swift").read_text().split("struct ChatBlackboardBoard: View {", 1)[0]
        self.assertIn('Text("Attachments · \\(snapshot.attachments.count)")', section)
        self.assertRegex(section, re.compile(r"if snapshot\.attachments\.count > ChatSharedBlackboardTileLayout\.limit \{\s*"
            r'Button\("Show all \\\(snapshot\.attachments\.count\) attachments"\) \{ ChatBlackboardWindows\.shared\.show\(model: model, chat: chat\) \}'))

    def test_companion_team_tab_matches_with_its_own_tile_size(self):
        text = (COMPANION / "CompanionBlackboardSummary.swift").read_text()
        summary = text[text.index("struct CompanionBlackboardSummary: View {"):text.index("private struct CompanionLinkTile: View {")]
        self.assertIn("ChatSharedBlackboardTiles(items: attachments, id: \\.self)", summary)
        self.assertNotIn("ScrollView(.horizontal)", summary)
        self.assertIn('Text("Attachments · \\(attachments.count)")', summary)
        self.assertIn('if attachments.count > ChatSharedBlackboardTileLayout.limit { Button("Show all \\(attachments.count) attachments", action: expand)', summary)
        # Links become tiles the size of CompanionFileTile so the rows stay even.
        preview = (COMPANION / "CompanionFilePreview.swift").read_text()
        self.assertIn(".frame(width: 108, height: 64).clipShape(RoundedRectangle(cornerRadius: 5))", preview)
        self.assertIn("}.frame(width: 108).contentShape(Rectangle())", preview)
        link = text[text.index("private struct CompanionLinkTile: View {"):]
        self.assertIn("Link(destination: url)", link)
        self.assertIn(".frame(width: 108, height: 64)", link)
        self.assertIn("}.frame(width: 108).contentShape(Rectangle())", link)


if __name__ == "__main__":
    unittest.main()
