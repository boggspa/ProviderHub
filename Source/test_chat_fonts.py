"""Verify bundled open fonts and the real native preference/font resolution path."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).parent


class ChatFontAssetsTests(unittest.TestCase):
    def test_unmodified_assets_have_pinned_sources_hashes_and_complete_licenses(self):
        root = SOURCE / "fonts"
        manifest = json.loads((root / "manifest.json").read_text())
        self.assertEqual(manifest["license"], "SIL Open Font License 1.1")
        self.assertFalse(manifest["modified"])
        self.assertRegex(manifest["revision"], r"^[0-9a-f]{40}$")
        expected = {"manifest.json"}
        families = set()
        for record in manifest["files"]:
            with self.subTest(file=record["file"]):
                path = root / record["file"]
                self.assertTrue(path.resolve().is_relative_to(root.resolve()))
                data = path.read_bytes()
                self.assertEqual(len(data), record["bytes"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), record["sha256"])
                self.assertIn("/" + manifest["revision"] + "/", record["source"])
                expected.add(record["file"])
                if path.suffix == ".ttf":
                    families.add(record["family"])
                    license_text = (path.parent / "OFL.txt").read_text()
                    self.assertIn("Copyright", license_text)
                    self.assertIn("SIL OPEN FONT LICENSE Version 1.1", license_text)
                    self.assertIn("TERMINATION", license_text)
                    self.assertIn("DISCLAIMER", license_text)
        self.assertEqual(families, {"Inter", "Source Serif 4", "JetBrains Mono"})
        self.assertEqual({str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}, expected)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "Native fonts need macOS")
    def test_native_registration_migration_custom_selection_and_fallback(self):
        cases = r'''
import AppKit
import SwiftUI
@main struct Cases {
    @MainActor static func main() throws {
        func check(_ value: Bool, _ message: String) { if !value { fatalError(message) } }
        ChatFonts.register(in: URL(fileURLWithPath: CommandLine.arguments[1]))
        for choice in ChatFonts.choices {
            if let name = choice.postScriptName {
                let font = ChatFonts.font(choice.id, size: 15)
                check(font.fontName == name, "Bundled font did not register: " + choice.id)
                check(font.pointSize == 15, "Font ignored configured size")
            }
        }
        check(ChatFonts.selection("", legacyMonospaced: true) == "monospaced", "Legacy Mono preference lost")
        check(ChatFonts.selection("", legacyMonospaced: false) == "system", "Legacy System preference lost")
        check(ChatFonts.selection("inter", legacyMonospaced: true) == "inter", "Legacy preference overrode chosen font")
        check(ChatFonts.selection("custom", legacyMonospaced: false) == "custom", "Custom selection lost")
        let custom = NSFont(name: "Menlo-Regular", size: 17)!
        let suite = "chat-font-tests." + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        ChatFontPanelController.shared.apply(custom, to: defaults)
        let reopened = UserDefaults(suiteName: suite)!
        check(reopened.string(forKey: "chatFontChoice") == "custom", "Font panel selection not persisted")
        check(reopened.string(forKey: "chatCustomFontName") == custom.fontName, "Custom font identity lost")
        check(reopened.double(forKey: "chatTextSize") == 17, "Custom size not persisted")
        check(ChatFonts.font("custom", customName: custom.fontName, size: 17) == custom, "Installed font not resolved")
        let fallback = ChatFonts.font("custom", customName: "AbsentFont-" + UUID().uuidString, size: 15)
        check(fallback == NSFont.systemFont(ofSize: 15), "Removed Font Book font did not fall back")
        check(ChatFonts.font("system", size: .nan).pointSize == 13, "Malformed size was not bounded")
        check(ChatFonts.font("system", size: 400).pointSize == 32, "Oversized preference escaped bounds")
        check(ChatFonts.font("system", size: -1).pointSize == 10, "Undersized preference escaped bounds")
        _ = NSApplication.shared
        let manager = NSFontManager.shared
        let previousTarget = manager.target
        let previousAction = manager.action
        ChatFontPanelController.shared.show(selection: "inter", customName: "", size: 15)
        check(manager.fontPanel(false)?.isVisible == true, "Custom did not open the native font panel")
        check(manager.target === ChatFontPanelController.shared, "Font panel changes not routed to Chat preferences")
        ChatFontPanelController.shared.dismiss()
        check(manager.fontPanel(false)?.isVisible == false, "Preset switch did not dismiss Custom panel")
        check(manager.target === previousTarget && manager.action == previousAction, "Font panel did not release its global target")
        print("Native Chat fonts passed")
    }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Cases.swift").write_text(cases)
            binary = root / "chat-font-tests"
            compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-module-cache-path", str(root / "cache"), str(SOURCE / "ChatFonts.swift"),
                str(root / "Cases.swift"), "-framework", "AppKit", "-framework", "SwiftUI",
                "-framework", "CoreText", "-o", str(binary)], capture_output=True, text=True, timeout=90)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary), str(SOURCE / "fonts")], capture_output=True, text=True, timeout=15)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertIn("Native Chat fonts passed", ran.stdout)


if __name__ == "__main__":
    unittest.main()
