"""Custom Chat colours: storage, derivation, contrast, the picker and wiring."""
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).parent


MODEL_CASES = r'''
import AppKit
import SwiftUI

@main struct Cases {
    @MainActor static func main() throws {
        func check(_ condition: Bool, _ message: String) { if !condition { fatalError(message) } }
        _ = NSApplication.shared

        // Storage: "hue,intensity", locale-independent, anything else is Default.
        let parsed = ChatHue(stored: "210,0.6")
        check(parsed == ChatHue(hue: 210, intensity: 0.6), "parse")
        check(parsed?.stored == "210,0.60", "format: " + (parsed?.stored ?? "nil"))
        check(ChatHue(stored: " 12 , 1 ")?.stored == "12,1.00", "surrounding spaces")
        for bad in ["", "210", "210,", ",0.5", "abc,0.5", "210,x", "nan,0.5", "210,inf", "1,2,3", "210;0.5"] {
            check(ChatHue(stored: bad) == nil, "accepted " + bad)
        }
        check(ChatHue(stored: "370,0.5")?.hue == 10, "hue wraps past 360")
        check(ChatHue(stored: "-30,0.5")?.hue == 330, "hue wraps below 0")
        check(ChatHue(stored: "359.6,0.5")?.hue == 0, "hue rounds to whole degrees")
        check(ChatHue(stored: "20,1.7")?.intensity == 1 && ChatHue(stored: "20,-1")?.intensity == 0, "intensity clamps")
        for preset in ChatHue.presets { check(ChatHue(stored: preset.hue.stored) == preset.hue, "preset round trip " + preset.name) }

        // Keys sit beside the theme's own and stay observable by @AppStorage.
        let keys = ChatHueSurface.allCases.map(\.defaultsKey)
        check(keys == ["chatHueTranscript", "chatHueText", "chatHueComposer", "chatHueBubble"], "keys: \(keys)")
        check(keys.allSatisfy { !$0.contains(".") }, "a dotted key is a key path to KVO")

        // Defaults: nothing stored, or nothing readable, is today's look.
        let suite = "chat-hue-tests-" + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        let empty = ChatHues(defaults: defaults)
        check(empty.isDefault && empty == ChatHues(), "empty store is Default")
        check(empty.textColor == nil && empty.textInk == nil && empty.bubbleFill == nil && empty.composerFill == nil
              && empty.transcriptFill == nil, "Default paints nothing")
        defaults.set("not a colour", forKey: "chatHueText")
        check(ChatHues(defaults: defaults).isDefault, "garbage reads as Default")

        // Persistence: what the picker writes is what the window reads.
        defaults.set("215,0.70", forKey: ChatHueSurface.transcript.defaultsKey)
        defaults.set("45,0.35", forKey: ChatHueSurface.text.defaultsKey)
        defaults.set("215,0.55", forKey: ChatHueSurface.composer.defaultsKey)
        defaults.set("265,0.70", forKey: ChatHueSurface.bubble.defaultsKey)
        let stored = ChatHues(defaults: defaults)
        check(stored.transcript == ChatHue(hue: 215, intensity: 0.7) && stored.text == ChatHue(hue: 45, intensity: 0.35)
              && stored.composer == ChatHue(hue: 215, intensity: 0.55) && stored[.bubble] == ChatHue(hue: 265, intensity: 0.7),
              "read back: \(stored)")
        var edited = stored; edited[.text] = nil
        check(edited.text == nil && edited.bubble != nil, "one surface resets on its own")
        ChatHues.reset(defaults)
        check(ChatHues(defaults: defaults).isDefault, "Reset All clears every key")
        defaults.removePersistentDomain(forName: suite)

        // Derivation: the hue is the user's, the brightness is the appearance's.
        func hsb(_ color: NSColor) -> (h: Double, s: Double, b: Double) {
            var h: CGFloat = 0, s: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
            color.usingColorSpace(.sRGB)!.getHue(&h, saturation: &s, brightness: &b, alpha: &a)
            return (Double(h) * 360, Double(s), Double(b))
        }
        let ocean = ChatHue(hue: 205, intensity: 0.7)
        for surface in ChatHueSurface.allCases {
            let light = hsb(surface.color(ocean, dark: false)), dark = hsb(surface.color(ocean, dark: true))
            check(abs(light.h - 205) < 1 && abs(dark.h - 205) < 1, "\(surface) keeps the hue")
            if surface == .text { check(light.b < 0.4 && dark.b > 0.85, "text is dark on Light and light on Dark") }
            else { check(light.b > 0.85 && dark.b < 0.35, "\(surface) is pale on Light and deep on Dark") }
        }
        check(hsb(ChatHueSurface.bubble.color(ChatHue(hue: 120, intensity: 0), dark: true)).s < 0.001, "zero intensity is grey")

        // One dynamic colour per choice: transcript text views compare by identity.
        let text = ChatHueSurface.text.dynamicColor(ocean)
        check(text === ChatHueSurface.text.dynamicColor(ChatHue(stored: ocean.stored)!), "same choice, same instance")
        check(text !== ChatHueSurface.bubble.dynamicColor(ocean), "each surface derives its own")
        check(ChatHues(text: ocean).textColor === text, "the environment hands out the cached colour")
        func resolve(_ color: NSColor, dark: Bool) -> NSColor {
            var out = color
            NSAppearance(named: dark ? .darkAqua : .aqua)!.performAsCurrentDrawingAppearance { out = color.usingColorSpace(.sRGB)! }
            return out
        }
        func same(_ a: NSColor, _ b: NSColor) -> Bool {
            let x = a.usingColorSpace(.sRGB)!, y = b.usingColorSpace(.sRGB)!
            return abs(x.redComponent - y.redComponent) < 0.002 && abs(x.greenComponent - y.greenComponent) < 0.002
                && abs(x.blueComponent - y.blueComponent) < 0.002 && abs(x.alphaComponent - y.alphaComponent) < 0.002
        }
        for dark in [false, true] {
            check(same(resolve(text, dark: dark), ChatHueSurface.text.color(ocean, dark: dark)), "dynamic text follows the appearance")
        }

        // The chosen background is opaque, whatever the window style: in
        // Glass it replaces the paper rather than tinting see-through glass.
        let painted = ChatHues(transcript: ocean)
        for dark in [false, true] {
            let fill = resolve(NSColor(painted.transcriptFill!), dark: dark)
            check(abs(fill.alphaComponent - 1) < 0.001, "the transcript stays opaque")
            check(same(fill, ChatHueSurface.transcript.color(ocean, dark: dark)), "the colour is painted as chosen")
        }

        // Contrast maths.
        check(abs(ChatContrast.ratio(.white, .black) - 21) < 0.01, "white on black is 21:1")
        check(abs(ChatContrast.ratio(.white, .white) - 1) < 0.001, "a colour on itself is 1:1")
        let half = ChatContrast.composite(NSColor(srgbRed: 1, green: 1, blue: 1, alpha: 0.5), over: NSColor(srgbRed: 0, green: 0, blue: 0, alpha: 1))
        check(abs(half.redComponent - 0.5) < 0.001 && half.alphaComponent == 1, "composite")

        // Warnings, against a fixed stand-in for today's colours.
        func base(_ dark: Bool) -> ChatHueBase {
            dark ? ChatHueBase(transcript: NSColor(srgbRed: 18 / 255, green: 18 / 255, blue: 18 / 255, alpha: 1),
                               text: NSColor(srgbRed: 0.93, green: 0.93, blue: 0.93, alpha: 1),
                               bubble: NSColor(srgbRed: 1, green: 1, blue: 1, alpha: 0.09),
                               composer: NSColor(srgbRed: 37 / 255, green: 38 / 255, blue: 41 / 255, alpha: 1))
                 : ChatHueBase(transcript: NSColor(srgbRed: 244 / 255, green: 244 / 255, blue: 246 / 255, alpha: 1),
                               text: NSColor(srgbRed: 0.11, green: 0.11, blue: 0.11, alpha: 1),
                               bubble: NSColor(srgbRed: 0, green: 0, blue: 0, alpha: 0.09),
                               composer: NSColor(srgbRed: 1, green: 1, blue: 1, alpha: 1))
        }
        check(ChatHues().contrastIssues(base: base).isEmpty, "Default never warns")
        check(ChatHues(text: ChatHue(hue: 45, intensity: 0.35)).contrastIssues(base: base).isEmpty, "a pastel reads")
        // A chosen background is measured as itself, not blended with anything.
        let deep = ChatHue(hue: 215, intensity: 0.7)
        check(same(ChatHues(transcript: deep).resolved(dark: true, base: base(true)).transcript,
                   ChatHueSurface.transcript.color(deep, dark: true)), "the chosen background is measured as is")
        let vividBlue = ChatHue(hue: 240, intensity: 1)
        let flagged = ChatHues(text: vividBlue).contrastIssues(base: base)
        check(flagged.contains { $0.dark && $0.pair == "Text on the background" }, "vivid blue text on Dark is flagged: \(flagged)")
        check(!flagged.contains { !$0.dark }, "the same text reads on Light")
        check(flagged[0].message.hasPrefix("Text on the background is ") && flagged[0].message.contains(":1 in Dark."), flagged[0].message)
        // Flagged, never adjusted: the window still paints exactly the choice.
        check(same(resolve(ChatHues(text: vividBlue).textColor!, dark: true), ChatHueSurface.text.color(vividBlue, dark: true)),
              "a low-contrast choice is kept")
        let bubble = ChatHues(text: vividBlue, bubble: vividBlue).contrastIssues(base: base)
        check(bubble.contains { $0.dark && $0.pair == "Text in your bubble" }, "text on a matching bubble is flagged: \(bubble)")
        print("Chat hue model checks passed")
    }
}
'''


SETTINGS_CASES = r'''
import AppKit
import SwiftUI

@main struct Cases {
    @MainActor static func main() throws {
        func check(_ condition: Bool, _ message: String) { if !condition { fatalError(message) } }
        _ = NSApplication.shared
        func rgb(_ color: NSColor) -> [Double] {
            let s = color.usingColorSpace(.sRGB)!
            return [s.redComponent, s.greenComponent, s.blueComponent].map(Double.init)
        }
        func near(_ a: [Double], _ b: [Double]) -> Bool { zip(a, b).allSatisfy { abs($0 - $1) < 0.004 } }

        // The picker measures against what the window paints today.
        let darkGlass = ChatHueSettings.base(dark: true, glass: true)
        let lightGlass = ChatHueSettings.base(dark: false, glass: true)
        check(near(rgb(darkGlass.transcript), [18.0 / 255, 18.0 / 255, 18.0 / 255]), "Dark Glass paper")
        check(near(rgb(lightGlass.transcript), [244.0 / 255, 244.0 / 255, 246.0 / 255]), "Light Glass paper")
        let darkSolid = ChatHueSettings.base(dark: true, glass: false)
        check(darkSolid.transcript.alphaComponent == 1 && !near(rgb(darkSolid.transcript), rgb(darkGlass.transcript)),
              "Solid measures against the window, not the paper")
        check(ChatContrast.luminance(darkGlass.text) > 0.7 && ChatContrast.luminance(lightGlass.text) < 0.05, "today's ink")
        check(darkGlass.bubble.alphaComponent < 0.2, "today's bubble is a translucent lift")

        for glass in [true, false] {
            let base = { (dark: Bool) in ChatHueSettings.base(dark: dark, glass: glass) }
            check(ChatHues().contrastIssues(base: base).isEmpty, "Default never warns")
            // Every swatch is safe on every surface with today's text.
            for preset in ChatHue.presets {
                for surface in ChatHueSurface.allCases {
                    var hues = ChatHues(); hues[surface] = preset.hue
                    let issues = hues.contrastIssues(base: base)
                    check(issues.isEmpty, "\(preset.name) on \(surface) warns: \(issues.map(\.message))")
                }
            }
        }

        // A chosen background replaces the paper in Glass and the window in
        // Solid, so both styles measure the colour itself.
        let ocean = ChatHues(transcript: ChatHue(hue: 205, intensity: 0.7))
        let tint = rgb(ChatHueSurface.transcript.color(ChatHue(hue: 205, intensity: 0.7), dark: true))
        check(near(rgb(ocean.resolved(dark: true, base: darkGlass).transcript), tint), "Glass measures the chosen colour")
        check(near(rgb(ocean.resolved(dark: true, base: darkSolid).transcript), tint), "Solid measures the chosen colour")

        // The sheet lays out in both appearances, with and without choices.
        for dark in [false, true] {
            NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
            let host = NSHostingView(rootView: ChatHueSettings())
            host.layoutSubtreeIfNeeded()
            check(host.fittingSize.width >= 540 && host.fittingSize.height > 300, "sheet size \(host.fittingSize)")
        }
        print("Chat hue settings checks passed")
    }
}
'''


SPLIT_CASES = r'''
import AppKit
import SwiftUI

@MainActor final class Seen { static var values: [String: String] = [:] }

/// Stands in for a pane's transcript: it reads the hues the window injects.
struct PaneProbe: View {
    let name: String
    @Environment(\.chatHues) private var hues
    var body: some View {
        Text(name)
            .onAppear { Seen.values[name] = hues.bubble?.stored ?? "default" }
            .onChange(of: hues) { _, value in Seen.values[name] = value.bubble?.stored ?? "default" }
    }
}

/// Each split pane is its own NSHostingController; the hues are injected
/// once, above the split, the way ChatWindow injects them.
struct Root: View {
    let layout: ChatWorkspaceLayout
    var body: some View {
        ChatSplitWorkspace(layout: .constant(layout), focusedPaneID: .constant(layout.focusedPaneID),
            leafContent: { _, assignment in
                PaneProbe(name: { if case .chat(let chat) = assignment { return chat }; return "empty" }())
            },
            onFocus: { _ in }, onDropChat: { _, _, _ in })
        .chatHues()
    }
}

@main struct Cases {
    @MainActor static func main() {
        func check(_ condition: Bool, _ message: String) { if !condition { print("FAIL " + message); exit(1) } }
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited)
        let suite = "chat-hue-split-" + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        var layout = ChatWorkspaceLayout.initial(chatID: "left")
        layout.splitRight(layout.focusedPaneID, assignment: .chat(id: "right"))
        layout.splitBelow(layout.focusedPaneID, assignment: .chat(id: "below"))
        let window = NSWindow(contentRect: NSRect(x: -20000, y: -20000, width: 1400, height: 1000),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = NSHostingView(rootView: Root(layout: layout).defaultAppStorage(defaults).frame(width: 1400, height: 1000))
        window.orderFrontRegardless()
        func settle(on expected: String) -> Bool {
            let deadline = Date().addingTimeInterval(15)
            while Date() < deadline {
                RunLoop.main.run(until: Date().addingTimeInterval(0.05))
                if ["left", "right", "below"].allSatisfy({ Seen.values[$0] == expected }) { return true }
            }
            return false
        }
        check(settle(on: "default"), "panes start on Default: \(Seen.values)")
        defaults.set("265,0.70", forKey: ChatHueSurface.bubble.defaultsKey)
        check(settle(on: "265,0.70"), "every pane takes the new bubble: \(Seen.values)")
        ChatHues.reset(defaults)
        check(settle(on: "default"), "Reset All reaches every pane: \(Seen.values)")
        window.orderOut(nil)
        defaults.removePersistentDomain(forName: suite)
        print("Chat hue split checks passed")
    }
}
'''


def run_native(test, files, cases, marker, timeout=60):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "Cases.swift").write_text(cases)
        binary = root / "chat-hue-tests"
        compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
            "-target", platform.machine() + "-apple-macosx14.0", "-module-cache-path", str(root / "cache"),
            *(str(SOURCE / name) for name in files), str(root / "Cases.swift"),
            "-framework", "AppKit", "-framework", "SwiftUI", "-o", str(binary)],
            capture_output=True, text=True, timeout=180)
        test.assertEqual(compiled.returncode, 0, compiled.stderr)
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=timeout)
        test.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        test.assertIn(marker, ran.stdout)


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "Native colour tests need macOS")
class ChatHueNativeTests(unittest.TestCase):
    def test_storage_parsing_defaults_derivation_and_contrast(self):
        # ChatHues.swift compiles alone: the stubbed transcript targets rely on it.
        run_native(self, ["ChatHues.swift"], MODEL_CASES, "Chat hue model checks passed")

    def test_picker_measures_todays_colours_and_every_swatch_reads(self):
        run_native(self, ["HubTheme.swift", "HubGlass.swift", "ChatHues.swift", "ChatHueSettings.swift"],
                   SETTINGS_CASES, "Chat hue settings checks passed")

    def test_a_change_reaches_every_split_pane_live(self):
        run_native(self, ["ChatWorkspaceLayout.swift", "ChatSplitWorkspace.swift", "ChatHues.swift"],
                   SPLIT_CASES, "Chat hue split checks passed", timeout=90)


class ChatHueWiringTests(unittest.TestCase):
    """Each surface paints the custom value and falls back to today's token."""

    @staticmethod
    def section(name, start, end):
        text = (SOURCE / name).read_text()
        return text.split(start, 1)[1].split(end, 1)[0]

    def test_transcript_text_bubble_and_composer_read_the_hues(self):
        text = (SOURCE / "ChatTranscriptText.swift").read_text()
        # Prose and fenced code both use the text hue; table cells use it as a SwiftUI colour.
        self.assertEqual(text.count("color: hues.textColor ?? HubTheme.Semantic.nsInk"), 2)
        self.assertIn(".foregroundStyle(hues.textInk ?? HubTheme.Semantic.ink)", text)
        self.assertNotIn("color: HubTheme.Semantic.nsInk", text)
        user = self.section("ChatTranscriptRows.swift", "struct UserRow: View", "struct AssistantRow: View")
        self.assertIn("@Environment(\\.chatHues) private var hues", user)
        self.assertIn("color: hues.textColor ?? Semantic.nsInk", user)
        self.assertIn(".fill(hues.bubbleFill ?? Semantic.selection)", user)
        composer = self.section("ChatWindow.swift", "private struct ChatComposer: View", "private var editable: Bool")
        self.assertIn("shape.fill(hues.composerFill ?? Semantic.composerFill)", composer)

    def test_the_window_injects_once_and_the_transcript_pane_paints_the_background(self):
        window = self.section("ChatWindow.swift", "struct ChatWindow: View", "private var titleBar")
        self.assertEqual(window.count(".chatHues()"), 1)
        self.assertIn(".environment(\\.hubSurfaceStyle, windowStyle)", window)
        glass = self.section("HubGlass.swift", "private struct HubGlassPaneModifier", "\n}\n")
        custom = glass.index("if pane == .transcript, let custom = hues.transcriptFill { return custom }")
        self.assertLess(custom, glass.index("guard style == .glass else { return .clear }"))
        # Opaque in both styles: nothing thins the chosen colour into glass.
        self.assertNotIn(".opacity(", glass)
        hues = (SOURCE / "ChatHues.swift").read_text()
        self.assertNotIn("glassOpacity", hues)

    def test_theme_menu_opens_the_picker_with_resets_and_warnings(self):
        theme = self.section("ChatSettings.swift", 'Menu("Theme") {', 'Menu("Window") {')
        self.assertIn("Custom Colours…", theme)
        settings = (SOURCE / "ChatSettings.swift").read_text()
        self.assertIn(".sheet(isPresented: $showColours) { ChatHueSettings() }", settings)
        picker = (SOURCE / "ChatHueSettings.swift").read_text()
        self.assertIn('Button("Reset All") { ChatHues.reset() }', picker)
        self.assertIn('Button("Reset") { store("", surface) }', picker)
        self.assertIn("hues.contrastIssues { Self.base(dark: $0, glass: glass) }", picker)
        self.assertNotIn("tints the glass", picker)
        # Drawn in the hub's own style: swatches and colour tracks, not the stock well.
        self.assertNotIn("ColorPicker(", picker)
        self.assertIn("ChatHue.presets", picker)

    def test_new_sources_are_compiled_into_the_app(self):
        build = (SOURCE / "build.sh").read_text()
        sources = build.split("SWIFT_SOURCES=(", 1)[1].split("\n)", 1)[0]
        self.assertIn('"$SOURCE_DIR/ChatHues.swift"', sources)
        self.assertIn('"$SOURCE_DIR/ChatHueSettings.swift"', sources)


if __name__ == "__main__":
    unittest.main()
