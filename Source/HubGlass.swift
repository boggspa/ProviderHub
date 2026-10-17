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

    /// The pane treatment for the window's Glass/Solid choice. In Glass mode
    /// the side panes take one step off the glass and the transcript goes
    /// solid; in Solid mode the opaque window background already separates
    /// them, so the modifier paints nothing and the pane keeps its own look.
    func hubGlassPane(_ pane: HubGlassPane) -> some View {
        modifier(HubGlassPaneModifier(pane: pane))
    }
}

/// Which Glass-mode fill a pane takes. The sidebar and inspector share one;
/// the transcript is the only pane that becomes opaque.
enum HubGlassPane {
    case sidebar
    case inspector
    case transcript
}

/// The surface style the panes resolve against, injected once per window from
/// the same `@AppStorage` key the settings page writes, so a change redraws
/// every pane without each of them reading UserDefaults.
private struct HubSurfaceStyleKey: EnvironmentKey {
    static let defaultValue = HubTheme.WindowStyle.Mode.glass
}

extension EnvironmentValues {
    var hubSurfaceStyle: HubTheme.WindowStyle.Mode {
        get { self[HubSurfaceStyleKey.self] }
        set { self[HubSurfaceStyleKey.self] = newValue }
    }
}

private struct HubGlassPaneModifier: ViewModifier {
    let pane: HubGlassPane
    @Environment(\.hubSurfaceStyle) private var style

    func body(content: Content) -> some View {
        if style == .glass { content.background(fill) } else { content }
    }

    private var fill: Color {
        pane == .transcript ? HubTheme.Glass.transcript : HubTheme.Glass.pane
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
