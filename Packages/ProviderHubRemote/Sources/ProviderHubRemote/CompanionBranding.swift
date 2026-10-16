import SwiftUI
#if os(iOS)
import UIKit
#else
import AppKit
#endif

struct CompanionPresentation {
    var provider: String
    var label: String
    var shortCode: String
    var accentHex: String
    var logo: RemoteValue
    var accent: Color { CompanionTheme.accent(accentHex) }

    init(_ value: RemoteValue, fallback: String = "Assistant") {
        provider = value["displayProvider"].text
        label = value["modelLabel"].text.isEmpty ? fallback : value["modelLabel"].text
        shortCode = value["shortCode"].text.isEmpty ? String(fallback.prefix(3)).uppercased() : value["shortCode"].text
        accentHex = value["accent"].text
        logo = value["logo"]
    }
}

enum CompanionBranding {
    #if SWIFT_PACKAGE
    static let bundle = Bundle.module
    #else
    static let bundle = Bundle.main
    #endif
    static let catalogue: RemoteValue = {
        guard let url = bundle.url(forResource: "provider_branding", withExtension: "json"),
              let data = try? Data(contentsOf: url), let value = try? JSONDecoder().decode(RemoteValue.self, from: data) else { return .null }
        return value
    }()

    static func presentation(route: String, label: String = "Assistant", models: [RemoteValue] = []) -> CompanionPresentation {
        if let row = models.first(where: { $0["route"].text == route }), row["presentation"] != .null {
            return CompanionPresentation(row["presentation"], fallback: row["label"].text)
        }
        let provider = String(route.split(separator: "/").first ?? "")
        let model = route.split(separator: "/", maxSplits: 1).dropFirst().first.map(String.init) ?? route
        var key = provider
        for rule in catalogue["modelBrandOverrides"].rows where rule["runtimeProvider"].text == provider {
            if rule["needles"].rows.contains(where: { model.lowercased().contains($0.text.lowercased()) }) {
                key = rule["providerClass"].text; break
            }
        }
        var value = catalogue["providers"][key].object
        let hue = value["hueKey"]?.text ?? key
        let canonical = catalogue["accentAliases"][hue].text
        value["accent"] = catalogue["accents"][canonical.isEmpty ? hue : canonical]
        value["modelLabel"] = .string(label)
        return CompanionPresentation(.object(value), fallback: label)
    }
}

/// Uses the desktop's exact PNGs, variant selection, crop and template rules.
struct CompanionProviderMark: View {
    @Environment(\.colorScheme) private var colorScheme
    let presentation: CompanionPresentation
    var size: CGFloat = 15
    var body: some View {
        Group {
            if let image = artwork {
                let ratio = presentation.logo["leadingMarkAspectRatio"].number ?? presentation.logo["trailingMarkAspectRatio"].number
                let scale = presentation.logo["scale"].number ?? 1
                Group {
                    if let ratio {
                        let height = size / max(ratio, 1)
                        image.image.resizable().renderingMode(presentation.logo["template"].flag ? .template : .original)
                            .frame(width: height * image.ratio, height: height)
                            .frame(width: height * ratio, height: height, alignment: presentation.logo["leadingMarkAspectRatio"] != .null ? .leading : .trailing)
                            .clipped()
                    } else {
                        image.image.resizable().renderingMode(presentation.logo["template"].flag ? .template : .original).scaledToFit()
                    }
                }
                .foregroundStyle(presentation.logo["tint"].text == "accent" ? presentation.accent : CompanionTheme.ink)
                .scaleEffect(scale)
            } else {
                Text(presentation.shortCode).font(.system(size: size * 0.4, weight: .bold, design: .rounded)).foregroundStyle(presentation.accent)
            }
        }.frame(width: size, height: size).accessibilityLabel(presentation.provider)
    }
    private var artwork: (image: Image, ratio: CGFloat)? {
        let logo = presentation.logo
        let file = colorScheme == .dark && !logo["dark"].text.isEmpty ? logo["dark"].text : logo["light"].text
        guard file.hasPrefix("provider-logos/provider-logo-"), !file.contains(".."), file.hasSuffix(".png"),
              let url = CompanionBranding.bundle.resourceURL?.appendingPathComponent(file) else { return nil }
        #if os(iOS)
        guard let image = UIImage(contentsOfFile: url.path) else { return nil }
        return (Image(uiImage: image), image.size.width / max(1, image.size.height))
        #else
        guard let image = NSImage(contentsOf: url) else { return nil }
        return (Image(nsImage: image), image.size.width / max(1, image.size.height))
        #endif
    }
}

extension RemoteValue {
    var number: Double? { if case .number(let value) = self { return value }; return nil }
}
