"""Verify the Glass-mode pane and transcript presentation on both platforms."""
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
    @MainActor static func main() throws {
        func check(_ condition: Bool, _ message: String) { if !condition { fatalError(message) } }
        // A dynamic NSColor answers for the current drawing appearance, which
        // a process with no run loop does not move when NSApp.appearance
        // changes. Resolve each read inside the appearance it asks about, the
        // same two appearances the Light/Dark setting uses.
        _ = NSApplication.shared
        func components(_ color: Color, _ appearance: NSAppearance) -> (CGFloat, CGFloat, CGFloat, CGFloat) {
            var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
            appearance.performAsCurrentDrawingAppearance {
                let resolved = NSColor(color)
                let rgb = resolved.usingColorSpace(.sRGB) ?? resolved
                rgb.getRed(&r, green: &g, blue: &b, alpha: &a)
            }
            return (r, g, b, a)
        }
        let dark = NSAppearance(named: .darkAqua)!
        let light = NSAppearance(named: .aqua)!

        // The transcript is paper, not glass: opaque #121212 on dark and its
        // light counterpart #F4F4F6 on light.
        func checkRGB(_ label: String, _ resolved: (CGFloat, CGFloat, CGFloat, CGFloat),
                      _ red: CGFloat, _ green: CGFloat, _ blue: CGFloat) {
            let channels = [resolved.0, resolved.1, resolved.2]
            let expected = [red, green, blue]
            for (index, channel) in channels.enumerated() {
                check(abs(channel - expected[index] / 255.0) < 0.004,
                      label + " channel " + String(index) + " moved to " + String(Double(channel * 255)))
            }
            check(abs(resolved.3 - 1.0) < 0.001, label + " is not opaque")
        }
        checkRGB("Dark transcript", components(HubTheme.Glass.transcript, dark), 18, 18, 18)
        checkRGB("Light transcript", components(HubTheme.Glass.transcript, light), 244, 244, 246)

        // The side panes take one step off the glass and keep their translucency.
        let darkPane = components(HubTheme.Glass.pane, dark)
        let lightPane = components(HubTheme.Glass.pane, light)
        check(abs(darkPane.3 - 0.07) < 0.004, "Dark pane wash moved")
        check(abs(lightPane.3 - 0.06) < 0.004, "Light pane wash moved")
        // White lifts the dark glass; black deepens the light glass.
        for channel in [darkPane.0, darkPane.1, darkPane.2] { check(channel > 0.99, "Dark pane is not a white lift") }
        for channel in [lightPane.0, lightPane.1, lightPane.2] { check(channel < 0.01, "Light pane is not a black deepen") }

        // A chosen transcript colour replaces the paper opaquely; it never
        // turns the transcript back into see-through glass.
        let chosen = ChatHues(transcript: ChatHue(hue: 215, intensity: 0.7))
        for appearance in [dark, light] {
            check(abs(components(chosen.transcriptFill!, appearance).3 - 1.0) < 0.001, "Custom transcript is not opaque")
        }

        // Solid mode keeps the plain window surface and paints no pane fill.
        check(HubTheme.WindowStyle.Mode.allCases.count == 2, "Window style cases changed")
        check(HubTheme.WindowStyle.Mode(rawValue: "glass") == .glass, "Glass style lost")
        print("Glass theme pane and transcript checks passed")
    }
}
'''


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "Native theme tests need macOS")
class GlassThemeTests(unittest.TestCase):
    def test_glass_pane_and_transcript_fills_resolve_per_appearance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Cases.swift").write_text(CASES)
            binary = root / "glass-theme-tests"
            compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-target", platform.machine() + "-apple-macosx14.0", "-module-cache-path", str(root / "cache"),
                str(SOURCE / "HubTheme.swift"), str(SOURCE / "HubGlass.swift"), str(SOURCE / "ChatHues.swift"), str(root / "Cases.swift"),
                "-framework", "AppKit", "-framework", "SwiftUI", "-o", str(binary)],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertIn("Glass theme pane and transcript checks passed", ran.stdout)


class GlassPresentationWiringTests(unittest.TestCase):
    """Both shells read the pane fills from the one place that defines them."""

    def test_chat_and_hub_windows_paint_the_panes_from_the_surface_style(self):
        chat = (SOURCE / "ChatWindow.swift").read_text()
        self.assertIn('.environment(\\.hubSurfaceStyle, windowStyle)', chat)
        self.assertIn("ChatWorkspaceRail(model: model).hubGlassPane(.sidebar)", chat)
        self.assertIn(".hubGlassPane(.inspector)", chat)
        # The transcript's paper runs under the live strip and the composer to
        # the window's bottom edge; no hard edge above the composer.
        pane = chat.split("struct ChatPane: View", 1)[1].split("// MARK: - Header", 1)[0]
        self.assertLess(pane.index("ChatTranscript(model: model)"), pane.index("ChatComposer(model: model)"))
        self.assertLess(pane.index("ChatComposer(model: model)"), pane.index(".hubGlassPane(.transcript)"))
        self.assertEqual(pane.count(".hubGlassPane(.transcript)"), 1)
        shell = (SOURCE / "CompactShell.swift").read_text()
        self.assertIn('.environment(\\.hubSurfaceStyle, windowStyle)', shell)
        self.assertIn("railView.hubGlassPane(.sidebar)", shell)
        self.assertIn("sidePane.hubGlassPane(.inspector)", shell)
        glass = (SOURCE / "HubGlass.swift").read_text()
        self.assertIn("HubTheme.WindowStyle.Mode.glass", glass)
        self.assertIn("HubTheme.Glass.transcript", glass)
        # A custom transcript colour takes the paper's place in both styles.
        modifier = glass.split("struct HubGlassPaneModifier", 1)[1].split("\n}\n", 1)[0]
        self.assertLess(modifier.index("if pane == .transcript, let custom = hues.transcriptFill { return custom }"),
                        modifier.index("guard style == .glass else { return .clear }"))

    def test_switching_surface_keeps_each_pane_in_place(self):
        """Glass/Solid changes a pane's fill, never its structural identity.

        An if/else around the content gives each style its own subtree, so a
        toggle would rebuild the transcript and drop its scroll position, open
        folds and loaded history."""
        glass = (SOURCE / "HubGlass.swift").read_text()
        modifier = glass.split("struct HubGlassPaneModifier", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("content.background(fill)", modifier)
        self.assertNotIn("else { content }", modifier)
        self.assertIn("return .clear", modifier)

    def test_ios_companion_mirrors_the_same_fills(self):
        theme = (COMPANION / "CompanionTheme.swift").read_text()
        self.assertIn("static func pane(_ pane: Pane, surfaceStyle: String) -> Color", theme)
        self.assertIn("18 / 255", theme)
        self.assertIn("244 / 255", theme)
        view = (COMPANION / "CompanionView.swift").read_text()
        self.assertIn("CompanionTheme.pane(.sidebar, surfaceStyle: surfaceStyle)", view)
        self.assertIn("CompanionTheme.pane(.transcript, surfaceStyle: surfaceStyle).ignoresSafeArea(edges: .bottom)", view)
        self.assertEqual(view.count("CompanionTheme.pane(.transcript"), 1)
        self.assertLess(view.index("composer(chat)"), view.index("CompanionTheme.pane(.transcript"))
        inspector = (COMPANION / "CompanionInspector.swift").read_text()
        self.assertIn('@AppStorage("companionSurface") private var surfaceStyle = "solid"', inspector)
        self.assertIn("CompanionTheme.pane(.inspector, surfaceStyle: surfaceStyle)", inspector)


class CompanionComposerHeightTests(unittest.TestCase):
    """The iOS composer is one line tall when empty, as on the Mac."""

    def test_mention_editor_sizes_to_its_text(self):
        field = (COMPANION / "CompanionMentionField.swift").read_text()
        body = field.split("struct CompanionMentionField: View", 1)[1].split("\n}\n", 1)[0]
        body = "\n".join(line for line in body.splitlines() if not line.strip().startswith("//"))
        # A flexible frame takes the whole proposed height whenever its child
        # is smaller; that is what grew the empty composer to its ceiling.
        self.assertNotIn(".frame(", body)
        self.assertIn("func sizeThatFits(_ proposal: ProposedViewSize, uiView: MentionUITextView", field)
        self.assertIn("MentionFieldHeight.clamp(fitted)", field)


if __name__ == "__main__":
    unittest.main()
