import AppKit
import SwiftUI

extension View {
    /// Tonal container treatment; callers keep their existing geometry.
    func hubCard(cornerRadius: CGFloat, accent: Color? = nil) -> some View {
        background(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
            .fill(HubTheme.Semantic.cardFill))
        .overlay(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
            .strokeBorder(accent.map { $0.opacity(0.22) } ?? HubTheme.Semantic.cardRim, lineWidth: 0.5))
    }
}

/// Window vibrancy behind the shell: the desktop shows through the glass.
/// The material follows the effective appearance (HubTheme.Appearance): HUD
/// glass on dark, popover glass on light, so the window re-tints on a change
/// without any SwiftUI state.
struct VibrancyBackground: NSViewRepresentable {
    final class AppearanceGlass: NSVisualEffectView {
        override func viewDidChangeEffectiveAppearance() {
            super.viewDidChangeEffectiveAppearance()
            material = effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? .hudWindow : .popover
        }
    }
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = AppearanceGlass()
        view.blendingMode = .behindWindow; view.state = .active
        view.viewDidChangeEffectiveAppearance()
        return view
    }
    func updateNSView(_ view: NSVisualEffectView, context: Context) {}
}
