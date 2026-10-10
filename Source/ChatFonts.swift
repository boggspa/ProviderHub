import AppKit
import SwiftUI
import CoreText

/// Open-font assets are registered only for this process. Custom fonts are
/// referenced by PostScript name; the app never copies or redistributes them.
enum ChatFonts {
    struct Choice: Identifiable {
        let id: String
        let title: String
        let postScriptName: String?
        let resource: String?
    }
    static let choices: [Choice] = [
        Choice(id: "system", title: "System", postScriptName: nil, resource: nil),
        Choice(id: "monospaced", title: "System Mono", postScriptName: nil, resource: nil),
        Choice(id: "inter", title: "Inter", postScriptName: "Inter-Regular", resource: "inter/Inter[opsz,wght].ttf"),
        Choice(id: "source-serif", title: "Source Serif 4", postScriptName: "SourceSerif4Roman-Regular", resource: "sourceserif4/SourceSerif4[opsz,wght].ttf"),
        Choice(id: "jetbrains-mono", title: "JetBrains Mono", postScriptName: "JetBrainsMono-Regular", resource: "jetbrainsmono/JetBrainsMono[wght].ttf")
    ]
    private static let registered: Void = {
        if let directory = Bundle.main.resourceURL?.appendingPathComponent("worker/fonts") { register(in: directory) }
    }()

    static func register(in directory: URL) {
        for choice in choices {
            if let resource = choice.resource {
                CTFontManagerRegisterFontsForURL(directory.appendingPathComponent(resource) as CFURL, .process, nil)
            }
        }
    }

    static func selection(_ stored: String, legacyMonospaced: Bool) -> String {
        if stored == "custom" || choices.contains(where: { $0.id == stored }) { return stored }
        return legacyMonospaced ? "monospaced" : "system"
    }

    /// `size` is the stored preference and stays bounded; `scale` is the
    /// reading zoom, applied after the bound so a large preference can still
    /// be zoomed.
    static func font(_ selection: String, customName: String = "", size: Double, scale: Double = 1) -> NSFont {
        _ = registered
        let size = (size.isFinite ? min(max(size, 10), 32) : 13) * (scale.isFinite && scale > 0 ? scale : 1)
        if selection == "monospaced" { return .monospacedSystemFont(ofSize: size, weight: .regular) }
        let name = selection == "custom" ? customName : choices.first { $0.id == selection }?.postScriptName ?? ""
        return NSFont(name: name, size: size) ?? .systemFont(ofSize: size)
    }
}

struct ChatTextStyle {
    var size: Double = 13
    var selection = "system"
    var customName = ""
    /// Reading zoom for transcript and composer text; chrome ignores it.
    var zoom: Double = 1
    var nsFont: NSFont { ChatFonts.font(selection, customName: customName, size: size, scale: zoom) }
    var font: Font { Font(nsFont) }
    var editorFont: NSFont { ChatFonts.font(selection, customName: customName, size: size + 1, scale: zoom) }
    /// A transcript measurement (row text, glyphs, gaps) at the current zoom.
    func scaled(_ value: CGFloat) -> CGFloat { value * CGFloat(zoom.isFinite && zoom > 0 ? zoom : 1) }
    /// System text for transcript rows that is not the reading font: headers,
    /// tool rows, notices. It zooms with the reading text.
    func system(_ size: CGFloat, weight: Font.Weight = .regular, design: Font.Design = .default) -> Font {
        .system(size: scaled(size), weight: weight, design: design)
    }
}

/// Chat's reading zoom: ⌘+, ⌘− and ⌘0, or the header's − 100% + control.
/// It multiplies transcript and composer text on top of the Text size
/// preference, the way a browser zooms a page, while the header, rail, footer
/// and buttons keep their size. Steps run to three times for readers who want
/// larger, much larger or much, much larger print.
enum ChatZoom {
    static let defaultsKey = "chatZoom"
    static let steps: [Double] = [0.85, 1, 1.15, 1.3, 1.5, 1.75, 2, 2.5, 3]

    static func clamp(_ value: Double) -> Double {
        value.isFinite ? min(max(value, steps[0]), steps[steps.count - 1]) : 1
    }
    static func larger(_ value: Double) -> Double { steps.first { $0 > clamp(value) + 0.001 } ?? steps[steps.count - 1] }
    static func smaller(_ value: Double) -> Double { steps.last { $0 < clamp(value) - 0.001 } ?? steps[0] }
    static func percent(_ value: Double) -> String { "\(Int((clamp(value) * 100).rounded()))%" }

    static func stored(_ defaults: UserDefaults = .standard) -> Double {
        clamp(defaults.object(forKey: defaultsKey) as? Double ?? 1)
    }
    static func zoomIn(_ defaults: UserDefaults = .standard) { defaults.set(larger(stored(defaults)), forKey: defaultsKey) }
    static func zoomOut(_ defaults: UserDefaults = .standard) { defaults.set(smaller(stored(defaults)), forKey: defaultsKey) }
    static func reset(_ defaults: UserDefaults = .standard) { defaults.set(1.0, forKey: defaultsKey) }
}

/// Target of the View menu's zoom items. They act only while the Chat window
/// is key, so ⌘+ in the Hub's own window never changes text out of sight.
@MainActor
final class ChatZoomCommands: NSObject, NSMenuItemValidation {
    static let shared = ChatZoomCommands()
    weak var window: NSWindow?

    @objc func zoomIn(_ sender: Any?) { ChatZoom.zoomIn() }
    @objc func zoomOut(_ sender: Any?) { ChatZoom.zoomOut() }
    @objc func actualSize(_ sender: Any?) { ChatZoom.reset() }

    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        guard let window, window.isKeyWindow else { return false }
        let zoom = ChatZoom.stored()
        switch item.action {
        case #selector(zoomIn(_:)): return zoom < ChatZoom.steps[ChatZoom.steps.count - 1]
        case #selector(zoomOut(_:)): return zoom > ChatZoom.steps[0]
        default: return true
        }
    }

    /// A View menu with Zoom In (⌘+, and ⌘= without Shift), Zoom Out (⌘−)
    /// and Actual Size (⌘0).
    func menu() -> NSMenu {
        let menu = NSMenu(title: "View")
        func add(_ title: String, _ action: Selector, _ key: String, hidden: Bool = false) {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
            item.target = self
            if hidden { item.isHidden = true; item.allowsKeyEquivalentWhenHidden = true }
            menu.addItem(item)
        }
        add("Zoom In", #selector(zoomIn(_:)), "+")
        add("Zoom In", #selector(zoomIn(_:)), "=", hidden: true)
        add("Zoom Out", #selector(zoomOut(_:)), "-")
        add("Actual Size", #selector(actualSize(_:)), "0")
        return menu
    }
}
private struct ChatTextStyleKey: EnvironmentKey { static let defaultValue = ChatTextStyle() }
extension EnvironmentValues {
    var chatTextStyle: ChatTextStyle {
        get { self[ChatTextStyleKey.self] }
        set { self[ChatTextStyleKey.self] = newValue }
    }
}

/// Target the shared native panel explicitly so composer focus cannot turn a
/// font selection into a rich-text-only edit or change an unrelated control.
@MainActor
final class ChatFontPanelController: NSObject, NSFontChanging {
    static let shared = ChatFontPanelController()
    private var selectedFont = NSFont.systemFont(ofSize: 13)
    private var defaults = UserDefaults.standard
    private weak var previousTarget: AnyObject?
    private var previousAction: Selector?
    private var ownsPanel = false

    func show(selection: String, customName: String, size: Double) {
        selectedFont = ChatFonts.font(selection, customName: customName, size: size)
        let manager = NSFontManager.shared
        if !ownsPanel {
            previousTarget = manager.target
            previousAction = manager.action
            ownsPanel = true
            NotificationCenter.default.addObserver(self, selector: #selector(panelWillClose(_:)),
                name: NSWindow.willCloseNotification, object: manager.fontPanel(true))
        }
        manager.target = self
        manager.action = #selector(changeFont(_:))
        manager.setSelectedFont(selectedFont, isMultiple: false)
        manager.orderFrontFontPanel(nil)
    }

    func dismiss() {
        guard ownsPanel else { return }
        if NSFontManager.shared.target === self { NSFontManager.shared.fontPanel(false)?.close() }
        releasePanel()
    }

    @objc private func panelWillClose(_ notification: Notification) { releasePanel() }

    private func releasePanel() {
        guard ownsPanel else { return }
        let manager = NSFontManager.shared
        NotificationCenter.default.removeObserver(self, name: NSWindow.willCloseNotification, object: manager.fontPanel(false))
        if manager.target === self {
            manager.target = previousTarget
            if let previousAction { manager.action = previousAction }
        }
        previousTarget = nil; previousAction = nil; ownsPanel = false
    }

    @objc func changeFont(_ sender: NSFontManager?) {
        let manager = sender ?? .shared
        selectedFont = manager.convert(selectedFont)
        apply(selectedFont, to: defaults)
    }

    func apply(_ font: NSFont, to defaults: UserDefaults) {
        defaults.set(font.fontName, forKey: "chatCustomFontName")
        defaults.set(min(max(font.pointSize, 10), 32), forKey: "chatTextSize")
        defaults.set("custom", forKey: "chatFontChoice")
    }
}
