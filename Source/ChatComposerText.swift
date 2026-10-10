import AppKit
import SwiftUI

/// A plain NSTextView: Return and ⌘Return send, Shift-Return and Option-Return
/// insert a line, and the view grows with its text up to a few lines.
struct ComposerTextView: NSViewRepresentable {
    @Environment(\.chatTextStyle) private var textStyle
    @Binding var text: String
    @Binding var height: CGFloat
    var placeholder: String
    var enabled: Bool
    var onSend: () -> Void
    /// Files or an image on the pasteboard; return true to consume the paste.
    var onPasteFiles: (([URL]) -> Bool)? = nil
    /// Team members `@` can address. Their tags are tinted exactly as the
    /// worker will route them; outside a Team the list is empty.
    var mentions: [ChatMentionTarget] = []
    /// The `@` list's state, when the composer floats one.
    var mentionMenu: ChatMentionMenu? = nil

    private static let minHeight: CGFloat = 22
    private static let maxHeight: CGFloat = 160

    final class SendTextView: NSTextView {
        var onSend: (() -> Void)?
        var onPasteFiles: (([URL]) -> Bool)?
        var placeholder = "" { didSet { if placeholder != oldValue { needsDisplay = true } } }

        override func keyDown(with event: NSEvent) {
            let command = event.modifierFlags.intersection(.deviceIndependentFlagsMask).contains(.command)
            if event.keyCode == 36, command, isEditable { onSend?(); return }
            super.keyDown(with: event)
        }

        /// Pasted files and images become attachments, matching + and drop.
        /// An image with no file behind it (a copied screenshot) is written to
        /// a temporary PNG first. Text still pastes as text.
        override func paste(_ sender: Any?) {
            if isEditable, let onPasteFiles, let urls = Self.pastedFiles(NSPasteboard.general), onPasteFiles(urls) { return }
            super.paste(sender)
        }

        static func pastedFiles(_ board: NSPasteboard) -> [URL]? {
            if let urls = board.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL], !urls.isEmpty {
                return urls
            }
            // Text wins over an image unless the text is only the image's URL.
            let text = board.string(forType: .string)?.trimmingCharacters(in: .whitespacesAndNewlines)
            let textIsLink = text.map { $0.range(of: #"^https?://\S+$"#, options: .regularExpression) != nil } ?? true
            guard textIsLink, let image = NSImage(pasteboard: board), let tiff = image.tiffRepresentation,
                  let bitmap = NSBitmapImageRep(data: tiff), let png = bitmap.representation(using: .png, properties: [:]) else { return nil }
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent("ProviderHubChatPaste", isDirectory: true)
            let formatter = DateFormatter(); formatter.dateFormat = "yyyy-MM-dd 'at' HH.mm.ss"
            let stem = "Pasted image " + formatter.string(from: Date())
            do {
                try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
                var url = directory.appendingPathComponent(stem + ".png")
                var copy = 2
                while FileManager.default.fileExists(atPath: url.path) { url = directory.appendingPathComponent("\(stem) \(copy).png"); copy += 1 }
                try png.write(to: url)
                return [url]
            } catch { return nil }
        }

        override func draw(_ dirtyRect: NSRect) {
            super.draw(dirtyRect)
            guard string.isEmpty, !placeholder.isEmpty else { return }
            let attributes: [NSAttributedString.Key: Any] = [.font: font ?? NSFont.systemFont(ofSize: 14), .foregroundColor: NSColor.placeholderTextColor]
            let origin = NSPoint(x: textContainerInset.width + (textContainer?.lineFragmentPadding ?? 0), y: textContainerInset.height)
            (placeholder as NSString).draw(at: origin, withAttributes: attributes)
        }
    }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: ComposerTextView
        /// What the view was last given, so an unchanged SwiftUI update
        /// neither restyles the text nor repaints its tags.
        var font: NSFont?
        var members: [ChatMentionTarget] = []
        /// True while SwiftUI replaces the text, when nothing may be published.
        var applying = false
        init(_ parent: ComposerTextView) { self.parent = parent }

        var plain: [NSAttributedString.Key: Any] { [.font: font ?? NSFont.systemFont(ofSize: 14), .foregroundColor: NSColor.labelColor] }

        func textDidChange(_ notification: Notification) {
            guard let view = notification.object as? NSTextView else { return }
            parent.text = view.string
            paint(view)
            parent.measure(view)
            refreshMenu(view)
        }

        func textViewDidChangeSelection(_ notification: Notification) {
            guard !applying, let view = notification.object as? NSTextView else { return }
            refreshMenu(view)
        }

        /// Typed text never takes on a neighbouring tag's chip, colour or weight.
        func textView(_ textView: NSTextView, shouldChangeTypingAttributes oldTypingAttributes: [String: Any],
                      toAttributes newTypingAttributes: [NSAttributedString.Key: Any]) -> [NSAttributedString.Key: Any] {
            plain
        }

        /// Tints each tag the worker would route, the way the transcript will
        /// draw it. Only earlier tags are cleared; the rest of the text is
        /// never restyled, so a long draft stays cheap to edit.
        func paint(_ view: NSTextView) {
            guard let storage = view.textStorage, let font, !view.hasMarkedText() else { return }
            let whole = NSRange(location: 0, length: storage.length)
            var stale: [NSRange] = []
            for key in [NSAttributedString.Key.chatCodeChip, .kern] {
                storage.enumerateAttribute(key, in: whole) { value, range, _ in if value != nil { stale.append(range) } }
            }
            let matches = ChatMentions.resolve(view.string, members: parent.mentions)
            guard !stale.isEmpty || !matches.isEmpty else { return }
            let string = view.string as NSString
            storage.beginEditing()
            for range in stale { storage.setAttributes(plain, range: range) }
            for match in matches {
                storage.addAttributes(ChatMentionStyle.attributes(match.target.accent, font: font),
                                      range: ChatMentions.display(match.range, in: string))
            }
            ChatLayoutManager.padChips(storage)
            storage.endEditing()
        }

        func refreshMenu(_ view: NSTextView) {
            guard let menu = parent.mentionMenu else { return }
            let selection = view.selectedRange()
            let caret = selection.length == 0 && view.isEditable && !view.hasMarkedText() ? selection.location : nil
            menu.update(text: view.string, caret: caret, members: parent.mentions)
        }

        /// The typed tag becomes the member's whole name and a space, as one
        /// undoable step, with the caret after it.
        func accept(_ target: ChatMentionTarget, in view: NSTextView) {
            guard let menu = parent.mentionMenu, let state = menu.state else { return }
            let caret = view.selectedRange(), string = view.string as NSString
            guard caret.length == 0, caret.location > state.start, caret.location <= string.length else { return }
            let next = caret.location < string.length ? string.character(at: caret.location) : 0
            let spaced = [0x20, 0x09, 0x0A].contains(next)
            menu.close()
            view.breakUndoCoalescing()
            view.insertText("@" + target.name + (spaced ? "" : " "),
                            replacementRange: NSRange(location: state.start, length: caret.location - state.start))
            view.breakUndoCoalescing()
            if spaced { view.setSelectedRange(NSRange(location: view.selectedRange().location + 1, length: 0)) }
            view.window?.makeFirstResponder(view)
        }

        func textView(_ textView: NSTextView, doCommandBy selector: Selector) -> Bool {
            let flags = NSApp.currentEvent?.modifierFlags.intersection(.deviceIndependentFlagsMask) ?? []
            let plainReturn = !flags.contains(.shift) && !flags.contains(.option)
            // While the @ list is open it owns the arrows, Tab, Escape and a plain Return.
            if let menu = parent.mentionMenu, menu.state != nil {
                switch selector {
                case #selector(NSResponder.moveUp(_:)): menu.move(-1); return true
                case #selector(NSResponder.moveDown(_:)): menu.move(1); return true
                case #selector(NSResponder.cancelOperation(_:)): menu.close(); return true
                case #selector(NSResponder.insertTab(_:)):
                    if let target = menu.current { accept(target, in: textView) }
                    return true
                case #selector(NSResponder.insertNewline(_:)) where plainReturn:
                    if let target = menu.current { accept(target, in: textView) }
                    return true
                default: break
                }
            }
            guard selector == #selector(NSResponder.insertNewline(_:)) else { return false }
            if !plainReturn { return false }
            parent.onSend()
            return true
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.drawsBackground = false; scroll.borderType = .noBorder
        scroll.hasVerticalScroller = true; scroll.autohidesScrollers = true
        let view = SendTextView(usingTextLayoutManager: false)
        // The transcript's layout manager, so a tag's chip draws here as it will there.
        view.textContainer?.replaceLayoutManager(ChatLayoutManager())
        view.delegate = context.coordinator
        view.drawsBackground = false
        view.font = .systemFont(ofSize: 14); view.textColor = .labelColor
        view.isRichText = false; view.allowsUndo = true; view.usesFindBar = false
        view.isAutomaticQuoteSubstitutionEnabled = false; view.isAutomaticDashSubstitutionEnabled = false
        view.textContainerInset = NSSize(width: 0, height: 3)
        view.isVerticallyResizable = true; view.isHorizontallyResizable = false
        view.autoresizingMask = [.width]
        view.minSize = .zero
        view.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        view.textContainer?.widthTracksTextView = true
        view.textContainer?.containerSize = NSSize(width: 0, height: CGFloat.greatestFiniteMagnitude)
        view.setAccessibilityLabel("Message")
        scroll.documentView = view
        let coordinator = context.coordinator
        mentionMenu?.choose = { [weak view, weak coordinator] target in
            if let view { coordinator?.accept(target, in: view) }
        }
        DispatchQueue.main.async { view.window?.makeFirstResponder(view) }
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        let coordinator = context.coordinator
        coordinator.parent = self
        guard let view = scroll.documentView as? SendTextView else { return }
        view.onSend = onSend
        view.onPasteFiles = onPasteFiles
        view.placeholder = placeholder
        view.isEditable = enabled
        // Assigning the font restyles every character, tags included, so only
        // a real change (zoom, or another reading font) does it.
        var repaint = coordinator.members != mentions
        var refresh = repaint
        coordinator.members = mentions
        if coordinator.font != textStyle.editorFont {
            coordinator.font = textStyle.editorFont
            view.font = textStyle.editorFont
            repaint = true
        }
        // Literally, not Swift `==`: an equivalent but differently encoded
        // draft would leave the painted chips out of step with the sent ones.
        if !ChatMentions.same(view.string, text) {
            coordinator.applying = true; view.string = text; coordinator.applying = false
            repaint = true; refresh = true
        }
        if repaint { coordinator.paint(view) }
        // The list publishes state, which must wait until this update ends.
        if refresh {
            DispatchQueue.main.async { [weak view, weak coordinator] in if let view { coordinator?.refreshMenu(view) } }
        }
        measure(view)
    }

    /// Publish the height the text needs, clamped to a few lines; the SwiftUI
    /// frame follows and the scroll view takes over past the ceiling.
    func measure(_ view: NSTextView) {
        guard let container = view.textContainer, let layout = view.layoutManager else { return }
        layout.ensureLayout(for: container)
        let used = layout.usedRect(for: container).height + view.textContainerInset.height * 2
        // The ceiling grows with zoom (to twice its height) so large print
        // still shows a few lines before the composer scrolls.
        let next = min(max(used.rounded(.up), Self.minHeight), Self.maxHeight * min(CGFloat(textStyle.zoom), 2))
        guard abs(next - height) > 0.5 else { return }
        DispatchQueue.main.async { height = next }
    }
}
