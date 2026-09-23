import AppKit
import SwiftUI

/// Phase 1: an unused snapshot of existing UI values, not an applied theme.
/// Keep this file outside build.sh's explicit source list until the later,
/// owner-approved substitution phase. It performs no appearance setup.
enum HubTheme {
    enum Appearance {
        // MistralBridge.swift:1249; the app remains dark-only.
        static let name: NSAppearance.Name = .darkAqua
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
        // MistralBridge.swift:5; decorative mark, not a filled-control tint.
        static let brand = Color(red: 1.0, green: 0.43, blue: 0.18)
        // MistralBridge.swift:9,1171; the concurrent control-contrast change.
        // #BF5419 decoded exactly as HubModels.swift:154-156 decodes it.
        // Do not merge this role with brand or with per-provider accents.
        static let control = Color(red: 191.0 / 255.0, green: 84.0 / 255.0, blue: 25.0 / 255.0)
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
    }
}
