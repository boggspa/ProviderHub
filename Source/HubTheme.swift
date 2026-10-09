import AppKit
import SwiftUI

/// A snapshot of existing UI values, with filled-button label ink applied,
/// plus the appearance preference and the semantic colours that follow it.
enum HubTheme {
    /// The user's appearance choice. "system" follows macOS; the others pin
    /// the app. Persisted in UserDefaults beside the design choice and applied
    /// through NSApp.appearance, which every window and dynamic colour obeys.
    enum Appearance {
        enum Mode: String, CaseIterable {
            case system, light, dark

            var title: String {
                switch self {
                case .system: return "System"
                case .light: return "Light"
                case .dark: return "Dark"
                }
            }

            var name: NSAppearance.Name? {
                switch self {
                case .system: return nil
                case .light: return .aqua
                case .dark: return .darkAqua
                }
            }
        }

        static let defaultsKey = "hubAppearance"

        /// The default follows macOS: the compact shell and the classic pages
        /// both resolve their colours per appearance, so a user who never
        /// chooses gets the hub in whatever their Mac is wearing.
        static var mode: Mode {
            get { Mode(rawValue: UserDefaults.standard.string(forKey: defaultsKey) ?? "") ?? .system }
            set { UserDefaults.standard.set(newValue.rawValue, forKey: defaultsKey) }
        }

        /// The appearance the app pins today, kept for callers that read it.
        static var name: NSAppearance.Name { mode.name ?? .darkAqua }

        /// Apply the stored choice to the app. Call at launch and after a change.
        @MainActor static func apply() {
            NSApp.appearance = mode.name.map { NSAppearance(named: $0) } ?? nil
        }

        /// Whether the effective appearance is dark, for code that cannot use
        /// a dynamic colour (for example a view that paints its own material).
        @MainActor static var isDark: Bool {
            let effective = NSApp.effectiveAppearance
            return effective.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        }
    }

    /// The main window's surface, independent of its Light/Dark/System choice.
    /// Glass preserves the existing shell; Solid uses an opaque macOS neutral.
    enum WindowStyle {
        enum Mode: String, CaseIterable {
            case glass, solid

            var title: String { self == .glass ? "Glass" : "Solid" }
            var icon: String { self == .glass ? "square.on.square" : "square.fill" }
        }

        static let defaultsKey = "hubWindowStyle"
        static var mode: Mode {
            Mode(rawValue: UserDefaults.standard.string(forKey: defaultsKey) ?? "") ?? .glass
        }
    }

    /// Colours that resolve per appearance. Each is a dynamic NSColor, so a
    /// SwiftUI view that uses it re-renders when the appearance changes
    /// without any state of its own. The dark values are today's constants;
    /// the light values are their counterparts on a light window.
    enum Semantic {
        /// The window or page background.
        static let surface = dynamic(light: NSColor(red: 0.965, green: 0.965, blue: 0.97, alpha: 1),
                                     dark: NSColor(red: 0.095, green: 0.098, blue: 0.102, alpha: 1))
        /// A card, row or panel lifted off the surface.
        static let raisedSurface = dynamic(light: NSColor.black.withAlphaComponent(0.045),
                                           dark: NSColor.white.withAlphaComponent(0.045))
        /// A selected navigation row or an active control background.
        static let selection = dynamic(light: NSColor.black.withAlphaComponent(0.09),
                                       dark: NSColor.white.withAlphaComponent(0.09))
        /// The one-point rule between panes and rows.
        static let hairline = dynamic(light: NSColor.black.withAlphaComponent(0.09),
                                      dark: NSColor.white.withAlphaComponent(0.07))
        /// Primary text.
        static let ink = dynamic(light: NSColor(white: 0.11, alpha: 1), dark: NSColor(white: 0.93, alpha: 1))
        /// Secondary text and glyphs.
        static let secondaryInk = dynamic(light: NSColor(white: 0.11, alpha: 0.6), dark: NSColor(white: 0.93, alpha: 0.6))
        /// The brand accent as it should sit on the surface of each appearance.
        static let accentOnSurface = dynamic(light: NSColor(red: 0.87, green: 0.34, blue: 0.1, alpha: 1),
                                             dark: NSColor(red: 1.0, green: 0.43, blue: 0.18, alpha: 1))
        /// Label ink for text drawn on `accentOnSurface`.
        static let inkOnAccent = Color.white
        /// Approval mode as state colour. Manual asks first (blue, the tint
        /// TaskWraith gives plan/read-only); Accept Edits is deliberately
        /// neutral; YOLO runs tools unprompted (red, its full-access tint).
        /// Light values are the AA-on-white allocations, dark their brighter
        /// counterparts on the dark surface.
        static let approvalManual = dynamic(light: NSColor(red: 0.0, green: 0.45, blue: 0.9, alpha: 1),
                                            dark: NSColor(red: 0.44, green: 0.71, blue: 1.0, alpha: 1))
        static let approvalAcceptEdits = secondaryInk
        static let approvalYolo = dynamic(light: NSColor(red: 0.86, green: 0.15, blue: 0.15, alpha: 1),
                                          dark: NSColor(red: 0.93, green: 0.35, blue: 0.35, alpha: 1))
        /// Context pressure, on TaskWraith's wheel thresholds: amber from 80%
        /// of the window, red from 95%. Below that the provider accent shows.
        static let contextWarn = dynamic(light: NSColor(red: 0.80, green: 0.50, blue: 0.0, alpha: 1),
                                         dark: NSColor(red: 0.94, green: 0.66, blue: 0.25, alpha: 1))
        static let contextCritical = approvalYolo

        static func dynamic(light: NSColor, dark: NSColor) -> Color {
            Color(nsColor: NSColor(name: nil) { appearance in
                appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
            })
        }
    }

    enum Surface {
        // MistralBridge.swift:1171.
        static let window = Color(red: 0.095, green: 0.098, blue: 0.102)
        // MistralBridge.swift:1148; preserve this tint until glass is approved.
        static let sidebarTint = Color.black.opacity(0.15)
        // MistralBridge.swift:1108; also used by catalogue rows and footer bars.
        static let raised = Color.white.opacity(0.045)
        // MistralBridge.swift:1139; applies only to the selected navigation row.
        static let navigationSelection = Color.white.opacity(0.09)
    }

    enum Accent {
        // MistralBridge.swift:5; the mark and prominent controls share one orange.
        static let brand = Color(red: 1.0, green: 0.43, blue: 0.18)
    }

    enum Material {
        /// Reserved by the design brief, not extracted from an existing view.
        /// Future use: one NSVisualEffectView behind the 200pt sidebar only.
        /// Declaring its material neither creates a view nor enables vibrancy.
        static let sidebar: NSVisualEffectView.Material = .sidebar
    }

    enum Separator {
        // MistralBridge.swift:1109,1149; do not replace native Divider() with it.
        static let subtle = Color.white.opacity(0.07)
        // MistralBridge.swift:1109,1149; one point, not a device-pixel hairline.
        static let width: CGFloat = 1
    }

    enum Radius {
        // HubLayout.swift:223; CodexHarness.swift:404; navigation also uses 8.
        static let row: CGFloat = 8
        // ProviderViews.swift:51-52.
        static let providerTile: CGFloat = 10
        // MistralBridge.swift:1236.
        static let metric: CGFloat = 12
        // MistralBridge.swift:1108-1109.
        static let panel: CGFloat = 14
    }

    /// The recurring subset of today's spacing scale. Other literals such as
    /// 5, 7, 10 and 11 remain distinct; never round them to a nearby token.
    enum Spacing {
        // HubLayout.swift:77; DevinAgentsView.swift:83,101.
        static let xs: CGFloat = 4
        // HubLayout.swift:131,208,210; CodexHarness.swift:295.
        static let sm: CGFloat = 8
        // HubLayout.swift:41,183; ProviderViews.swift:50,58.
        static let md: CGFloat = 12
        // MistralBridge.swift:1106; HubLayout.swift:21,51.
        static let lg: CGFloat = 18
        // MistralBridge.swift:1107,1152.
        static let xl: CGFloat = 22
        // MistralBridge.swift:1160.
        static let xxl: CGFloat = 30
    }

    enum Typography {
        // MistralBridge.swift:1196.
        static let pageTitle = Font.system(size: 27, weight: .semibold)
        // HubLayout.swift:78; ProviderViews.swift:61.
        static let paneTitle = Font.system(size: 18, weight: .semibold)
        // MistralBridge.swift:1197; not the 13pt medium preference label.
        static let body = Font.system(size: 13)
        // HubLayout.swift:212; ProviderViews.swift:150.
        // CodexHarness.swift:394 currently uses semibold and is not equivalent.
        static let rowTitle = Font.system(size: 12, weight: .medium)
        // HubLayout.swift:42,185; CodexHarness.swift:395.
        // Native .caption and monospaced 11pt fonts remain separate choices.
        static let detail = Font.system(size: 11)
    }

    enum Layout {
        // MistralBridge.swift:1148; includes the existing sidebar padding.
        static let sidebarWidth: CGFloat = 200
    }

    enum Control {
        // MistralBridge.swift:1187; HubLayout.swift:280; CodexHarness.swift:503.
        // The standard macOS style; control size remains a per-view decision.
        static let prominentButton = BorderedProminentButtonStyle()
        /// Label color for a filled prominent button on `Accent.brand`.
        static let ink = Color.white
    }
}
