"""Exercise manual scroll priority and deferred follow on a real AppKit viewport."""
from pathlib import Path
import platform
import subprocess
import tempfile
import unittest


CASES = r'''
import AppKit
import SwiftUI

struct Content: View {
    let follow: ChatTranscriptFollow
    var body: some View {
        ScrollView {
            VStack(spacing: 0) {
                ForEach(0..<80, id: \.self) { value in Text("Recorded row \(value)").frame(height: 28) }
            }.frame(maxWidth: .infinity).background(ChatTranscriptScrollObserver(follow: follow))
        }
    }
}

@main struct Cases {
    @MainActor static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        func check(_ condition: Bool, _ message: String) { if !condition { fatalError(message) } }
        func settle(_ seconds: Double = 0.08) { RunLoop.main.run(until: Date().addingTimeInterval(seconds)) }
        let follow = ChatTranscriptFollow()
        var actions = 0
        for _ in 0..<500 { follow.request { actions += 1 } }
        check(actions == 0, "follow ran synchronously inside the update transaction")
        settle()
        check(actions == 1, "stream burst scheduled multiple scrolls")
        follow.request { actions += 1 }
        follow.beginInteraction()
        settle()
        check(actions == 1 && !follow.followsLatest, "gesture did not cancel queued follow")
        follow.endInteraction(atBottom: false)
        for _ in 0..<100 { follow.request { actions += 1 } }
        settle()
        check(actions == 1, "scrolling slightly up lost the user's reading position")
        follow.resume(); follow.request { actions += 1 }
        settle()
        check(actions == 2, "Latest did not resume follow")
        follow.beginInteraction(); follow.endInteraction(atBottom: true)
        follow.request { actions += 1 }; settle()
        check(actions == 3, "manual return to bottom did not resume follow")
        follow.request { actions += 1 }; follow.cancel(); settle()
        check(actions == 3, "disappearing view retained a queued scroll")

        follow.request { actions += 1 }
        let host = NSHostingView(rootView: Content(follow: follow))
        let window = NSWindow(contentRect: NSRect(x:0,y:0,width:520,height:400), styleMask:[.titled], backing:.buffered, defer:false)
        window.contentView = host; window.setFrameOrigin(NSPoint(x:-20000,y:-20000)); window.orderFrontRegardless()
        settle(); host.layoutSubtreeIfNeeded(); settle()
        check(actions == 4, "first viewport attachment cancelled initial follow")
        func find(_ view: NSView) -> NSScrollView? {
            if let scroll = view as? NSScrollView { return scroll }
            return view.subviews.lazy.compactMap(find).first
        }
        guard let scroll = find(host), let document = scroll.documentView else { fatalError("native viewport missing") }
        let clip = scroll.contentView
        clip.scroll(to: NSPoint(x:0,y:document.bounds.maxY-clip.bounds.height)); scroll.reflectScrolledClipView(clip)
        let other = NSScrollView()
        NotificationCenter.default.post(name:NSScrollView.willStartLiveScrollNotification,object:other)
        check(follow.followsLatest, "another pane disabled this pane's follow")
        follow.request { actions += 1 }
        NotificationCenter.default.post(name:NSScrollView.willStartLiveScrollNotification,object:scroll)
        check(!follow.followsLatest, "probe did not attach to the transcript viewport")
        clip.scroll(to: NSPoint(x:0,y:document.bounds.maxY-clip.bounds.height-50)); scroll.reflectScrolledClipView(clip)
        NotificationCenter.default.post(name:NSScrollView.didEndLiveScrollNotification,object:scroll)
        settle()
        check(actions == 4 && !follow.followsLatest, "near-bottom reading was mistaken for follow mode")
        NotificationCenter.default.post(name:NSScrollView.willStartLiveScrollNotification,object:scroll)
        clip.scroll(to: NSPoint(x:0,y:document.bounds.maxY-clip.bounds.height)); scroll.reflectScrolledClipView(clip)
        NotificationCenter.default.post(name:NSScrollView.didEndLiveScrollNotification,object:scroll)
        check(follow.followsLatest, "scrolling all the way down did not restore follow")
        follow.request { actions += 1 }
        window.contentView = NSView()
        settle()
        check(actions == 4, "detached viewport retained an automatic scroll")
        window.orderOut(nil)
        print("TRANSCRIPT_SCROLL_PRIORITY_OK")
    }
}
'''


@unittest.skipUnless(platform.system() == "Darwin", "requires native AppKit scrolling")
class TranscriptScrollTests(unittest.TestCase):
    def test_manual_scrolling_cancels_coalesced_follow_and_preserves_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Cases.swift"
            source.write_text(CASES)
            executable = root / "scroll-test"
            built = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-target", platform.machine() + "-apple-macosx14.0",
                str(Path(__file__).with_name("ChatTranscriptScroll.swift")), str(source),
                "-framework", "AppKit", "-framework", "SwiftUI", "-o", str(executable)],
                capture_output=True, text=True, timeout=90)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn("TRANSCRIPT_SCROLL_PRIORITY_OK", ran.stdout)


class TranscriptStackTests(unittest.TestCase):
    """A lazy transcript stack only estimates rows it has not drawn. When a
    Team section above the viewport shrinks or leaves the paged tail, the
    viewport lands past the rows it drew and stays blank until the reader
    scrolls; a follow aimed at the estimated end also stops short of it."""

    ROOT = Path(__file__).resolve().parent.parent

    def section(self, path, start, end):
        text = (self.ROOT / path).read_text()
        return text.split(start, 1)[1].split(end, 1)[0]

    def test_mac_transcript_is_an_eager_paged_stack(self):
        transcript = self.section("Source/ChatWindow.swift", "struct ChatTranscript: View", "private struct ErrorRow")
        self.assertFalse("LazyVStack" in transcript, "the Chat transcript went lazy again")
        self.assertIn("return VStack(alignment: .leading, spacing: 0)", transcript)
        self.assertIn("visibleTail(segments, limit: visibleSegmentLimit", transcript)
        # Paged-in history goes again once the reader is back at the latest.
        self.assertIn(".onChange(of: follow.followsLatest) { _, follows in if follows { visibleSegmentLimit = 100 } }", transcript)

    def test_companion_transcript_is_an_eager_paged_stack_that_follows_by_intent(self):
        transcript = self.section("Packages/ProviderHubRemote/Sources/ProviderHubRemote/CompanionTranscript.swift",
                                  "struct CompanionTranscript: View", "/// A chat with no turns yet")
        self.assertFalse("LazyVStack" in transcript, "the companion transcript went lazy again")
        self.assertIn("VStack(alignment: .leading, spacing: 0)", transcript)
        self.assertIn("ChatTranscriptSegment.visibleTail(segments, limit: visibleSegmentLimit", transcript)
        # One outline per update; rows never rebuild it.
        self.assertEqual(transcript.count("outline(live: live)"), 1)
        # In an eager stack the end spacer appears once and never disappears,
        # so following cannot be inferred from it; the viewport is observed.
        self.assertFalse(".onDisappear { followsLatest = false }" in transcript, "following is inferred from the end spacer again")
        self.assertIn(".background(CompanionScrollProbe { placed($0, proxy: proxy) })", transcript)


if __name__ == "__main__": unittest.main()
