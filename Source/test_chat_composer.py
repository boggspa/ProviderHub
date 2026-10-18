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

/// The drafts a composer binds, one per chat as ChatModel keeps them, and
/// what each send took.
final class Draft: ObservableObject {
    @Published var drafts: [String: String] = [:]
    /// The chat whose draft is shown; nil is the draft shown with no chat.
    @Published var chat: String? = "A"
    @Published var enabled = true
    /// Bumped to make SwiftUI update the composer, as a streamed reply does.
    @Published var unrelated = 0
    var sent: [String] = []
    var text: String {
        get { drafts[chat ?? ""] ?? "" }
        set { drafts[chat ?? ""] = newValue }
    }
}

struct Composer: View {
    @ObservedObject var draft: Draft
    let members: [ChatMentionTarget]
    let menu: ChatMentionMenu
    @State private var height: CGFloat = 22
    var body: some View {
        VStack {
            Text("\(draft.unrelated)")
            ComposerTextView(text: $draft.text, height: $height, placeholder: "Message", enabled: draft.enabled,
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

        // Deleting the last chat mid-composition shows the draft with no chat
        // and disables the composer in one update. The composition ends with
        // the deleted chat; none of it flows into the draft now shown.
        draft.text = ""; settle()
        compose("@中", "文")
        check(draft.text == "@中文", "the draft lagged a second composition: \(draft.text.debugDescription)")
        draft.drafts["A"] = nil; draft.chat = nil; draft.enabled = false; settle()
        check(!text.isEditable && text.string.isEmpty && !text.hasMarkedText() && (draft.drafts[""] ?? "").isEmpty,
              "a deleted chat's composition reached the next draft: \(text.string.debugDescription), \(String(describing: draft.drafts[""]))")

        // Disabled mid-composition in the same chat (a lost connection), the
        // draft keeps exactly what was shown, and the @ list closes.
        draft.chat = "B"; draft.enabled = true; settle()
        window.makeFirstResponder(text)
        compose("@中", "文")
        draft.enabled = false; settle()
        check(draft.text == "@中文" && text.string == "@中文",
              "disabling lost a composition: \(text.string.debugDescription), \(draft.text.debugDescription)")
        draft.enabled = true; draft.text = ""; settle()
        window.makeFirstResponder(text)
        text.insertText("@S", replacementRange: none); settle()
        check(menu.current?.id == "s", "the @ list did not open for @S")
        draft.enabled = false; settle()
        check(menu.state == nil, "the @ list stayed open over a disabled composer")

        if !failures.isEmpty {
            FileHandle.standardError.write(Data(failures.joined(separator: "\n").utf8))
            exit(1)
        }
        print("composer cases passed")
    }
}
'''



PANE_FOCUS_CASES = r'''
import WebKit

struct FocusBrowser: NSViewRepresentable {
    let view: WKWebView
    func makeNSView(context: Context) -> WKWebView { view }
    func updateNSView(_ view: WKWebView, context: Context) {}
}

@main struct PaneFocusCases {
    @MainActor static func main() {
        NSApplication.shared.setActivationPolicy(.prohibited)
        var failures: [String] = []
        func check(_ yes: Bool, _ message: String) { if !yes { failures.append(message) } }
        let left = Draft(), right = Draft()
        right.chat = "B"
        let leftMenu = ChatMentionMenu(), rightMenu = ChatMentionMenu()
        let browser = WKWebView()
        let controller = ChatNativeWorkspaceController()
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1400, height: 1000),
                              styleMask: [.titled, .resizable], backing: .buffered, defer: false)
        window.contentViewController = controller
        window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
        window.orderFrontRegardless()
        var layout = ChatWorkspaceLayout.initial(chatID: "a")
        let a = layout.focusedPaneID
        layout.splitRight(a, assignment: .chat(id: "b"))
        let b = layout.focusedPaneID
        var request: (pane: UUID, token: UUID)?
        func settle() {
            for _ in 0..<8 {
                RunLoop.main.run(until: Date().addingTimeInterval(0.025))
                controller.view.layoutSubtreeIfNeeded()
            }
        }
        func update(_ focusedOnly: Bool = false, settleAfter: Bool = true) {
            controller.update(layout: layout, focused: layout.focusedPaneID, focusedOnly: focusedOnly,
                content: { _, assignment in
                    switch assignment {
                    case .chat(let chat):
                        return AnyView(Composer(draft: chat == "a" ? left : right, members: [],
                                                menu: chat == "a" ? leftMenu : rightMenu))
                    case .browser: return AnyView(FocusBrowser(view: browser))
                    case nil: return AnyView(Text("Open a chat"))
                    }
                }, onFocus: { _ in }, onDrop: { _, _, _ in }, onResize: { _, _ in },
                keyboardFocusRequest: request)
            if settleAfter { settle() }
        }
        func findEditor(_ view: NSView) -> ComposerTextView.SendTextView? {
            if let editor = view as? ComposerTextView.SendTextView { return editor }
            return view.subviews.lazy.compactMap(findEditor).first
        }
        func editor(_ pane: UUID) -> ComposerTextView.SendTextView {
            guard let leaf = controller.leafController(pane), let editor = findEditor(leaf.view) else {
                fatalError("Missing composer for pane \(pane)")
            }
            return editor
        }
        func navigate(_ pane: UUID, maximized: Bool = false) {
            layout.focus(paneID: pane)
            request = (pane, UUID())
            update(maximized)
        }
        update()
        window.makeFirstResponder(editor(a))
        navigate(b)
        check(window.firstResponder === editor(b), "next pane left the caret in the old composer")
        let destination = window.firstResponder as! ComposerTextView.SendTextView
        destination.insertText("for the right chat", replacementRange: NSRange(location: NSNotFound, length: 0))
        settle()
        check(left.text.isEmpty && right.text == "for the right chat", "typing edited the wrong chat")
        let send = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: .command, timestamp: 0,
                                   windowNumber: window.windowNumber, context: nil, characters: "\r",
                                   charactersIgnoringModifiers: "\r", isARepeat: false, keyCode: 36)!
        destination.keyDown(with: send)
        settle()
        check(left.sent.isEmpty && right.sent == ["for the right chat"], "Return sent to the wrong chat")

        // A mouse focus and subsequent streamed update do not replay an old
        // keyboard request. Asking for that same destination again still works.
        layout.focus(paneID: a)
        update()
        window.makeFirstResponder(editor(a))
        right.unrelated += 1
        update()
        check(window.firstResponder === editor(a), "a streamed update stole keyboard focus")
        navigate(b)
        check(window.firstResponder === editor(b), "a repeated destination did not receive keyboard focus")

        // A click that supersedes a queued navigation cancels the old handoff.
        layout.focus(paneID: a)
        request = (a, UUID())
        update(settleAfter: false)
        layout.focus(paneID: b)
        update(settleAfter: false)
        window.makeFirstResponder(editor(b))
        settle()
        check(window.firstResponder === editor(b), "a stale navigation stole focus after a newer selection")

        navigate(a, maximized: true)
        check(window.firstResponder === editor(a), "maximized navigation did not focus the mounted composer")
        navigate(b, maximized: true)
        check(window.firstResponder === editor(b), "cycling maximized panes left focus in a detached composer")

        // An empty pane has no composer, but must stop the previous chat from
        // receiving text. Browser panes direct their keyboard input to WebKit.
        layout.splitBelow(b)
        let empty = layout.focusedPaneID
        navigate(empty)
        let emptyRoot = controller.leafController(empty)!.view
        let emptyResponder = window.firstResponder as? NSView
        check(emptyResponder === emptyRoot || emptyResponder?.isDescendant(of: emptyRoot) == true,
              "empty navigation left the old composer active")
        window.makeFirstResponder(editor(a))
        layout.assignBrowser(paneID: empty, surface: "a")
        navigate(empty)
        let browserResponder = window.firstResponder as? NSView
        check(browserResponder === browser || browserResponder?.isDescendant(of: browser) == true,
              "browser navigation did not transfer keyboard focus")

        if !failures.isEmpty {
            FileHandle.standardError.write(Data(failures.joined(separator: "\n").utf8))
            exit(1)
        }
        print("pane keyboard focus cases passed")
    }
}
'''

@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "the native composer needs macOS")
class ComposerTests(unittest.TestCase):
    def test_compositions_stay_in_the_draft_and_send_as_shown(self):
        self.run_cases(CASES, "composer cases passed")

    def test_keyboard_navigation_types_and_sends_in_the_destination_pane(self):
        cases = CASES.split("@main struct Cases", 1)[0] + PANE_FOCUS_CASES
        self.run_cases(cases, "pane keyboard focus cases passed", workspace=True)

    def run_cases(self, cases, expected, workspace=False):
        source = Path(__file__).parent
        files = ["ChatFonts.swift", "ChatMentions.swift", "ChatSelectableText.swift", "ChatTranscriptText.swift", "ChatComposerText.swift"]
        if workspace:
            files += ["ChatWorkspaceLayout.swift", "ChatSplitWorkspace.swift"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Stubs.swift").write_text(STUBS)
            (root / "Cases.swift").write_text(cases)
            binary = root / "composer-tests"
            compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-target", platform.machine() + "-apple-macosx14.0", "-module-cache-path", str(root / "cache"),
                str(root / "Stubs.swift"), *(str(source / name) for name in files), str(root / "Cases.swift"),
                "-framework", "AppKit", "-framework", "SwiftUI", "-o", str(binary)], capture_output=True, text=True, timeout=120)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn(expected, ran.stdout)


if __name__ == "__main__": unittest.main()
