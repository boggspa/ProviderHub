import AppKit
import SwiftUI

/// Transcript prose as a native, non-editable NSTextView, so reading behaves
/// like any Mac document: drag to select, double-click a word, triple-click a
/// paragraph, ⌘C, links that open on click, and the system right-click menu
/// (Copy, Look Up, Translate, Share, Services…) with **Copy Message** added
/// for the whole entry. SwiftUI's selectable Text could not offer this: the
/// row's own context menu replaced the system one.
///
/// TextKit 1 is used for both drawing and measuring, so the height SwiftUI
/// is given is exactly the height the text view lays out. Inline Markdown
/// arrives as SwiftUI presentation intents and is mapped to real fonts here.
struct ChatSelectableText: NSViewRepresentable {
    var text: AttributedString
    var font: NSFont
    var color: NSColor
    /// The whole entry for **Copy Message**; nil leaves the item out.
    var message: String?

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> TextView {
        let view = TextView(usingTextLayoutManager: false)
        view.isEditable = false
        view.isSelectable = true
        view.isRichText = true
        view.drawsBackground = false
        view.allowsUndo = false
        view.isAutomaticLinkDetectionEnabled = false
        view.textContainerInset = .zero
        view.textContainer?.lineFragmentPadding = 0
        view.textContainer?.widthTracksTextView = true
        view.isVerticallyResizable = false
        view.isHorizontallyResizable = false
        view.linkTextAttributes = [.foregroundColor: NSColor.linkColor, .cursor: NSCursor.pointingHand]
        return view
    }

    func updateNSView(_ view: TextView, context: Context) {
        let attributed = context.coordinator.attributed(for: self)
        if view.textStorage?.isEqual(to: attributed) == false { view.textStorage?.setAttributedString(attributed) }
        view.message = message
    }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView: TextView, context: Context) -> CGSize? {
        _ = context.coordinator.attributed(for: self)
        let width = proposal.width.flatMap { $0.isFinite && $0 > 0 ? $0 : nil } ?? 10_000
        return context.coordinator.size(width: width)
    }

    /// Owns a private TextKit 1 stack for measuring, so sizing never touches
    /// the displayed view's layout or selection.
    final class Coordinator {
        private var source: (text: AttributedString, font: NSFont, color: NSColor)?
        private var converted = NSAttributedString()
        private let storage = NSTextStorage()
        private let layout = NSLayoutManager()
        private let container = NSTextContainer()
        private var measured: [CGFloat: CGSize] = [:]

        init() {
            container.lineFragmentPadding = 0
            layout.addTextContainer(container)
            storage.addLayoutManager(layout)
        }

        func attributed(for view: ChatSelectableText) -> NSAttributedString {
            if let source, source.text == view.text, source.font == view.font, source.color === view.color { return converted }
            source = (view.text, view.font, view.color)
            converted = ChatSelectableText.attributed(view.text, font: view.font, color: view.color)
            storage.setAttributedString(converted)
            measured = [:]
            return converted
        }

        /// The laid-out size at a width: the full height, and the longest line
        /// rather than the whole width, so a short message keeps a short bubble.
        func size(width: CGFloat) -> CGSize {
            if let size = measured[width] { return size }
            container.size = CGSize(width: width, height: .greatestFiniteMagnitude)
            layout.ensureLayout(for: container)
            let used = layout.usedRect(for: container)
            let size = CGSize(width: min(width, ceil(used.width)), height: ceil(used.height))
            measured[width] = size
            return size
        }
    }

    /// Inline Markdown as real fonts and attributes: strong, emphasis, code,
    /// strikethrough and links. Unparsed text keeps the base font and colour.
    static func attributed(_ text: AttributedString, font: NSFont, color: NSColor) -> NSAttributedString {
        let result = NSMutableAttributedString()
        for run in text.runs {
            var runFont = font
            var attributes: [NSAttributedString.Key: Any] = [.foregroundColor: color]
            let intent = run.inlinePresentationIntent ?? []
            if intent.contains(.code) { runFont = .monospacedSystemFont(ofSize: font.pointSize * 0.94, weight: .regular) }
            if intent.contains(.stronglyEmphasized) { runFont = bold(runFont) }
            if intent.contains(.emphasized) {
                let italic = NSFontManager.shared.convert(runFont, toHaveTrait: .italicFontMask)
                if italic.fontDescriptor.symbolicTraits.contains(.italic) { runFont = italic } else { attributes[.obliqueness] = 0.18 }
            }
            if intent.contains(.strikethrough) { attributes[.strikethroughStyle] = NSUnderlineStyle.single.rawValue }
            if let link = run.link { attributes[.link] = link }
            attributes[.font] = runFont
            result.append(NSAttributedString(string: String(text[run.range].characters), attributes: attributes))
        }
        return result
    }

    /// Bold that also works for the bundled variable fonts, whose bold face
    /// the font manager cannot always find by trait.
    private static func bold(_ font: NSFont) -> NSFont {
        let converted = NSFontManager.shared.convert(font, toHaveTrait: .boldFontMask)
        if converted.fontDescriptor.symbolicTraits.contains(.bold) { return converted }
        let descriptor = font.fontDescriptor.addingAttributes([.traits: [NSFontDescriptor.TraitKey.weight: NSFont.Weight.bold.rawValue]])
        if let weighted = NSFont(descriptor: descriptor, size: font.pointSize), weighted.fontName != font.fontName { return weighted }
        return .systemFont(ofSize: font.pointSize, weight: .bold)
    }

    final class TextView: NSTextView {
        var message: String?
        /// Injectable so tests never touch the user's clipboard.
        var pasteboard = NSPasteboard.general

        override func menu(for event: NSEvent) -> NSMenu? {
            // A copy: the system may hand back a shared default menu, and
            // inserting into that would add the item again on every click.
            let menu = (super.menu(for: event)?.copy() as? NSMenu) ?? NSMenu()
            guard message != nil else { return menu }
            let item = NSMenuItem(title: "Copy Message", action: #selector(copyMessage(_:)), keyEquivalent: "")
            item.target = self
            let copy = menu.items.firstIndex { $0.action == #selector(NSText.copy(_:)) }
            menu.insertItem(item, at: copy.map { $0 + 1 } ?? 0)
            return menu
        }

        @objc func copyMessage(_ sender: Any?) {
            guard let message else { return }
            pasteboard.clearContents()
            pasteboard.setString(message, forType: .string)
        }
    }
}
