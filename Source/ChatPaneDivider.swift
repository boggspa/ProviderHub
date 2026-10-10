import AppKit
import SwiftUI

/// A visible hairline with an eight-point pointer and accessibility target.
struct ChatPaneDivider: View {
    let label: String
    let width: CGFloat
    let direction: CGFloat
    let resize: (CGFloat) -> Void
    let reset: () -> Void
    @State private var dragOrigin: CGFloat?
    @State private var cursorPushed = false

    var body: some View {
        Rectangle().fill(Color.clear)
            .frame(width: ChatPaneSizing.divider)
            .overlay { Rectangle().fill(HubTheme.Semantic.hairline).frame(width: 1) }
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 0, coordinateSpace: .global)
                .onChanged { value in
                    if dragOrigin == nil { dragOrigin = width }
                    resize((dragOrigin ?? width) + direction * value.translation.width)
                }
                .onEnded { _ in dragOrigin = nil })
            .onTapGesture(count: 2, perform: reset)
            .onHover { hovering in
                if hovering && !cursorPushed { NSCursor.resizeLeftRight.push(); cursorPushed = true }
                else if !hovering { releaseCursor() }
            }
            .onDisappear { releaseCursor(); dragOrigin = nil }
            .accessibilityElement()
            .accessibilityLabel(label)
            .accessibilityValue("\(Int(width)) points")
            .accessibilityHint("Adjust to resize. Double-click to reset.")
            .accessibilityAdjustableAction { adjustment in
                switch adjustment {
                case .increment: resize(width + 10)
                case .decrement: resize(width - 10)
                @unknown default: break
                }
            }
            .accessibilityAction(named: Text("Reset width"), reset)
    }

    private func releaseCursor() {
        if cursorPushed { NSCursor.pop(); cursorPushed = false }
    }
}
