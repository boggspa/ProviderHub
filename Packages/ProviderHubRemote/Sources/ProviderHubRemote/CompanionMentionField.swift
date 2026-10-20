import SwiftUI
#if os(iOS)
import UIKit
#else
import AppKit
#endif

/// The composer field that tints `@Name` the way the desktop composer does and
/// floats the same member list while a tag is being typed.
struct CompanionMentionField: View {
    @Binding var text: String
    var placeholder: String
    var enabled: Bool
    var mentions: [CompanionMentionTarget]
    @ObservedObject var menu: CompanionMentionMenu

    var body: some View {
        // No flexible frame here: a `.frame(maxHeight:)` takes the whole
        // proposed height whenever its child is smaller, which is what grew
        // the empty composer to its ceiling. The editor sizes itself.
        MentionTextEditor(text: $text, placeholder: placeholder, enabled: enabled, mentions: mentions, menu: menu)
    }
}

/// The composer hugs its text, one line when empty, and scrolls past the
/// ceiling, as the vertical `TextField` it replaced did.
enum MentionFieldHeight {
    static let minimum: CGFloat = 36
    static let maximum: CGFloat = 140

    static func clamp(_ fitted: CGFloat) -> CGFloat { min(max(fitted.rounded(.up), minimum), maximum) }
}

enum MentionPaint {
    #if os(iOS)
    typealias Font = UIFont
    typealias Color = UIColor
    #else
    typealias Font = NSFont
    typealias Color = NSColor
    #endif

    static func plain(_ font: Font) -> [NSAttributedString.Key: Any] {
        #if os(iOS)
        [.font: font, .foregroundColor: UIColor.label]
        #else
        [.font: font, .foregroundColor: NSColor.labelColor]
        #endif
    }

    static func chip(_ hex: String, font: Font) -> [NSAttributedString.Key: Any] {
        let light = CompanionMentionStyle.blend(hex, toward: 0, fraction: 0.25)
        let dark = CompanionMentionStyle.blend(hex, toward: 1, fraction: 0.40)
        let semibold = Font.systemFont(ofSize: font.pointSize, weight: .semibold)
        return [.font: semibold, .foregroundColor: adaptive(light, dark), .backgroundColor: adaptiveFill(hex)]
    }

    /// Restyles the draft: member tags, and on iOS room after each colour
    /// code for the swatch the text view lays there. Attributes only, so the
    /// draft sends exactly as typed. Returns the codes given room.
    @discardableResult
    static func paint(_ storage: NSTextStorage, members: [CompanionMentionTarget], font: Font) -> [ChatHexColor] {
        let whole = NSRange(location: 0, length: storage.length)
        let base = plain(font)
        storage.beginEditing()
        storage.setAttributes(base, range: whole)
        let matches = CompanionMentions.resolve(storage.string, members: members)
        let string = storage.string as NSString
        for match in matches {
            let hex = match.target.accentHex.isEmpty ? "#888888" : match.target.accentHex
            storage.addAttributes(chip(hex, font: font), range: CompanionMentions.display(match.range, in: string))
        }
        #if os(iOS)
        let colors = ChatHexColor.find(in: storage.string, skippingFences: true)
        for color in colors {
            let last = string.rangeOfComposedCharacterSequence(at: NSMaxRange(color.range) - 1)
            storage.addAttribute(.kern, value: ChatHexSwatch.advance(font.pointSize), range: last)
        }
        #else
        let colors: [ChatHexColor] = []
        #endif
        storage.endEditing()
        return colors
    }

    private static func adaptive(_ light: (CGFloat, CGFloat, CGFloat), _ dark: (CGFloat, CGFloat, CGFloat)) -> Color {
        #if os(iOS)
        UIColor { traits in
            let c = traits.userInterfaceStyle == .dark ? dark : light
            return UIColor(red: c.0, green: c.1, blue: c.2, alpha: 1)
        }
        #else
        NSColor(name: nil) { appearance in
            let c = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
            return NSColor(red: c.0, green: c.1, blue: c.2, alpha: 1)
        }
        #endif
    }

    private static func adaptiveFill(_ hex: String) -> Color {
        let rgb = CompanionMentionStyle.components(hex) ?? (0.45, 0.45, 0.45)
        #if os(iOS)
        return UIColor { traits in
            let alpha: CGFloat = traits.userInterfaceStyle == .dark ? 0.26 : 0.14
            return UIColor(red: rgb.0, green: rgb.1, blue: rgb.2, alpha: alpha)
        }
        #else
        return NSColor(name: nil) { appearance in
            let alpha: CGFloat = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? 0.26 : 0.14
            return NSColor(red: rgb.0, green: rgb.1, blue: rgb.2, alpha: alpha)
        }
        #endif
    }
}

#if os(iOS)
final class MentionUITextView: UITextView {
    var placeholder = "" { didSet { setNeedsDisplay() } }
    var onEscape: (() -> Bool)?
    var onMove: ((Int) -> Bool)?
    var onAccept: (() -> Bool)?
    var menuOpen: (() -> Bool)?
    /// The colour codes the last paint made room after, with the text they
    /// were found in; a swatch is laid in each room while the text matches.
    var swatches: (text: String, colors: [ChatHexColor]) = ("", []) {
        didSet { if oldValue.text != swatches.text || oldValue.colors != swatches.colors { setNeedsLayout() } }
    }
    private var swatchLayers: [CALayer] = []

    override init(frame: CGRect, textContainer: NSTextContainer?) {
        super.init(frame: frame, textContainer: textContainer)
        // A swatch's rim is drawn for one appearance.
        registerForTraitChanges([UITraitUserInterfaceStyle.self]) { (view: MentionUITextView, _: UITraitCollection) in view.setNeedsLayout() }
    }

    required init?(coder: NSCoder) { super.init(coder: coder) }

    /// Scroll only once the text outgrows the height SwiftUI actually gave
    /// the view. Deciding here, against the placed bounds, keeps measurement
    /// probes at other widths from flipping it.
    override func layoutSubviews() {
        super.layoutSubviews()
        guard bounds.width > 0 else { return }
        let fitted = sizeThatFits(CGSize(width: bounds.width, height: .greatestFiniteMagnitude)).height
        let overflowing = fitted > bounds.height + 1
        if isScrollEnabled != overflowing { isScrollEnabled = overflowing }
        placeSwatches()
    }

    /// Each swatch goes after its code's own width, measured in the plain
    /// font, so it lands in the kerned room whatever the line's rect includes.
    /// The text input geometry answers without dropping TextKit 2.
    private func placeSwatches() {
        let colors = swatches.text == text ? swatches.colors : []
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        defer { CATransaction.commit() }
        while swatchLayers.count > colors.count { swatchLayers.removeLast().removeFromSuperlayer() }
        guard !colors.isEmpty, let font else { return }
        let size = font.pointSize, string = text as NSString
        let dark = traitCollection.userInterfaceStyle == .dark
        for (index, color) in colors.enumerated() {
            if index == swatchLayers.count {
                let swatch = CALayer()
                layer.addSublayer(swatch)
                swatchLayers.append(swatch)
            }
            let swatch = swatchLayers[index]
            guard NSMaxRange(color.range) <= string.length, let start = position(from: beginningOfDocument, offset: color.range.location),
                  let end = position(from: start, offset: color.range.length), let range = textRange(from: start, to: end) else {
                swatch.isHidden = true; continue
            }
            let line = firstRect(for: range)
            guard !line.isNull, !line.isInfinite else { swatch.isHidden = true; continue }
            let image = ChatHexSwatch.image(color, size: size, dark: dark)
            let width = string.substring(with: color.range).size(withAttributes: [.font: font]).width
            let foot = line.minY + font.ascender + ChatHexSwatch.drop(size)
            swatch.contents = image.cgImage
            swatch.contentsScale = image.scale
            swatch.frame = CGRect(x: line.minX + width, y: foot - image.size.height, width: image.size.width, height: image.size.height)
            swatch.isHidden = false
        }
    }

    override func draw(_ rect: CGRect) {
        super.draw(rect)
        guard text.isEmpty, !placeholder.isEmpty else { return }
        // One line, truncated: a long model name must end in an ellipsis
        // rather than run under the send button.
        let font = font ?? .preferredFont(forTextStyle: .body)
        let padding = textContainer.lineFragmentPadding
        let line = CGRect(x: textContainerInset.left + padding, y: textContainerInset.top,
                          width: max(0, bounds.width - textContainerInset.left - textContainerInset.right - padding * 2),
                          height: font.lineHeight)
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineBreakMode = .byTruncatingTail
        (placeholder as NSString).draw(in: line, withAttributes: [
            .font: font, .foregroundColor: UIColor.placeholderText, .paragraphStyle: paragraph])
    }

    /// The text and focus systems own these keys unless the member list is
    /// open. Only then are the list's commands offered, and with priority, so
    /// Up/Down move the highlight rather than the caret and Tab completes the
    /// tag rather than moving focus. Closed, editing keys behave as usual.
    override var keyCommands: [UIKeyCommand]? {
        guard menuOpen?() == true else { return super.keyCommands }
        var commands = super.keyCommands ?? []
        let menuKeys: [(String, Selector)] = [
            (UIKeyCommand.inputUpArrow, #selector(mentionUp)), (UIKeyCommand.inputDownArrow, #selector(mentionDown)),
            (UIKeyCommand.inputEscape, #selector(mentionEscape)), ("\t", #selector(mentionTab)),
        ]
        for (input, action) in menuKeys {
            let command = UIKeyCommand(input: input, modifierFlags: [], action: action)
            command.wantsPriorityOverSystemBehavior = true
            commands.append(command)
        }
        return commands
    }
    @objc private func mentionUp() { _ = onMove?(-1) }
    @objc private func mentionDown() { _ = onMove?(1) }
    @objc private func mentionEscape() { _ = onEscape?() }
    @objc private func mentionTab() { _ = onAccept?() }
}

struct MentionTextEditor: UIViewRepresentable {
    @Binding var text: String
    var placeholder: String
    var enabled: Bool
    var mentions: [CompanionMentionTarget]
    var menu: CompanionMentionMenu

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> MentionUITextView {
        let view = MentionUITextView()
        view.backgroundColor = .clear
        view.font = .preferredFont(forTextStyle: .body)
        view.textColor = .label
        view.isScrollEnabled = false
        view.contentMode = .redraw
        view.textContainerInset = UIEdgeInsets(top: 8, left: 0, bottom: 8, right: 0)
        view.delegate = context.coordinator
        view.menuOpen = { [weak menu = self.menu] in menu?.state != nil }
        view.onMove = { [weak menu = self.menu] step in
            guard menu?.state != nil else { return false }
            menu?.move(step); return true
        }
        view.onEscape = { [weak menu = self.menu] in
            guard menu?.state != nil else { return false }
            menu?.close(); return true
        }
        view.onAccept = { [weak coord = context.coordinator] in
            guard let coord, coord.parent.menu.state != nil, let target = coord.parent.menu.current else { return false }
            coord.accept(target, in: coord.view); return true
        }
        context.coordinator.view = view
        menu.choose = { [weak coord = context.coordinator] target in coord?.accept(target, in: coord?.view) }
        return view
    }

    /// A text view has no height of its own to offer, so without this SwiftUI
    /// hands it whatever is on offer. Measure the text at the proposed width;
    /// the view decides about scrolling once it is placed.
    func sizeThatFits(_ proposal: ProposedViewSize, uiView: MentionUITextView, context: Context) -> CGSize? {
        let width = proposal.width ?? uiView.bounds.width
        let measured = width.isFinite ? max(width, 1) : 10_000
        let fitted = uiView.sizeThatFits(CGSize(width: measured, height: .greatestFiniteMagnitude)).height
        return CGSize(width: width, height: MentionFieldHeight.clamp(fitted))
    }

    func updateUIView(_ view: MentionUITextView, context: Context) {
        context.coordinator.parent = self
        view.placeholder = placeholder
        view.isEditable = enabled
        view.menuOpen = { [weak menu] in menu?.state != nil }
        view.onMove = { [weak menu] step in
            guard menu?.state != nil else { return false }
            menu?.move(step); return true
        }
        menu.choose = { [weak coord = context.coordinator] target in coord?.accept(target, in: coord?.view) }
        context.coordinator.apply(view)
    }

    final class Coordinator: NSObject, UITextViewDelegate {
        var parent: MentionTextEditor
        weak var view: MentionUITextView?
        var applying = false
        init(_ parent: MentionTextEditor) { self.parent = parent }

        func apply(_ view: MentionUITextView) {
            guard !applying else { return }
            applying = true
            if view.text != parent.text {
                view.text = parent.text
            }
            if let font = view.font, view.markedTextRange == nil {
                let selected = view.selectedRange
                let colors = MentionPaint.paint(view.textStorage, members: parent.mentions, font: font)
                view.swatches = (view.text, colors)
                view.selectedRange = selected
            }
            view.setNeedsDisplay()
            refresh(view)
            applying = false
        }

        func textViewDidChange(_ textView: UITextView) {
            guard !applying, let view = textView as? MentionUITextView else { return }
            parent.text = view.text
            apply(view)
            view.invalidateIntrinsicContentSize()
            view.setNeedsLayout()
        }

        func textViewDidChangeSelection(_ textView: UITextView) {
            guard !applying, let view = textView as? MentionUITextView else { return }
            refresh(view)
        }

        func textView(_ textView: UITextView, shouldChangeTextIn range: NSRange, replacementText text: String) -> Bool {
            if text == "\n", parent.menu.state != nil, let view = textView as? MentionUITextView, let target = parent.menu.current {
                accept(target, in: view); return false
            }
            return true
        }

        func refresh(_ view: UITextView) {
            let selection = view.selectedRange
            let caret = selection.length == 0 && view.isEditable && view.markedTextRange == nil ? selection.location : nil
            parent.menu.update(text: view.text, caret: caret, members: parent.mentions)
        }

        func accept(_ target: CompanionMentionTarget, in view: UITextView?) {
            guard let view, let state = parent.menu.state else { return }
            let caret = view.selectedRange, string = view.text as NSString
            let location = caret.length == 0 && caret.location > state.start && caret.location <= string.length ? caret.location : state.caret
            guard location > state.start, location <= string.length else { return }
            let spaced = [0x20, 0x09, 0x0A].contains(location < string.length ? string.character(at: location) : 0)
            parent.menu.close()
            let replacement = "@" + target.name + (spaced ? "" : " ")
            view.textStorage.replaceCharacters(in: NSRange(location: state.start, length: location - state.start), with: replacement)
            parent.text = view.text
            let landed = state.start + (replacement as NSString).length + (spaced ? 1 : 0)
            view.selectedRange = NSRange(location: min(landed, (view.text as NSString).length), length: 0)
            apply(view as! MentionUITextView)
        }
    }
}
#else
final class MentionNSTextView: NSTextView {
    var placeholder = "" { didSet { needsDisplay = true } }
    var onSend: (() -> Void)?
    override func keyDown(with event: NSEvent) {
        let command = event.modifierFlags.intersection(.deviceIndependentFlagsMask).contains(.command)
        if event.keyCode == 36, command, isEditable { onSend?(); return }
        super.keyDown(with: event)
    }
    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard string.isEmpty, !placeholder.isEmpty else { return }
        let attributes: [NSAttributedString.Key: Any] = [.font: font ?? NSFont.systemFont(ofSize: 15), .foregroundColor: NSColor.placeholderTextColor]
        let origin = NSPoint(x: textContainerInset.width + (textContainer?.lineFragmentPadding ?? 0), y: textContainerInset.height)
        (placeholder as NSString).draw(at: origin, withAttributes: attributes)
    }
}

struct MentionTextEditor: NSViewRepresentable {
    @Binding var text: String
    var placeholder: String
    var enabled: Bool
    var mentions: [CompanionMentionTarget]
    var menu: CompanionMentionMenu

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        let view = MentionNSTextView()
        view.drawsBackground = false
        view.isRichText = true
        view.font = .systemFont(ofSize: 15)
        view.textContainerInset = NSSize(width: 0, height: 6)
        view.delegate = context.coordinator
        view.isVerticallyResizable = true
        view.autoresizingMask = [.width]
        scroll.documentView = view
        context.coordinator.view = view
        menu.choose = { [weak coord = context.coordinator] target in coord?.accept(target) }
        return scroll
    }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView scroll: NSScrollView, context: Context) -> CGSize? {
        guard let width = proposal.width, width.isFinite, width > 0,
              let view = scroll.documentView as? MentionNSTextView, let storage = view.textStorage else { return nil }
        let padding = (view.textContainer?.lineFragmentPadding ?? 0) * 2
        let font = view.font ?? .systemFont(ofSize: 15)
        let text = storage.length == 0 ? NSAttributedString(string: " ", attributes: [.font: font]) : storage
        let bounds = text.boundingRect(with: NSSize(width: max(1, width - padding), height: .greatestFiniteMagnitude),
                                       options: [.usesLineFragmentOrigin, .usesFontLeading])
        return CGSize(width: width, height: MentionFieldHeight.clamp(bounds.height + view.textContainerInset.height * 2))
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.parent = self
        guard let view = scroll.documentView as? MentionNSTextView else { return }
        view.placeholder = placeholder
        view.isEditable = enabled
        menu.choose = { [weak coord = context.coordinator] target in coord?.accept(target) }
        context.coordinator.apply(view)
    }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: MentionTextEditor
        weak var view: MentionNSTextView?
        var applying = false
        init(_ parent: MentionTextEditor) { self.parent = parent }

        func apply(_ view: MentionNSTextView) {
            guard !applying else { return }
            applying = true
            if view.string != parent.text { view.string = parent.text }
            if let font = view.font, !view.hasMarkedText(), let storage = view.textStorage {
                let selected = view.selectedRange()
                MentionPaint.paint(storage, members: parent.mentions, font: font)
                view.setSelectedRange(selected)
            }
            view.needsDisplay = true
            refresh(view)
            applying = false
        }

        func textDidChange(_ notification: Notification) {
            guard !applying, let view = notification.object as? MentionNSTextView else { return }
            parent.text = view.string
            apply(view)
        }

        func textViewDidChangeSelection(_ notification: Notification) {
            guard !applying, let view = notification.object as? MentionNSTextView else { return }
            refresh(view)
        }

        func textView(_ textView: NSTextView, doCommandBy selector: Selector) -> Bool {
            if let menu = Optional(parent.menu), menu.state != nil {
                switch selector {
                case #selector(NSResponder.moveUp(_:)): menu.move(-1); return true
                case #selector(NSResponder.moveDown(_:)): menu.move(1); return true
                case #selector(NSResponder.cancelOperation(_:)): menu.close(); return true
                case #selector(NSResponder.insertTab(_:)), #selector(NSResponder.insertNewline(_:)):
                    if let target = menu.current { accept(target) }; return true
                default: break
                }
            }
            return false
        }

        func refresh(_ view: NSTextView) {
            let selection = view.selectedRange()
            let caret = selection.length == 0 && view.isEditable && !view.hasMarkedText() ? selection.location : nil
            parent.menu.update(text: view.string, caret: caret, members: parent.mentions)
        }

        func accept(_ target: CompanionMentionTarget) {
            guard let view, let state = parent.menu.state else { return }
            let caret = view.selectedRange(), string = view.string as NSString
            let location = caret.length == 0 && caret.location > state.start && caret.location <= string.length ? caret.location : state.caret
            guard location > state.start, location <= string.length else { return }
            let spaced = [0x20, 0x09, 0x0A].contains(location < string.length ? string.character(at: location) : 0)
            parent.menu.close()
            let replacement = "@" + target.name + (spaced ? "" : " ")
            view.insertText(replacement, replacementRange: NSRange(location: state.start, length: location - state.start))
            parent.text = view.string
            if spaced { view.setSelectedRange(NSRange(location: view.selectedRange().location + 1, length: 0)) }
            apply(view)
        }
    }
}
#endif
