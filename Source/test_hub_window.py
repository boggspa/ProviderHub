"""The Hub and Chat share one window: a Providers | Code switch picks the half."""
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).parent


def read(name: str) -> str:
    return (SOURCE / name).read_text()


def between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


class OneWindowRoutingTests(unittest.TestCase):
    """AppDelegate builds one window and every way in lands on the right half."""

    def setUp(self):
        self.bridge = read("MistralBridge.swift")
        self.delegate = self.bridge.split("final class AppDelegate", 1)[1]

    def test_app_builds_one_window_with_both_halves_in_it(self):
        self.assertEqual(self.delegate.count("HubWindow(contentRect:"), 1)
        self.assertNotIn("NSWindow(contentRect", self.delegate)
        self.assertIsNone(re.search(r"\bchatWindow\b", self.bridge), "a second Chat window is still declared")
        # Halves are installed once into one hierarchy and switched by
        # visibility, never by swapping the window's content view.
        self.assertEqual(self.delegate.count("window.contentView ="), 1)
        self.assertIn("content.install(ChatWindow(model: chatModel)", self.delegate)
        self.assertIn("content.install(CompactShell(model: model)", self.delegate)
        self.assertIn("content.install(ClassicHubFrame(model: model, tabs: tabs), for: .providers)", self.delegate)
        apply = between(self.delegate, "func applyTab() {", "\n    }\n")
        self.assertIn("if tab == .code, !content.has(.code)", apply)
        self.assertIn("content.show(tab)", apply)
        content = between(read("HubWindow.swift"), "final class HubWindowContent", "\n}\n")
        self.assertIn("host.isHidden = key != tab", content)
        self.assertNotIn("removeFromSuperview", between(content, "func show(", "\n    }\n"))

    def test_every_entry_point_lands_on_its_half(self):
        show_window = between(self.delegate, "@objc func showWindow()", "\n")
        self.assertIn("tabs.select(.providers); present()", show_window)
        show_chat = between(self.delegate, "@objc func showChat() {", "\n    }\n")
        self.assertIn("tabs.select(.code); present()", show_chat)
        reopen = between(self.delegate, "func applicationShouldHandleReopen", "\n    }\n")
        self.assertIn("if !hasVisibleWindows { present() }", reopen)
        menu = between(self.delegate, "func menuWillOpen(_ menu: NSMenu) {", "\n    }\n")
        self.assertIn('add(menu, "Chat with Model…", #selector(showChat), "k")', menu)
        self.assertIn('add(menu, "Models & Settings…", #selector(showWindow), ",")', menu)
        # ⌘1 and ⌘2 in the View menu do what the switch does.
        self.assertIn("#selector(chooseTab(_:)), keyEquivalent: String(index + 1)", self.delegate)
        self.assertIn("tabs.select(tab); present()", between(self.delegate, "@objc func chooseTab(", "\n    }\n"))
        # A hub notice raised from the menu bar or Code brings Providers forward.
        self.assertIn("attention: { [weak self] in self?.showWindow() })", between(self.delegate, "@objc func launchClaude()", "\n"))
        self.assertIn("attention: { [weak self] in self?.showWindow() })", between(self.delegate, "@objc func launchCodex()", "\n"))
        self.assertIn("HubWindowContext(tabs: tabs, hub: model, attention: { [weak self] in self?.showWindow() })", self.delegate)

    def test_window_keeps_its_dock_frame_style_and_size_rules(self):
        present = between(self.delegate, "func present() {", "\n    }\n")
        self.assertIn("NSApp.setActivationPolicy(.regular)", present)
        close = between(self.delegate, "func windowWillClose(_ notification: Notification) {", "\n    }\n")
        self.assertIn("NSApp.setActivationPolicy(.accessory)", close)
        self.assertIn('static let frameName = "ProviderHubWindow"', self.delegate)
        self.assertIn('!window.setFrameUsingName("ProviderHubChat")', self.delegate)
        self.assertIn("window.setFrameAutosaveName(Self.frameName)", self.delegate)
        self.assertIn('static let defaultsKey = "hubWindowTab"', read("HubWindow.swift"))
        minimum = between(self.delegate, "var minimumSize: NSSize {", "\n    }\n")
        self.assertIn("if tabs.current == .code { return NSSize(width: 720, height: 536) }", minimum)
        self.assertIn('design == "classic" ? NSSize(width: 870, height: 752) : NSSize(width: 760, height: 536)', minimum)
        apply = between(self.delegate, "func applyTab() {", "\n    }\n")
        self.assertIn("ChatZoomCommands.shared.window = tab == .code ? window : nil", apply)
        self.assertIn("window.minSize = minimumSize", apply)
        style = between(self.delegate, "func applyWindowStyle() {", "\n    }\n")
        self.assertIn('!(tabs.current == .providers && design == "classic")', style)
        # The updater's restart blocker still reads both halves' state.
        blocker = between(self.delegate, "func updateRestartBlocker() -> String? {", "\n    }\n")
        self.assertIn("unsavedSettings: model.changed, drafts: chatModel.hasUnsentDrafts", blocker)

    def test_chat_keeps_the_gateway_alive_while_its_half_is_open(self):
        self.assertIn("var chatOpen = false { didSet { model.chatWindowOpen = chatOpen } }", self.delegate)
        close = between(self.delegate, "func windowWillClose(_ notification: Notification) {", "\n    }\n")
        self.assertIn("windowOpen = false; chatOpen = false", close)
        open_chat = between(self.delegate, "func openChat() {", "\n    }\n")
        self.assertIn("guard windowOpen, !chatOpen else { return }", open_chat)
        self.assertIn("await chatModel.start()", open_chat)
        # Chat's own half carries Stop, so an open but idle Chat cannot block it.
        toggle = between(self.bridge, "func toggleGateway() async {", "\n    }\n")
        self.assertNotIn("anyOwnedHarnessRunning", toggle)
        self.assertIn("if chatBusy {", toggle)
        self.assertIn("if desktopOwnedHarnessRunning || activeRequests > 0 {", toggle)


class SwitchAndTitleRowTests(unittest.TestCase):
    """The switch is the hub's own control, in one place; the launchers are shared."""

    def setUp(self):
        self.window = read("HubWindow.swift")
        self.shell = read("CompactShell.swift")
        self.chat = read("ChatWindow.swift")

    def test_switch_is_a_custom_capsule_not_a_stock_control(self):
        switch = between(self.window, "struct HubModeSwitch: View {", "struct HubModeSwitchBand")
        for stock in ("Picker", "pickerStyle", "NSSegmentedControl", "SegmentedPickerStyle", "Toggle("):
            self.assertNotIn(stock, switch)
        for token in ("Capsule().fill(Semantic.raisedSurface)", "Capsule().strokeBorder(Semantic.hairline",
                      "Capsule().fill(Semantic.selection)", ".buttonStyle(.plain)", "accessibilityAddTraits(on ? .isSelected : [])"):
            self.assertIn(token, switch)
        self.assertIn('self == .providers ? "Providers" : "Code"', self.window)

    def test_switch_sits_in_the_same_place_in_every_half(self):
        # Under the title row, at the left, in a band of one fixed height.
        body = between(self.shell, "    var body: some View {\n", ".font(.system(size: 11.5))")
        self.assertLess(body.index("titleBar"), body.index("HubModeSwitchBand(tabs: hubWindow.tabs)"))
        self.assertLess(body.index("HubModeSwitchBand(tabs: hubWindow.tabs)"), body.index("if tab == .settings"))
        rail = between(self.chat, "if panes.rail > 0 {", ".hubGlassPane(.sidebar)")
        self.assertLess(rail.index("HubModeSwitchBand(tabs: hubWindow.tabs)"), rail.index("ChatWorkspaceRail(model: model)"))
        self.assertIn("if panes.rail == 0, let hubWindow { HubModeSwitchBand(tabs: hubWindow.tabs) }", self.chat)
        classic = between(self.window, "struct ClassicHubFrame: View {", "\n}\n")
        self.assertLess(classic.index("HubTitleRow"), classic.index("HubModeSwitchBand(tabs: tabs)"))
        self.assertLess(classic.index("HubModeSwitchBand(tabs: tabs)"), classic.index("BridgeWindow(model: model)"))
        # Every half draws the same title row, so the band starts at the same height.
        self.assertIn("HubTitleRow {", between(self.shell, "private var titleBar: some View {", "// MARK: rail"))
        self.assertIn("HubTitleRow {", between(self.chat, "private var titleBar: some View {", "\n    }\n"))
        # The fixed-width switch fits Chat's narrowest rail.
        width = float(re.search(r"static let width: CGFloat = ([\d.]+)", self.window).group(1))
        inset = float(re.search(r"static let switchInset: CGFloat = ([\d.]+)", self.window).group(1))
        narrowest = float(re.search(r"railWidth = Double\(ChatPaneSizing\.clamp\(\$0, ([\d.]+), 360\)\)", self.chat).group(1))
        self.assertLessEqual(width + inset, narrowest)

    def test_code_title_row_reuses_the_hub_launchers_and_gateway(self):
        title = between(self.chat, "private var titleBar: some View {", "\n    }\n")
        self.assertIn("HubQuickControls(model: hubWindow.hub, attention: hubWindow.attention)", title)
        quick = between(self.window, "struct HubQuickControls: View {", "\n}\n")
        self.assertIn("HubGatewayControl(model: model, attention: attention)", quick)
        self.assertIn("HubLaunchCluster(model: model, attention: attention)", quick)
        self.assertNotIn("showsAddress: true", quick)
        shell_title = between(self.shell, "private var titleBar: some View {", "// MARK: rail")
        self.assertIn("HubGatewayControl(model: model, showsAddress: true)", shell_title)
        self.assertIn("HubLaunchCluster(model: model)", shell_title)
        self.assertNotIn("func launchMark", self.shell)
        # The controls call the hub's one implementation of each action.
        gateway = between(self.window, "struct HubGatewayControl: View {", "\n}\n")
        self.assertIn("await model.toggleGateway()", gateway)
        self.assertNotIn("127.0.0.1", between(gateway, "Button(", "\n"))
        launch = between(self.window, "struct HubLaunchCluster: View {", "\n}\n")
        self.assertIn("await model.launchClaude()", launch)
        self.assertIn("await model.launchCodex()", launch)
        for duplicated in ("command(", "prepare-launch", "openApplication", "startGateway", "stopGateway"):
            self.assertNotIn(duplicated, self.window)
        # Running state reads as Show, closed as Launch.
        self.assertIn('let action = live ? "Show " + name : "Launch " + name', launch)
        self.assertIn(".accessibilityLabel(action)", launch)

    def test_hidden_half_cannot_take_focus_or_pause_the_transcript(self):
        window = between(self.window, "final class HubWindow: NSWindow {", "\n}\n")
        self.assertIn("view.isHiddenOrHasHiddenAncestor { return false }", window)
        scroll = read("ChatTranscriptScroll.swift")
        observe = between(scroll, "private func observe(_ event: NSEvent) {", "\n        }\n")
        self.assertIn("!scroll.isHiddenOrHasHiddenAncestor", observe)


CASES = r'''
import AppKit
import SwiftUI

final class Counter: ObservableObject {
    var hits: [String: Int] = [:]
    var appeared: [String: Int] = [:]
}

struct Half: View {
    let name: String
    let counter: Counter
    @State private var taps = 0
    var body: some View {
        Button("Act " + name + " \(taps)") { taps += 1; counter.hits[name, default: 0] += 1 }
            .keyboardShortcut("n", modifiers: .command)
            .onAppear { counter.appeared[name, default: 0] += 1 }
    }
}

@main struct Cases {
    @MainActor static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        func check(_ condition: Bool, _ message: String) { if !condition { fatalError(message) } }
        func settle(_ seconds: Double = 0.15) { RunLoop.main.run(until: Date().addingTimeInterval(seconds)) }

        let suite = "ProviderHub.HubWindowTests." + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let tabs = HubWindowTabs(defaults: defaults)
        check(tabs.current == .providers, "a fresh install must open on Providers")
        var selected: [HubWindowTab] = []
        tabs.onSelect = { selected.append($0) }
        tabs.select(.code); tabs.select(.code)
        check(selected == [.code], "choosing the shown half switched again")
        check(HubWindowTabs(defaults: defaults).current == .code, "the chosen half did not persist")

        let window = HubWindow(contentRect: NSRect(x: 0, y: 0, width: 480, height: 320), styleMask: [.titled, .resizable],
                               backing: .buffered, defer: false)
        let content = HubWindowContent()
        window.contentView = content
        let counter = Counter()
        content.install(Half(name: "providers", counter: counter), for: .providers)
        content.install(Half(name: "code", counter: counter), for: .code)
        content.show(.providers)
        window.setFrameOrigin(NSPoint(x: -20000, y: -20000)); window.orderFrontRegardless()
        settle()
        func press() {
            let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: .command, timestamp: 0,
                                         windowNumber: window.windowNumber, context: nil, characters: "n",
                                         charactersIgnoringModifiers: "n", isARepeat: false, keyCode: 45)!
            _ = window.performKeyEquivalent(with: event); settle()
        }
        press()
        check(counter.hits == ["providers": 1], "a key equivalent reached the hidden half")
        content.show(.code); settle()
        press()
        check(counter.hits == ["providers": 1, "code": 1], "the shown half lost its key equivalent")
        check(counter.appeared == ["providers": 1, "code": 1], "switching rebuilt a half")

        let host = content.subviews.first { !$0.isHidden }!
        let field = NSTextField(frame: NSRect(x: 10, y: 10, width: 120, height: 22))
        host.addSubview(field)
        func focused(_ view: NSView) -> Bool {
            window.firstResponder === view || (window.firstResponder as? NSText)?.delegate === view
        }
        check(window.makeFirstResponder(field) && focused(field), "the shown half could not take focus")
        content.show(.providers); settle()
        check(!((window.firstResponder as? NSView)?.isHiddenOrHasHiddenAncestor ?? false), "the hidden half kept the keyboard")
        check(!window.makeFirstResponder(field), "a hidden half took focus")
        content.show(.code); settle()
        check(focused(field), "Code did not get back the focus it had")

        // A design change rebuilds Providers only.
        content.install(Half(name: "classic", counter: counter), for: .providers)
        content.show(.providers); settle()
        check(counter.appeared == ["providers": 1, "code": 1, "classic": 1], "replacing Providers disturbed Code")
        check(content.subviews.count == 2, "the replaced half stayed in the window")
        window.orderOut(nil)
        print("HUB_WINDOW_OK")
    }
}
'''


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "AppKit window test needs macOS")
class HubWindowContentTests(unittest.TestCase):
    """The real container and window classes: one half live, the other kept."""

    def test_hidden_half_keeps_state_and_gets_no_keys_or_focus(self):
        window = read("HubWindow.swift")
        # The model-free parts: the tab state and the AppKit container.
        types = window[window.index("enum HubWindowTab"):window.index("/// Present when a view is one half")]
        appkit = window[window.index("// MARK: - AppKit"):]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "HubWindowParts.swift").write_text("import AppKit\nimport SwiftUI\n" + types + appkit)
            (root / "Cases.swift").write_text(CASES)
            binary = root / "hub-window-tests"
            compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-target", platform.machine() + "-apple-macosx14.0", "-module-cache-path", str(root / "cache"),
                str(root / "HubWindowParts.swift"), str(root / "Cases.swift"),
                "-framework", "AppKit", "-framework", "SwiftUI", "-o", str(binary)],
                capture_output=True, text=True, timeout=180)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn("HUB_WINDOW_OK", ran.stdout)


TRANSCRIPT_CASES = r'''
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
        func settle(_ seconds: Double = 0.1) { RunLoop.main.run(until: Date().addingTimeInterval(seconds)) }
        let follow = ChatTranscriptFollow()
        let container = NSView(frame: NSRect(x: 0, y: 0, width: 520, height: 400))
        let host = NSHostingView(rootView: Content(follow: follow))
        host.frame = container.bounds; container.addSubview(host)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 400), styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = container; window.setFrameOrigin(NSPoint(x: -20000, y: -20000)); window.orderFrontRegardless()
        settle(); host.layoutSubtreeIfNeeded(); settle()
        func find(_ view: NSView) -> NSScrollView? {
            if let scroll = view as? NSScrollView { return scroll }
            return view.subviews.lazy.compactMap(find).first
        }
        guard let scroll = find(host), let scroller = scroll.verticalScroller else { fatalError("native viewport missing") }
        // A press where the transcript's scroller is, delivered through the
        // event queue so the probe's local monitor sees it.
        let point = scroll.convert(NSPoint(x: scroller.frame.midX, y: scroller.frame.midY), to: nil)
        func press() {
            let event = NSEvent.mouseEvent(with: .leftMouseDown, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                           windowNumber: window.windowNumber, context: nil, eventNumber: 0, clickCount: 1, pressure: 1)!
            NSApp.postEvent(event, atStart: true)
            if let next = NSApp.nextEvent(matching: .any, until: Date(), inMode: .default, dequeue: true) { NSApp.sendEvent(next) }
            settle()
        }
        press()
        check(!follow.followsLatest, "a press on the shown transcript's scroller did not pause follow")
        follow.resume(); settle(0.4)
        // Code hidden behind Providers: the same spot now belongs to the hub.
        host.isHidden = true; settle()
        press()
        check(follow.followsLatest, "a press in the other half paused the hidden transcript's follow")
        window.orderOut(nil)
        print("HIDDEN_TRANSCRIPT_OK")
    }
}
'''


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "AppKit scrolling test needs macOS")
class HiddenTranscriptTests(unittest.TestCase):
    """Code stays in the window while Providers shows; its transcript must
    not read the hub's clicks as the user scrolling Chat."""

    def test_hidden_transcript_ignores_presses_over_its_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Cases.swift").write_text(TRANSCRIPT_CASES)
            binary = root / "hidden-transcript-tests"
            compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-target", platform.machine() + "-apple-macosx14.0", "-module-cache-path", str(root / "cache"),
                str(SOURCE / "ChatTranscriptScroll.swift"), str(root / "Cases.swift"),
                "-framework", "AppKit", "-framework", "SwiftUI", "-o", str(binary)],
                capture_output=True, text=True, timeout=180)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn("HIDDEN_TRANSCRIPT_OK", ran.stdout)


if __name__ == "__main__":
    unittest.main()
