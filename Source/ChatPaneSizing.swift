import Foundation

/// Stored preferences survive temporary window constraints; displayed widths
/// are recalculated on every resize so the transcript retains usable space.
enum ChatPaneSizing {
    static let divider: CGFloat = 8
    static let transcriptMinimum: CGFloat = 420
    static let railDefault: CGFloat = 190
    static func clamp(_ value: CGFloat, _ lower: CGFloat, _ upper: CGFloat) -> CGFloat {
        min(max(value.isFinite ? value : lower, lower), max(lower, upper))
    }
    static func inspectorDefault(_ window: CGFloat) -> CGFloat {
        min(360, max(260, window * 0.35))
    }
    struct Layout {
        let rail: CGFloat
        let inspector: CGFloat
        let railAvailable: Bool
        let transcript: CGFloat
    }
    static func layout(window: CGFloat, railPreferred: Bool, inspectorVisible: Bool,
                       rail: CGFloat, inspector: CGFloat) -> Layout {
        let inspectorWidth = inspectorVisible
            ? clamp(inspector > 0 ? inspector : inspectorDefault(window), 260,
                    min(600, window - transcriptMinimum - divider)) : 0
        let remaining = window - inspectorWidth - (inspectorVisible ? divider : 0)
        let available = remaining >= 860
        let railWidth = railPreferred && available
            ? clamp(rail, 150, min(360, remaining - transcriptMinimum - divider)) : 0
        return Layout(rail: railWidth, inspector: inspectorWidth, railAvailable: available,
                      transcript: remaining - railWidth - (railWidth > 0 ? divider : 0))
    }
}
