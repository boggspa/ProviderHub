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

    static func font(_ selection: String, customName: String = "", size: Double) -> NSFont {
        _ = registered
        let size = size.isFinite ? min(max(size, 10), 32) : 13
        if selection == "monospaced" { return .monospacedSystemFont(ofSize: size, weight: .regular) }
        let name = selection == "custom" ? customName : choices.first { $0.id == selection }?.postScriptName ?? ""
        return NSFont(name: name, size: size) ?? .systemFont(ofSize: size)
    }
}

struct ChatTextStyle {
    var size: Double = 13
    var selection = "system"
    var customName = ""
    var font: Font { Font(ChatFonts.font(selection, customName: customName, size: size)) }
    var editorFont: NSFont { ChatFonts.font(selection, customName: customName, size: size + 1) }
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
