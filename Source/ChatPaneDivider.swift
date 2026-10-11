import AppKit
import SwiftUI

/// A visible hairline with an eight-point pointer and accessibility target.
struct ChatPaneDivider: NSViewRepresentable {
    let label: String
    let width: CGFloat
    let direction: CGFloat
    let resize: (CGFloat) -> Void
    let reset: () -> Void
    func makeNSView(context: Context) -> DividerView { DividerView() }
    func updateNSView(_ view: DividerView, context: Context) {
        view.width = width; view.direction = direction; view.resize = resize; view.reset = reset
        view.setAccessibilityLabel(label)
        view.setAccessibilityHelp("Adjust to resize. Double-click to reset.")
    }
    func sizeThatFits(_ proposal: ProposedViewSize, nsView: DividerView, context: Context) -> CGSize? {
        CGSize(width: ChatPaneSizing.divider, height: proposal.height ?? 0)
    }
    final class DividerView: NSView {
        var width: CGFloat = 190
        var direction: CGFloat = 1
        var resize: (CGFloat) -> Void = { _ in }
        var reset: () -> Void = {}
        private var origin: (width: CGFloat, x: CGFloat)?
        override var intrinsicContentSize: NSSize { NSSize(width: ChatPaneSizing.divider, height: NSView.noIntrinsicMetric) }
        override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
        override func resetCursorRects() { addCursorRect(bounds, cursor: .resizeLeftRight) }
        override func draw(_ dirtyRect: NSRect) {
            NSColor.separatorColor.setFill()
            NSRect(x: bounds.midX - 0.5, y: bounds.minY, width: 1, height: bounds.height).fill()
        }
        override func mouseDown(with event: NSEvent) {
            if event.clickCount >= 2 { origin = nil; reset(); return }
            origin = (width, event.locationInWindow.x)
        }
        override func mouseDragged(with event: NSEvent) {
            guard let origin else { return }
            resize(origin.width + direction * (event.locationInWindow.x - origin.x))
        }
        override func mouseUp(with event: NSEvent) { origin = nil }
        override func viewDidMoveToWindow() { super.viewDidMoveToWindow(); origin = nil }
        override func isAccessibilityElement() -> Bool { true }
        override func accessibilityRole() -> NSAccessibility.Role? { .splitter }
        override func accessibilityValue() -> Any? { "\(Int(width)) points" }
        override func accessibilityPerformIncrement() -> Bool { resize(width + 10); return true }
        override func accessibilityPerformDecrement() -> Bool { resize(width - 10); return true }
        override func accessibilityCustomActions() -> [NSAccessibilityCustomAction]? {
            [NSAccessibilityCustomAction(name: "Reset width") { [weak self] in self?.reset(); return self != nil }]
        }
    }
}
