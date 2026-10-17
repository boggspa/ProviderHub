import SwiftUI
#if os(iOS)
import UIKit
#else
import AppKit
#endif

/// Matches HubTheme.Semantic/Radius/Spacing on macOS. Text uses native Dynamic
/// Type on the phone, while the surfaces and provider colour remain familiar.
enum CompanionTheme {
    static let surface = adaptive((0.965, 0.965, 0.97, 1), (0.095, 0.098, 0.102, 1))
    static let raised = adaptive((0, 0, 0, 0.045), (1, 1, 1, 0.045))
    static let cardFill = adaptive((0, 0, 0, 0.065), (0, 0, 0, 0.12))
    static let brand = adaptive((0.87, 0.34, 0.1, 1), (1, 0.43, 0.18, 1))
    static let cardRim = brand.opacity(0.20)
    static let selection = adaptive((0, 0, 0, 0.09), (1, 1, 1, 0.09))
    static let hairline = adaptive((0, 0, 0, 0.09), (1, 1, 1, 0.07))
    static let ink = adaptive((0.11, 0.11, 0.11, 1), (0.93, 0.93, 0.93, 1))
    static let secondary = adaptive((0.11, 0.11, 0.11, 0.6), (0.93, 0.93, 0.93, 0.6))
    static let manual = adaptive((0, 0.45, 0.9, 1), (0.44, 0.71, 1, 1))
    static let rowRadius: CGFloat = 8
    static let panelRadius: CGFloat = 14
    static let gap: CGFloat = 12
    static let margin: CGFloat = 18

    /// Which fill a pane takes. The sidebar and inspector share one; the
    /// transcript is the only pane that becomes opaque.
    enum Pane {
        case sidebar
        case inspector
        case transcript
    }

    /// Glass-mode fills, mirroring HubTheme.Glass on the Mac. The side panes
    /// take one step off the material — a lighter gray on dark, a deeper one
    /// on light — and the transcript stops being glass and becomes paper:
    /// solid #121212 on dark, its light counterpart #F4F4F6 on light.
    static let glassPane = adaptive((0, 0, 0, 0.06), (1, 1, 1, 0.07))
    static let glassTranscript = adaptive((244 / 255, 244 / 255, 246 / 255, 1), (18 / 255, 18 / 255, 18 / 255, 1))

    /// The fill a pane takes under the current surface choice, read from the
    /// same `companionSurface` key the settings sheet writes. Solid keeps the
    /// flat surface the phone has always had.
    static func pane(_ pane: Pane, surfaceStyle: String) -> Color {
        guard surfaceStyle == "glass" else { return surface }
        return pane == .transcript ? glassTranscript : glassPane
    }

    /// The approval mode's name and colour, as the Mac composer footer shows them.
    static func approvalLabel(_ mode: String) -> String {
        switch mode { case "accept_edits": return "Accept Edits"; case "yolo": return "YOLO"; default: return "Manual" }
    }
    static func approvalColor(_ mode: String) -> Color {
        switch mode { case "accept_edits": return secondary; case "yolo": return .red; default: return manual }
    }

    static func accent(_ hex: String?) -> Color {
        guard let hex, hex.count == 7, hex.hasPrefix("#"), let value = UInt32(hex.dropFirst(), radix: 16) else { return secondary }
        return Color(.sRGB, red: Double(value >> 16 & 255) / 255, green: Double(value >> 8 & 255) / 255,
                     blue: Double(value & 255) / 255, opacity: 1)
    }
    private static func adaptive(_ light: (Double, Double, Double, Double), _ dark: (Double, Double, Double, Double)) -> Color {
        #if os(iOS)
        Color(uiColor: UIColor { traits in
            let c = traits.userInterfaceStyle == .dark ? dark : light
            return UIColor(red: c.0, green: c.1, blue: c.2, alpha: c.3)
        })
        #else
        Color(nsColor: NSColor(name: nil) { appearance in
            let c = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
            return NSColor(red: c.0, green: c.1, blue: c.2, alpha: c.3)
        })
        #endif
    }
}

extension View {
    func companionCard(cornerRadius: CGFloat, accent: Color? = nil) -> some View {
        background(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous).fill(CompanionTheme.cardFill))
            .overlay(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                .strokeBorder(accent.map { $0.opacity(0.22) } ?? CompanionTheme.cardRim, lineWidth: 0.5))
    }
}

extension RemoteChat {
    var displayModel: String { modelLabel.flatMap { $0.isEmpty ? nil : $0 } ?? "Assistant" }
    var displayProvider: String { providerLabel.flatMap { $0.isEmpty ? nil : $0 } ?? displayModel }
    var providerAccent: Color { CompanionTheme.accent(accent) }
}
