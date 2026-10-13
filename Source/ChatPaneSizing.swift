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
    static let inspectorMinimum: CGFloat = 260
    /// The rail stays available while the transcript side keeps this much.
    static let railThreshold: CGFloat = 860
    /// The widest the inspector may be: everything but the transcript's minimum.
    static func inspectorMaximum(_ window: CGFloat) -> CGFloat {
        window - transcriptMinimum - divider
    }
    /// Equal priority with the transcript: with no drag override the inspector
    /// takes half of what is left beside the rail, giving ground only so the
    /// rail stays available and the transcript keeps its minimum. The browser
    /// tab in particular was starved by the old fixed 360 cap. The rail's
    /// width is reserved even while the rail is hidden, so hiding it with the
    /// inspector open never grows the inspector past the point where the rail
    /// could be shown again.
    static func inspectorDefault(_ window: CGFloat, rail: CGFloat = 0) -> CGFloat {
        let beside = window - (rail > 0 ? rail + divider : 0) - divider
        var even = (beside / 2).rounded(.down)
        if rail > 0 { even = min(even, window - railThreshold - divider) }
        return clamp(even, inspectorMinimum, inspectorMaximum(window))
    }
    struct Layout {
        let rail: CGFloat
        let inspector: CGFloat
        let railAvailable: Bool
        let transcript: CGFloat
    }
    static func layout(window: CGFloat, railPreferred: Bool, inspectorVisible: Bool,
                       rail: CGFloat, inspector: CGFloat) -> Layout {
        let railWanted = clamp(rail, 150, 360)
        let inspectorWidth = inspectorVisible
            ? clamp(inspector > 0 ? inspector : inspectorDefault(window, rail: railWanted), inspectorMinimum,
                    inspectorMaximum(window)) : 0
        let remaining = window - inspectorWidth - (inspectorVisible ? divider : 0)
        let available = remaining >= railThreshold
        let railWidth = railPreferred && available
            ? clamp(rail, 150, min(360, remaining - transcriptMinimum - divider)) : 0
        return Layout(rail: railWidth, inspector: inspectorWidth, railAvailable: available,
                      transcript: remaining - railWidth - (railWidth > 0 ? divider : 0))
    }
}
