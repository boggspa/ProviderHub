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

    func makeNSView(context: Context) -> TextView { Self.makeTextView() }

    static func makeTextView() -> TextView {
        let view = TextView(usingTextLayoutManager: false)
        view.textContainer?.replaceLayoutManager(ChatLayoutManager())
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
        /// A rule runs across the column, so its block takes the full width.
        private var fillsWidth = false
        private let storage = NSTextStorage()
        private let layout = ChatLayoutManager()
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
            fillsWidth = false
            converted.enumerateAttribute(.chatRule, in: NSRange(location: 0, length: converted.length)) { value, _, stop in
                if value != nil { fillsWidth = true; stop.pointee = true }
            }
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
            let size = CGSize(width: fillsWidth ? width : min(width, ceil(used.width)), height: ceil(used.height))
            measured[width] = size
            return size
        }
    }

    /// Inline Markdown as real fonts and attributes: strong, emphasis, code
    /// chips, strikethrough and links; block styles become heading fonts,
    /// hanging list indents, quotes and rules. Unparsed text keeps the base
    /// font and colour.
    static func attributed(_ text: AttributedString, font: NSFont, color: NSColor) -> NSAttributedString {
        let result = NSMutableAttributedString()
        let size = font.pointSize
        let chip = NSColor(HubTheme.Semantic.selection)
        var headings: [Int: NSFont] = [:]
        for run in text.runs {
            let block = run[ChatBlockStyleAttribute.self]
            var base = font
            var attributes: [NSAttributedString.Key: Any] = [.foregroundColor: color]
            switch block?.kind {
            case .heading(let level)?:
                if headings[level] == nil {
                    let scaled = NSFont(descriptor: font.fontDescriptor, size: size * (level == 1 ? 1.25 : level == 2 ? 1.12 : 1)) ?? font
                    headings[level] = semibold(scaled)
                }
                base = headings[level]!
            case .quote?:
                attributes[.foregroundColor] = NSColor(HubTheme.Semantic.secondaryInk)
                attributes[.chatQuoteBar] = NSColor(HubTheme.Semantic.selection)
            case .rule?:
                attributes[.foregroundColor] = NSColor.clear
                attributes[.chatRule] = NSColor(HubTheme.Semantic.hairline)
            default: break
            }
            if let block { attributes[.chatBlockStyle] = ChatBlockStyleBox(block) }
            var runFont = base
            let intent = run.inlinePresentationIntent ?? []
            if intent.contains(.code) {
                runFont = .monospacedSystemFont(ofSize: base.pointSize * 0.94, weight: .regular)
                attributes[.chatCodeChip] = chip
            }
            if let accent = run[ChatMentionAttribute.self] {
                runFont = semibold(runFont)
                attributes[.foregroundColor] = ChatMentionStyle.ink(accent.color)
                attributes[.chatCodeChip] = ChatMentionStyle.fill(accent.color)
            }
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
        guard result.length > 0 else { return result }
        let string = result.string as NSString
        let whole = NSRange(location: 0, length: result.length)
        ChatLayoutManager.padChips(result)

        var location = 0
        while location < string.length {
            let paragraph = string.paragraphRange(for: NSRange(location: location, length: 0))
            let style = (result.attribute(.chatBlockStyle, at: paragraph.location, effectiveRange: nil) as? ChatBlockStyleBox)?.style
            let lead = (result.attribute(.chatCodeChip, at: paragraph.location, effectiveRange: nil) == nil ? nil
                : result.attribute(.font, at: paragraph.location, effectiveRange: nil) as? NSFont).map(ChatLayoutManager.chipPadding) ?? 0
            if style != nil || lead > 0 {
                result.addAttribute(.paragraphStyle, value: paragraphStyle(style, size: size, lead: lead, first: paragraph.location == 0),
                                    range: paragraph)
            }
            location = NSMaxRange(paragraph)
        }
        result.removeAttribute(.chatBlockStyle, range: whole)
        return result
    }

    private static func paragraphStyle(_ style: ChatBlockStyle?, size: CGFloat, lead: CGFloat, first: Bool) -> NSParagraphStyle {
        let paragraph = NSMutableParagraphStyle()
        if let style {
            paragraph.firstLineHeadIndent = style.first * size
            paragraph.headIndent = style.content * size
            switch style.kind {
            case .heading(let level):
                paragraph.paragraphSpacingBefore = first ? 0 : size * (level <= 2 ? 0.6 : 0.45)
                paragraph.paragraphSpacing = size * 0.15
            case .item:
                paragraph.tabStops = [NSTextTab(textAlignment: .right, location: style.marker * size),
                                      NSTextTab(textAlignment: .left, location: style.content * size)]
                paragraph.paragraphSpacingBefore = first ? 0 : size * 0.15
            case .quote:
                paragraph.firstLineHeadIndent = size * 0.9
                paragraph.headIndent = size * 0.9
            case .continuation, .rule: break
            }
        }
        paragraph.firstLineHeadIndent += lead
        return paragraph
    }

    static func semibold(_ font: NSFont) -> NSFont {
        if font.familyName == NSFont.systemFont(ofSize: font.pointSize).familyName {
            return .systemFont(ofSize: font.pointSize, weight: .semibold)
        }
        let descriptor = font.fontDescriptor.addingAttributes([.traits: [NSFontDescriptor.TraitKey.weight: NSFont.Weight.semibold.rawValue]])
        if let weighted = NSFont(descriptor: descriptor, size: font.pointSize), weighted.fontName != font.fontName { return weighted }
        return bold(font)
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

extension NSAttributedString.Key {
    /// Fill colour for an inline code chip, or a member tag's tinted chip.
    static let chatCodeChip = NSAttributedString.Key("ProviderHub.chatCodeChip")
    /// Colour of a blockquote's leading bar.
    static let chatQuoteBar = NSAttributedString.Key("ProviderHub.chatQuoteBar")
    /// Colour of a thematic break drawn across its line.
    static let chatRule = NSAttributedString.Key("ProviderHub.chatRule")
    /// Carries a block style to the paragraph pass; removed before display.
    fileprivate static let chatBlockStyle = NSAttributedString.Key("ProviderHub.chatBlockStyle")
}

private final class ChatBlockStyleBox: NSObject {
    let style: ChatBlockStyle
    init(_ style: ChatBlockStyle) { self.style = style }
}

/// A member tag's look, shared by the composer and the transcript: semibold
/// text in the member's provider accent on a chip of the same hue. The text is
/// pulled toward the ink so every accent stays legible in either appearance,
/// on the composer and on a message bubble alike.
enum ChatMentionStyle {
    static func ink(_ accent: NSColor) -> NSColor {
        let rgb = accent.usingColorSpace(.sRGB) ?? accent
        return HubTheme.Semantic.dynamicNS(light: rgb.blended(withFraction: 0.25, of: .black) ?? rgb,
                                           dark: rgb.blended(withFraction: 0.4, of: .white) ?? rgb)
    }

    static func fill(_ accent: NSColor) -> NSColor {
        let rgb = accent.usingColorSpace(.sRGB) ?? accent
        return HubTheme.Semantic.dynamicNS(light: rgb.withAlphaComponent(0.14), dark: rgb.withAlphaComponent(0.26))
    }

    static func attributes(_ accent: NSColor, font: NSFont) -> [NSAttributedString.Key: Any] {
        [.font: ChatSelectableText.semibold(font), .foregroundColor: ink(accent), .chatCodeChip: fill(accent)]
    }
}

/// TextKit 1 layout that draws the transcript's block decorations behind
/// the text: rounded code chips (one per line fragment when a chip wraps),
/// a blockquote's bar and a rule's hairline. Layout itself is the stock
/// typesetter, so the measuring stack and the view agree on every height.
final class ChatLayoutManager: NSLayoutManager {
    static func chipPadding(_ font: NSFont) -> CGFloat { font.pointSize * 0.22 }

    /// Room around each chip, so its fill never touches its neighbours.
    static func padChips(_ text: NSMutableAttributedString) {
        let string = text.string as NSString
        var chips: [NSRange] = []
        text.enumerateAttribute(.chatCodeChip, in: NSRange(location: 0, length: text.length)) { value, range, _ in
            if value != nil { chips.append(range) }
        }
        for range in chips {
            guard let font = text.attribute(.font, at: range.location, effectiveRange: nil) as? NSFont else { continue }
            let kern = chipPadding(font) + font.pointSize * 0.08
            text.addAttribute(.kern, value: kern, range: string.rangeOfComposedCharacterSequence(at: NSMaxRange(range) - 1))
            if range.location > 0, ![9, 10].contains(string.character(at: range.location - 1)) {
                text.addAttribute(.kern, value: kern, range: string.rangeOfComposedCharacterSequence(at: range.location - 1))
            }
        }
    }

    override func drawBackground(forGlyphRange glyphsToShow: NSRange, at origin: NSPoint) {
        if let storage = textStorage, storage.length > 0 {
            let characters = characterRange(forGlyphRange: glyphsToShow, actualGlyphRange: nil)
            let all = NSRange(location: 0, length: storage.length)
            storage.enumerateAttribute(.chatQuoteBar, in: characters) { value, range, _ in
                guard let color = value as? NSColor, let font = storage.attribute(.font, at: range.location, effectiveRange: nil) as? NSFont else { return }
                color.setFill()
                enumerateLineFragments(forGlyphRange: glyphRange(forCharacterRange: range, actualCharacterRange: nil)) { rect, _, _, _, _ in
                    NSBezierPath(roundedRect: NSRect(x: origin.x + rect.minX, y: origin.y + rect.minY, width: max(2, font.pointSize / 6.5),
                                                     height: rect.height), xRadius: 1, yRadius: 1).fill()
                }
            }
            storage.enumerateAttribute(.chatRule, in: characters) { value, range, _ in
                guard let color = value as? NSColor else { return }
                color.setFill()
                enumerateLineFragments(forGlyphRange: glyphRange(forCharacterRange: range, actualCharacterRange: nil)) { rect, _, _, _, _ in
                    NSBezierPath.fill(NSRect(x: origin.x + rect.minX, y: origin.y + rect.midY.rounded(.down), width: rect.width, height: 1))
                }
            }
            storage.enumerateAttribute(.chatCodeChip, in: characters) { value, range, _ in
                guard let color = value as? NSColor else { return }
                var chip = range
                _ = storage.attribute(.chatCodeChip, at: range.location, longestEffectiveRange: &chip, in: all)
                drawChip(chip, color: color, origin: origin)
            }
        }
        // Selection paints over the chips.
        super.drawBackground(forGlyphRange: glyphsToShow, at: origin)
    }

    private func drawChip(_ characters: NSRange, color: NSColor, origin: NSPoint) {
        guard let storage = textStorage, let font = storage.attribute(.font, at: characters.location, effectiveRange: nil) as? NSFont else { return }
        let glyphs = glyphRange(forCharacterRange: characters, actualCharacterRange: nil)
        let string = storage.string as NSString
        let pad = Self.chipPadding(font), lift = font.pointSize * 0.12, radius = font.pointSize * 0.3
        color.setFill()
        var glyph = glyphs.location
        while glyph < NSMaxRange(glyphs) {
            var line = NSRange()
            let fragment = lineFragmentRect(forGlyphAt: glyph, effectiveRange: &line)
            let segment = NSIntersectionRange(line, glyphs)
            guard segment.length > 0 else { break }
            glyph = NSMaxRange(segment)
            let finalSegment = NSMaxRange(segment) == NSMaxRange(glyphs)
            // A wrapped segment ends at the break; trailing spaces there are
            // laid out past the line and must not stretch the fill.
            var last = NSMaxRange(segment) - 1
            while !finalSegment, last > segment.location,
                  let scalar = UnicodeScalar(string.character(at: characterIndexForGlyph(at: last))),
                  CharacterSet.whitespacesAndNewlines.contains(scalar) { last -= 1 }
            let start = location(forGlyphAt: segment.location), end = location(forGlyphAt: last)
            var minX = fragment.minX + start.x
            var maxX = fragment.minX + end.x + font.advancement(forCGGlyph: cgGlyph(at: last)).width
            if segment.location == glyphs.location { minX -= pad }
            if finalSegment { maxX += pad }
            minX = max(minX, fragment.minX)
            guard maxX > minX else { continue }
            let baseline = fragment.minY + start.y
            let rect = NSRect(x: origin.x + minX, y: origin.y + baseline - font.ascender - lift,
                              width: maxX - minX, height: font.ascender - font.descender + lift * 2)
            NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius).fill()
        }
    }
}
