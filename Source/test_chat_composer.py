"""Drive the real Chat composer text view: input-method compositions, @Name tints and sends."""
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_chat_transcript import STUBS


CASES = r'''
import AppKit
import SwiftUI
import Combine

/// The draft a composer binds, and what each send took.
final class Draft: ObservableObject {
    @Published var text = ""
    /// Bumped to make SwiftUI update the composer, as a streamed reply does.
    @Published var unrelated = 0
    var sent: [String] = []
}

struct Composer: View {
    @ObservedObject var draft: Draft
    let members: [ChatMentionTarget]
    let menu: ChatMentionMenu
    @State private var height: CGFloat = 22
    var body: some View {
        VStack {
            Text("\(draft.unrelated)")
            ComposerTextView(text: $draft.text, height: $height, placeholder: "Message", enabled: true,
                             onSend: { draft.sent.append(draft.text); draft.text = "" },
                             mentions: members, mentionMenu: menu)
                .frame(width: 360, height: 60)
        }
    }
}

@main struct Cases {
    @MainActor static func main() {
        var failures: [String] = []
        func check(_ yes: Bool, _ text: String) { if !yes { failures.append(text) } }
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        let draft = Draft(), menu = ChatMentionMenu()
        let members = [ChatMentionTarget(id: "zh", name: "中文", route: "r1", accent: .systemPurple),
                       ChatMentionTarget(id: "s", name: "Sol", route: "r2", accent: .systemOrange)]
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 400, height: 120), styleMask: [.titled], backing: .buffered, defer: false)
        let host = NSHostingView(rootView: Composer(draft: draft, members: members, menu: menu))
        window.contentView = host
        window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
        window.orderFrontRegardless()
        func settle() { for _ in 0..<5 { RunLoop.main.run(until: Date().addingTimeInterval(0.03)); host.layoutSubtreeIfNeeded() } }
        settle()
        func composer(_ view: NSView) -> ComposerTextView.SendTextView? {
            if let text = view as? ComposerTextView.SendTextView { return text }
            return view.subviews.lazy.compactMap(composer).first
        }
        guard let text = composer(host) else { fatalError("The composer text view was not found") }
        window.makeFirstResponder(text)
        let none = NSRange(location: NSNotFound, length: 0)
        // What an input method does: commit some text, then show a composition.
        func compose(_ committed: String, _ marked: String) {
            text.insertText(committed, replacementRange: none); settle()
            text.setMarkedText(marked, selectedRange: NSRange(location: (marked as NSString).length, length: 0), replacementRange: none)
        }
        func tinted(_ location: Int) -> Bool { text.textStorage?.attribute(.chatCodeChip, at: location, effectiveRange: nil) != nil }

        // AppKit reports no text change for a composition, yet it is in the
        // draft as soon as it shows, and an update meanwhile (a streamed
        // reply, the @ list closing) leaves it alone.
        compose("@中", "文")
        check(draft.text == "@中文", "the draft lagged a composition: \(draft.text.debugDescription)")
        draft.unrelated += 1; settle()
        check(text.string == "@中文" && text.hasMarkedText(), "an update erased the composition: \(text.string.debugDescription)")

        // ⌘Return sends what is shown, with the tag the composition completes,
        // and the cleared draft ends the composition.
        let commandReturn = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: .command, timestamp: 0,
                                             windowNumber: window.windowNumber, context: nil, characters: "\r",
                                             charactersIgnoringModifiers: "\r", isARepeat: false, keyCode: 36)!
        text.keyDown(with: commandReturn); settle()
        check(draft.sent == ["@中文"], "⌘Return dropped the composition: \(draft.sent)")
        check(ChatMentions.resolve(draft.sent.first ?? "", members: members).map(\.target.id) == ["zh"], "the sent tag reaches no member")
        check(text.string.isEmpty && !text.hasMarkedText() && draft.text.isEmpty, "the sent composition stayed: \(text.string.debugDescription)")

        // A tag is tinted once the composition completing it is committed, never during it.
        compose("ask @中", "文"); settle()
        check(!tinted(5), "a tag was tinted mid-composition")
        text.unmarkText(); settle()
        check(draft.text == "ask @中文" && tinted(5), "a committed tag stayed untinted: \(draft.text.debugDescription)")

        // A draft replaced under a composition (another chat, a refused send's
        // restore) ends it, and the old text never flows back into the draft.
        compose(" and ", "x")
        draft.text = "restored"; settle()
        check(text.string == "restored" && !text.hasMarkedText() && draft.text == "restored",
              "a replaced draft kept the old composition: \(text.string.debugDescription), \(draft.text.debugDescription)")

        if !failures.isEmpty {
            FileHandle.standardError.write(Data(failures.joined(separator: "\n").utf8))
            exit(1)
        }
        print("composer cases passed")
    }
}
'''


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "the native composer needs macOS")
class ComposerTests(unittest.TestCase):
    def test_compositions_stay_in_the_draft_and_send_as_shown(self):
        source = Path(__file__).parent
        files = ["ChatFonts.swift", "ChatMentions.swift", "ChatSelectableText.swift", "ChatTranscriptText.swift", "ChatComposerText.swift"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Stubs.swift").write_text(STUBS)
            (root / "Cases.swift").write_text(CASES)
            binary = root / "composer-tests"
            compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-target", platform.machine() + "-apple-macosx14.0", "-module-cache-path", str(root / "cache"),
                str(root / "Stubs.swift"), *(str(source / name) for name in files), str(root / "Cases.swift"),
                "-framework", "AppKit", "-framework", "SwiftUI", "-o", str(binary)], capture_output=True, text=True, timeout=120)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn("composer cases passed", ran.stdout)


if __name__ == "__main__": unittest.main()
