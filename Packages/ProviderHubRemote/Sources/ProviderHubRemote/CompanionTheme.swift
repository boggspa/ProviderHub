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
    var approvalLabel: String {
        switch approvalMode { case "accept_edits": return "Accept Edits"; case "yolo": return "YOLO"; default: return "Manual" }
    }
    var approvalColor: Color {
        switch approvalMode { case "accept_edits": return CompanionTheme.secondary; case "yolo": return .red; default: return CompanionTheme.manual }
    }
}
