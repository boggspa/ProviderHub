"""Exercise the production Git indicator's SwiftUI task lifecycle offscreen."""
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

STUBS = r'''
import AppKit
import SwiftUI
enum HubTheme {
    enum Semantic {
        static func dynamic(light: NSColor, dark: NSColor) -> Color { Color(light) }
    }
}
struct ChatGitStatus {
    var files = 3; var added = 105; var deleted = 7; var ahead = 2
    var behind: Int? = 0; var upstream: String? = "origin/main"; var branch = "main"
}
struct HeaderSummary { var workspace: String }
@MainActor final class ChatModel: ObservableObject {
    @Published var selectedID: String? = "A"
    @Published var selected: HeaderSummary? = HeaderSummary(workspace: "/workspace")
    @Published var connected = false
    @Published var gitStatus: ChatGitStatus?
    var lookups = 0
    func refreshGitStatus() {
        guard connected, selectedID != nil else { return }
        lookups += 1; gitStatus = ChatGitStatus()
    }
}
'''

CASES = r'''
@main struct Cases {
    @MainActor static func main() async throws {
        _ = NSApplication.shared
        let model = ChatModel()
        let hosting = NSHostingView(rootView: ChatGitIndicator(model: model))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 360, height: 36),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = hosting
        hosting.layoutSubtreeIfNeeded()
        try await Task.sleep(nanoseconds: 300_000_000)
        precondition(model.lookups == 0, "Disconnected header attempted Git lookup")
        model.connected = true
        try await Task.sleep(nanoseconds: 400_000_000)
        precondition(model.lookups == 1 && model.gitStatus?.added == 105,
                     "Connection did not wake an initially empty Git indicator")
        model.selectedID = "B"
        try await Task.sleep(nanoseconds: 400_000_000)
        precondition(model.lookups == 2, "Another chat in the same workspace kept the old Git task")
        model.connected = false
        try await Task.sleep(nanoseconds: 100_000_000)
        model.connected = true
        try await Task.sleep(nanoseconds: 400_000_000)
        precondition(model.lookups == 3, "Reconnect did not restart Git lookup")
        model.selected = HeaderSummary(workspace: "/different")
        try await Task.sleep(nanoseconds: 400_000_000)
        precondition(model.lookups == 4, "Changing workspace kept the old Git task")
        window.contentView = nil
        print("Git indicator connection, chat and workspace lifecycle passed")
    }
}
'''


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "Native Git header test needs macOS")
class GitHeaderTests(unittest.TestCase):
    def test_empty_header_wakes_on_connection_and_tracks_chat_identity(self):
        # Compile the production view verbatim with a tiny observable model;
        # neither the worker nor unrelated settings controls belong in this test.
        view = "struct ChatGitIndicator: View {" + Path(__file__).with_name("ChatSettings.swift").read_text().split("struct ChatGitIndicator: View {", 1)[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Cases.swift").write_text(STUBS + view + CASES)
            binary = root / "git-header-tests"
            compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-target", platform.machine() + "-apple-macosx14.0", "-module-cache-path", str(root / "cache"),
                str(root / "Cases.swift"), "-framework", "AppKit", "-framework", "SwiftUI", "-o", str(binary)],
                capture_output=True, text=True, timeout=90)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertIn("Git indicator connection, chat and workspace lifecycle passed", ran.stdout)


if __name__ == "__main__":
    unittest.main()
