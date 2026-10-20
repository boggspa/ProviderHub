import AppKit
import SwiftUI

/// The user's own colours for Chat's reading surfaces: the transcript
/// background, its text, the composer and the user's message bubble.
///
/// A choice is a hue and an intensity, never a fixed RGB value. Each surface
/// sets its own brightness per appearance, close to its default's, and takes
/// only the hue from the user, so one choice reads as a deep tint on Dark and
/// a pale wash on Light, and the text stays light on Dark and dark on Light.
/// This file depends on nothing but AppKit and SwiftUI, so the transcript and
/// composer test targets that stub HubTheme can compile it as it is.
struct ChatHue: Equatable, Hashable {
    /// Whole degrees round the colour wheel, 0 up to 360.
    var hue: Double
    /// How far from neutral grey, 0 to 1. Each surface scales it to its own
    /// saturation ceiling, so 1 is vivid text but still a quiet background.
    var intensity: Double

    init(hue: Double, intensity: Double) {
        let wrapped = hue.isFinite ? hue.truncatingRemainder(dividingBy: 360) : 0
        let degrees = (wrapped < 0 ? wrapped + 360 : wrapped).rounded()
        self.hue = degrees >= 360 ? 0 : degrees
        self.intensity = ((intensity.isFinite ? min(max(intensity, 0), 1) : 0) * 100).rounded() / 100
    }

    /// Parse the stored "hue,intensity" form. Anything else, including the
    /// empty string the reset writes, is Default.
    init?(stored: String) {
        let parts = stored.split(separator: ",", omittingEmptySubsequences: false)
        guard parts.count == 2,
              let hue = Double(parts[0].trimmingCharacters(in: .whitespaces)), hue.isFinite,
              let intensity = Double(parts[1].trimmingCharacters(in: .whitespaces)), intensity.isFinite else { return nil }
        self.init(hue: hue, intensity: intensity)
    }

    /// Locale-independent, so a choice survives a change of region format.
    var stored: String {
        String(Int(hue)) + "," + String(format: "%.2f", locale: Locale(identifier: "en_US_POSIX"), intensity)
    }

    /// The choice as one recognisable colour, for a swatch or a slider thumb;
    /// no surface paints it.
    var vivid: NSColor { Self.color(hue: hue, saturation: 0.25 + intensity * 0.6, brightness: 0.88) }

    /// HSB to sRGB, done by hand so every caller gets identical components
    /// whatever colour space AppKit would otherwise pick.
    static func color(hue: Double, saturation: Double, brightness: Double, alpha: Double = 1) -> NSColor {
        let s = min(max(saturation, 0), 1), v = min(max(brightness, 0), 1)
        let sector = (hue.truncatingRemainder(dividingBy: 360) + 360).truncatingRemainder(dividingBy: 360) / 60
        let index = Int(sector.rounded(.down)) % 6, fraction = sector - sector.rounded(.down)
        let p = v * (1 - s), q = v * (1 - s * fraction), t = v * (1 - s * (1 - fraction))
        let (r, g, b): (Double, Double, Double)
        switch index {
        case 0: (r, g, b) = (v, t, p)
        case 1: (r, g, b) = (q, v, p)
        case 2: (r, g, b) = (p, v, t)
        case 3: (r, g, b) = (p, q, v)
        case 4: (r, g, b) = (t, p, v)
        default: (r, g, b) = (v, p, q)
        }
        return NSColor(srgbRed: r, green: g, blue: b, alpha: alpha)
    }

    /// Starting points for the picker's swatches. Each keeps today's text
    /// at 4.5:1 on every surface in both appearances (test_chat_hues).
    static let presets: [(name: String, hue: ChatHue)] = [
        ("Slate", ChatHue(hue: 215, intensity: 0.3)), ("Ocean", ChatHue(hue: 205, intensity: 0.7)),
        ("Teal", ChatHue(hue: 175, intensity: 0.6)), ("Sage", ChatHue(hue: 135, intensity: 0.45)),
        ("Sand", ChatHue(hue: 38, intensity: 0.55)), ("Coral", ChatHue(hue: 12, intensity: 0.6)),
        ("Rose", ChatHue(hue: 340, intensity: 0.55)), ("Violet", ChatHue(hue: 265, intensity: 0.45))
    ]
}

/// The four surfaces a hue can be chosen for.
enum ChatHueSurface: String, CaseIterable, Identifiable {
    case transcript, text, composer, bubble

    var id: String { rawValue }

    var title: String {
        switch self {
        case .transcript: "Transcript background"
        case .text: "Transcript text"
        case .composer: "Composer fill"
        case .bubble: "Your message bubble"
        }
    }

    var shortTitle: String {
        switch self {
        case .transcript: "Background"
        case .text: "Text"
        case .composer: "Composer"
        case .bubble: "Bubble"
        }
    }

    /// Beside the theme's own keys (hubAppearance, hubWindowStyle). No dots:
    /// @AppStorage observes UserDefaults by key path.
    var defaultsKey: String {
        switch self {
        case .transcript: "chatHueTranscript"
        case .text: "chatHueText"
        case .composer: "chatHueComposer"
        case .bubble: "chatHueBubble"
        }
    }

    /// The HSB brightness the surface sits at in each appearance, near its
    /// default's, and the saturation a full intensity reaches there. Light
    /// surfaces take less saturation than dark ones: a pale wash shows a hue
    /// at a fraction of the chroma a deep tint needs.
    func tone(dark: Bool) -> (brightness: Double, saturation: Double) {
        switch self {
        case .transcript: dark ? (0.11, 0.60) : (0.965, 0.12)
        case .text: dark ? (0.94, 0.85) : (0.26, 0.85)
        case .composer: dark ? (0.19, 0.50) : (1.0, 0.08)
        case .bubble: dark ? (0.28, 0.65) : (0.90, 0.30)
        }
    }

    /// The surface's colour for a choice in one appearance, opaque sRGB.
    func color(_ hue: ChatHue, dark: Bool) -> NSColor {
        let tone = tone(dark: dark)
        return ChatHue.color(hue: hue.hue, saturation: tone.saturation * hue.intensity, brightness: tone.brightness)
    }

    /// Light and Dark in one NSColor. The instance is cached per choice:
    /// transcript text views compare colours by identity and would otherwise
    /// rebuild every message on each redraw.
    func dynamicColor(_ hue: ChatHue) -> NSColor {
        let key = rawValue + "|" + hue.stored
        if let cached = Self.cache[key] { return cached }
        if Self.cache.count > 256 { Self.cache.removeAll() }
        let light = color(hue, dark: false), dark = color(hue, dark: true)
        let made = NSColor(name: nil) { $0.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light }
        Self.cache[key] = made
        return made
    }
    private static var cache: [String: NSColor] = [:]
}

/// Every surface's choice; nil is Default, today's colour.
struct ChatHues: Equatable {
    var transcript: ChatHue?
    var text: ChatHue?
    var composer: ChatHue?
    var bubble: ChatHue?

    init(transcript: ChatHue? = nil, text: ChatHue? = nil, composer: ChatHue? = nil, bubble: ChatHue? = nil) {
        self.transcript = transcript; self.text = text; self.composer = composer; self.bubble = bubble
    }

    init(stored: (ChatHueSurface) -> String?) {
        self.init(transcript: stored(.transcript).flatMap(ChatHue.init(stored:)),
                  text: stored(.text).flatMap(ChatHue.init(stored:)),
                  composer: stored(.composer).flatMap(ChatHue.init(stored:)),
                  bubble: stored(.bubble).flatMap(ChatHue.init(stored:)))
    }

    init(defaults: UserDefaults) { self.init { defaults.string(forKey: $0.defaultsKey) } }

    subscript(surface: ChatHueSurface) -> ChatHue? {
        get {
            switch surface {
            case .transcript: transcript
            case .text: text
            case .composer: composer
            case .bubble: bubble
            }
        }
        set {
            switch surface {
            case .transcript: transcript = newValue
            case .text: text = newValue
            case .composer: composer = newValue
            case .bubble: bubble = newValue
            }
        }
    }

    var isDefault: Bool { ChatHueSurface.allCases.allSatisfy { self[$0] == nil } }

    static func reset(_ defaults: UserDefaults = .standard) {
        for surface in ChatHueSurface.allCases { defaults.removeObject(forKey: surface.defaultsKey) }
    }

    // What each surface paints; nil keeps the surface's own default.

    /// Prose ink for the selectable transcript text views.
    var textColor: NSColor? { text.map(ChatHueSurface.text.dynamicColor) }
    var textInk: Color? { textColor.map { Color(nsColor: $0) } }
    var bubbleFill: Color? { bubble.map { Color(nsColor: ChatHueSurface.bubble.dynamicColor($0)) } }
    var composerFill: Color? { composer.map { Color(nsColor: ChatHueSurface.composer.dynamicColor($0)) } }
    /// Opaque in both window styles: in Glass it replaces the transcript's
    /// paper, and the sidebar and chrome around it stay glass.
    var transcriptFill: Color? { transcript.map { Color(nsColor: ChatHueSurface.transcript.dynamicColor($0)) } }
}

// MARK: - Contrast

/// Today's colours in one appearance, resolved to sRGB by the caller, which
/// knows the theme. `transcript` is the opaque surface the transcript shows
/// by default for the current window style (Glass's paper or the Solid
/// window); `bubble` may be translucent over the transcript.
struct ChatHueBase {
    var transcript: NSColor
    var text: NSColor
    var bubble: NSColor
    var composer: NSColor
}

/// A pair of colours a reader must tell apart, below the AA ratio.
struct ChatContrastIssue: Equatable, Identifiable {
    var pair: String
    var dark: Bool
    var ratio: Double
    var id: String { pair + (dark ? "|dark" : "|light") }
    var message: String {
        pair + " is " + String(format: "%.1f", locale: Locale(identifier: "en_US_POSIX"), ratio) + ":1 in "
            + (dark ? "Dark" : "Light") + ". Aim for 4.5:1."
    }
}

enum ChatContrast {
    /// WCAG AA for body text.
    static let minimum = 4.5

    static func luminance(_ color: NSColor) -> Double {
        guard let rgb = color.usingColorSpace(.sRGB) else { return 0 }
        func linear(_ c: CGFloat) -> Double {
            let c = Double(c)
            return c <= 0.04045 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4)
        }
        return 0.2126 * linear(rgb.redComponent) + 0.7152 * linear(rgb.greenComponent) + 0.0722 * linear(rgb.blueComponent)
    }

    static func ratio(_ a: NSColor, _ b: NSColor) -> Double {
        let x = luminance(a), y = luminance(b)
        return (max(x, y) + 0.05) / (min(x, y) + 0.05)
    }

    /// `top`, at its own alpha, painted over an opaque `bottom`.
    static func composite(_ top: NSColor, over bottom: NSColor) -> NSColor {
        guard let t = top.usingColorSpace(.sRGB), let b = bottom.usingColorSpace(.sRGB) else { return bottom }
        let a = Double(t.alphaComponent)
        func mix(_ x: CGFloat, _ y: CGFloat) -> Double { Double(x) * a + Double(y) * (1 - a) }
        return NSColor(srgbRed: mix(t.redComponent, b.redComponent), green: mix(t.greenComponent, b.greenComponent),
                       blue: mix(t.blueComponent, b.blueComponent), alpha: 1)
    }
}

extension ChatHues {
    /// The opaque colours one appearance shows, for the contrast check and
    /// the picker's preview.
    struct Resolved {
        var transcript: NSColor
        var text: NSColor
        var bubble: NSColor
        var composer: NSColor
        /// The composer's own text, which keeps the default ink.
        var composerText: NSColor
    }

    func resolved(dark: Bool, base: ChatHueBase) -> Resolved {
        let background = transcript.map { ChatHueSurface.transcript.color($0, dark: dark) } ?? base.transcript
        return Resolved(transcript: background,
                        text: text.map { ChatHueSurface.text.color($0, dark: dark) } ?? base.text,
                        bubble: bubble.map { ChatHueSurface.bubble.color($0, dark: dark) }
                            ?? ChatContrast.composite(base.bubble, over: background),
                        composer: composer.map { ChatHueSurface.composer.color($0, dark: dark) } ?? base.composer,
                        composerText: base.text)
    }

    /// Low-contrast pairs in either appearance. A choice applies to both, so
    /// both are checked whichever is showing. Only pairs a custom colour
    /// touches are reported; the choice is never adjusted, only flagged.
    func contrastIssues(base: (_ dark: Bool) -> ChatHueBase) -> [ChatContrastIssue] {
        guard !isDefault else { return [] }
        var issues: [ChatContrastIssue] = []
        for dark in [false, true] {
            let colours = resolved(dark: dark, base: base(dark))
            if text != nil || transcript != nil {
                let ratio = ChatContrast.ratio(colours.text, colours.transcript)
                if ratio < ChatContrast.minimum { issues.append(ChatContrastIssue(pair: "Text on the background", dark: dark, ratio: ratio)) }
            }
            if text != nil || bubble != nil || transcript != nil {
                let ratio = ChatContrast.ratio(colours.text, colours.bubble)
                if ratio < ChatContrast.minimum { issues.append(ChatContrastIssue(pair: "Text in your bubble", dark: dark, ratio: ratio)) }
            }
        }
        return issues
    }
}

// MARK: - Environment

private struct ChatHuesKey: EnvironmentKey { static let defaultValue = ChatHues() }

extension EnvironmentValues {
    /// Injected once per window from UserDefaults; split panes' hosting
    /// controllers inherit it, so a change repaints every pane at once.
    var chatHues: ChatHues {
        get { self[ChatHuesKey.self] }
        set { self[ChatHuesKey.self] = newValue }
    }
}

/// Reads the four choices and hands them down the view tree.
struct ChatHueEnvironment: ViewModifier {
    @AppStorage(ChatHueSurface.transcript.defaultsKey) private var transcript = ""
    @AppStorage(ChatHueSurface.text.defaultsKey) private var text = ""
    @AppStorage(ChatHueSurface.composer.defaultsKey) private var composer = ""
    @AppStorage(ChatHueSurface.bubble.defaultsKey) private var bubble = ""

    func body(content: Content) -> some View {
        content.environment(\.chatHues, ChatHues(transcript: ChatHue(stored: transcript), text: ChatHue(stored: text),
                                                 composer: ChatHue(stored: composer), bubble: ChatHue(stored: bubble)))
    }
}

extension View {
    func chatHues() -> some View { modifier(ChatHueEnvironment()) }
}
