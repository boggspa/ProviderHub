import AppKit
import SwiftUI

/// Theme ▸ Custom Colours…: a hue for each of Chat's reading surfaces, with
/// the result previewed in Light and Dark side by side. Choices write
/// straight to UserDefaults, which the Chat window observes, so every pane
/// repaints as a slider moves. A low-contrast pair is flagged, never fixed.
struct ChatHueSettings: View {
    private typealias Semantic = HubTheme.Semantic
    @Environment(\.dismiss) private var dismiss
    @AppStorage(ChatHueSurface.transcript.defaultsKey) private var transcript = ""
    @AppStorage(ChatHueSurface.text.defaultsKey) private var text = ""
    @AppStorage(ChatHueSurface.composer.defaultsKey) private var composer = ""
    @AppStorage(ChatHueSurface.bubble.defaultsKey) private var bubble = ""
    @AppStorage(HubTheme.WindowStyle.defaultsKey) private var windowStyle = HubTheme.WindowStyle.Mode.glass
    @State private var surface = ChatHueSurface.transcript
    /// Where the sliders sit for a surface still on Default; moving one makes it custom.
    private static let start = ChatHue(hue: 215, intensity: 0.45)

    private var hues: ChatHues { ChatHues { stored($0) } }
    private var glass: Bool { windowStyle == .glass }

    var body: some View {
        let light = hues.resolved(dark: false, base: Self.base(dark: false, glass: glass, onThisMac: true))
        let dark = hues.resolved(dark: true, base: Self.base(dark: true, glass: glass, onThisMac: true))
        VStack(alignment: .leading, spacing: 14) {
            header
            HStack(spacing: 10) {
                ChatHuePreview(colours: light, dark: false)
                ChatHuePreview(colours: dark, dark: true)
            }
            surfaces(light: light, dark: dark)
            editor(light: light, dark: dark)
            contrast
        }
        .padding(20).frame(width: 560)
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Custom Colours").font(.headline)
                Text("Pick a hue for each surface; its brightness follows Light and Dark.")
                    .font(.system(size: 11)).foregroundStyle(Semantic.secondaryInk)
            }
            Spacer()
            Button("Reset All") { ChatHues.reset() }.disabled(hues.isDefault)
            Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
        }
    }

    /// One card per surface, its swatch split into the Light and Dark colours it will show.
    private func surfaces(light: ChatHues.Resolved, dark: ChatHues.Resolved) -> some View {
        HStack(spacing: 8) {
            ForEach(ChatHueSurface.allCases) { item in
                let selected = item == surface
                let accent = Semantic.accentOnSurface
                Button { surface = item } label: {
                    HStack(spacing: 7) {
                        ChatHueSwatch(light: Self.colour(item, light), dark: Self.colour(item, dark), size: 18)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(item.shortTitle).font(.system(size: 11.5, weight: .semibold)).foregroundStyle(Semantic.ink)
                            Text(hues[item] == nil ? "Default" : "Custom").font(.system(size: 10))
                                .foregroundStyle(hues[item] == nil ? Semantic.secondaryInk : accent)
                        }
                        Spacer(minLength: 0)
                    }
                    .padding(.horizontal, 8).padding(.vertical, 6).frame(maxWidth: .infinity)
                    .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(selected ? accent.opacity(0.10) : .clear))
                    .hubCard(cornerRadius: 9, accent: selected ? accent : nil)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(item.title + ", " + (hues[item] == nil ? "Default" : "Custom"))
                .accessibilityAddTraits(selected ? .isSelected : [])
            }
        }
    }

    private func editor(light: ChatHues.Resolved, dark: ChatHues.Resolved) -> some View {
        let choice = hues[surface]
        let shown = choice ?? Self.start
        let base = (light: Self.base(dark: false, glass: glass, onThisMac: true), dark: Self.base(dark: true, glass: glass, onThisMac: true))
        let fallback = (light: ChatHues().resolved(dark: false, base: base.light),
                        dark: ChatHues().resolved(dark: true, base: base.dark))
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(surface.title).font(.system(size: 13, weight: .semibold)).foregroundStyle(Semantic.ink)
                Text(choice == nil ? "Default" : "\(Int(shown.hue))° · \(Int((shown.intensity * 100).rounded()))%")
                    .font(.system(size: 11)).foregroundStyle(Semantic.secondaryInk)
                Spacer()
                Button("Reset") { store("", surface) }.controlSize(.small).disabled(choice == nil)
            }
            HStack(spacing: 6) {
                Button { store("", surface) } label: {
                    ChatHueSwatch(light: Self.colour(surface, fallback.light), dark: Self.colour(surface, fallback.dark),
                                  size: 22, selected: choice == nil)
                }.buttonStyle(.plain).help("Default").accessibilityLabel("Default")
                Rectangle().fill(Semantic.hairline).frame(width: 1, height: 18).padding(.horizontal, 3)
                ForEach(ChatHue.presets, id: \.name) { preset in
                    let swatch = Color(nsColor: preset.hue.vivid)
                    Button { store(preset.hue.stored, surface) } label: {
                        ChatHueSwatch(light: swatch, dark: swatch, size: 22, selected: choice == preset.hue)
                    }.buttonStyle(.plain).help(preset.name).accessibilityLabel(preset.name)
                }
                Spacer(minLength: 0)
            }
            // On Default the sliders wait, dimmed, at a starting hue; moving one makes the surface custom.
            ChatHueTrack(title: "Hue", value: shown.hue / 359,
                         colours: stride(from: 0.0, through: 360, by: 30).map { Color(nsColor: ChatHue(hue: $0, intensity: 0.75).vivid) },
                         thumb: Color(nsColor: ChatHue(hue: shown.hue, intensity: 0.75).vivid), valueText: "\(Int(shown.hue))°") {
                store(ChatHue(hue: $0 * 359, intensity: shown.intensity).stored, surface)
            }
            .opacity(choice == nil ? 0.55 : 1)
            ChatHueTrack(title: "Intensity", value: shown.intensity,
                         colours: [Color(nsColor: ChatHue(hue: shown.hue, intensity: 0).vivid), Color(nsColor: ChatHue(hue: shown.hue, intensity: 1).vivid)],
                         thumb: Color(nsColor: shown.vivid), valueText: "\(Int((shown.intensity * 100).rounded()))%") {
                store(ChatHue(hue: shown.hue, intensity: $0).stored, surface)
            }
            .opacity(choice == nil ? 0.55 : 1)
        }
        .padding(12)
        .hubCard(cornerRadius: HubTheme.Radius.row)
    }

    @ViewBuilder private var contrast: some View {
        let issues = hues.contrastIssues { Self.base(dark: $0, glass: glass) }
        if !issues.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(issues) { issue in
                    Label(issue.message, systemImage: "exclamationmark.triangle.fill")
                        .font(.system(size: 11)).foregroundStyle(Semantic.contextWarn)
                }
                Text("Your colours are kept as chosen. A lower intensity or another hue raises the contrast.")
                    .font(.system(size: 10.5)).foregroundStyle(Semantic.secondaryInk)
            }
            .accessibilityElement(children: .combine)
        } else if !hues.isDefault {
            Label("Text contrast holds in Light and Dark.", systemImage: "checkmark.circle")
                .font(.system(size: 11)).foregroundStyle(Semantic.secondaryInk)
        }
    }

    private func stored(_ surface: ChatHueSurface) -> String {
        switch surface {
        case .transcript: transcript
        case .text: text
        case .composer: composer
        case .bubble: bubble
        }
    }

    /// Writes only a change, so a drag that stays on one degree does not
    /// repaint every transcript in the window.
    private func store(_ value: String, _ surface: ChatHueSurface) {
        guard stored(surface) != value else { return }
        switch surface {
        case .transcript: transcript = value
        case .text: text = value
        case .composer: composer = value
        case .bubble: bubble = value
        }
    }

    private static func colour(_ surface: ChatHueSurface, _ colours: ChatHues.Resolved) -> Color {
        switch surface {
        case .transcript: Color(nsColor: colours.transcript)
        case .text: Color(nsColor: colours.text)
        case .composer: Color(nsColor: colours.composer)
        case .bubble: Color(nsColor: colours.bubble)
        }
    }

    /// Solid shows the system's windowBackgroundColor behind the transcript,
    /// and macOS has moved it: #323232 / #ECECEC on macOS 15, #1E1E1E /
    /// #FFFFFF on macOS 26. Contrast is measured against fixed values rather
    /// than whatever this Mac resolves, taking the lighter Dark and the darker
    /// Light of the two, so a pair that passes here passes on either release.
    static let solidWindow = (light: NSColor(srgbRed: 236 / 255, green: 236 / 255, blue: 236 / 255, alpha: 1),
                              dark: NSColor(srgbRed: 50 / 255, green: 50 / 255, blue: 50 / 255, alpha: 1))

    /// Today's colours in one appearance, from the app's own fixed tokens:
    /// Glass's paper or the Solid window reference behind the transcript. A
    /// chosen background replaces either, so it is measured as is. The
    /// previews pass `onThisMac` to show the Solid window this Mac paints;
    /// the contrast check never does, so its verdict is the same everywhere.
    static func base(dark: Bool, glass: Bool, onThisMac: Bool = false) -> ChatHueBase {
        var base = ChatHueBase(transcript: .black, text: .white, bubble: .clear, composer: .black)
        NSAppearance(named: dark ? .darkAqua : .aqua)?.performAsCurrentDrawingAppearance {
            func srgb(_ color: NSColor) -> NSColor { color.usingColorSpace(.sRGB) ?? color }
            let solid = onThisMac ? srgb(.windowBackgroundColor) : dark ? solidWindow.dark : solidWindow.light
            base = ChatHueBase(transcript: glass ? srgb(NSColor(HubTheme.Glass.transcript)) : solid,
                               text: srgb(Semantic.nsInk),
                               bubble: srgb(NSColor(Semantic.selection)),
                               composer: srgb(NSColor(Semantic.composerFill)))
        }
        return base
    }
}

/// A few lines of transcript in one appearance: your bubble, a reply and the
/// composer, in the colours the window will paint.
private struct ChatHuePreview: View {
    var colours: ChatHues.Resolved
    var dark: Bool

    var body: some View {
        let text = Color(nsColor: colours.text)
        let ink = Color(nsColor: colours.composerText)
        // The brand accent as each appearance shows it (HubTheme.Semantic.accentOnSurface).
        let accent = dark ? Color(red: 1.0, green: 0.43, blue: 0.18) : Color(red: 0.87, green: 0.34, blue: 0.1)
        VStack(alignment: .leading, spacing: 9) {
            Text(dark ? "DARK" : "LIGHT").font(.system(size: 9.5, weight: .semibold)).tracking(0.6)
                .foregroundStyle(ink.opacity(0.45))
            HStack {
                Spacer(minLength: 30)
                (Text("User: ").bold() + Text("Can you tighten the intro?"))
                    .font(.system(size: 11.5)).foregroundStyle(text)
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .background(RoundedRectangle(cornerRadius: 11, style: .continuous).fill(Color(nsColor: colours.bubble)))
            }
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 4) {
                    Circle().fill(accent).frame(width: 6, height: 6)
                    Text("Reply").font(.system(size: 10, weight: .semibold)).foregroundStyle(accent)
                }
                Text("Done. It now opens with what the hub does, then a two-line quick start.")
                    .font(.system(size: 11.5)).foregroundStyle(text).fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 6) {
                Text("Message…").font(.system(size: 11.5)).foregroundStyle(ink.opacity(0.4))
                Spacer(minLength: 0)
                Image(systemName: "arrow.up").font(.system(size: 8.5, weight: .bold)).foregroundStyle(Color(nsColor: colours.composer))
                    .frame(width: 18, height: 18).background(Circle().fill(ink))
            }
            .padding(.leading, 10).padding(.trailing, 5).padding(.vertical, 5)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color(nsColor: colours.composer)))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(ink.opacity(0.13), lineWidth: 0.5))
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color(nsColor: colours.transcript)))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(HubTheme.Semantic.hairline, lineWidth: 1))
        .accessibilityElement(children: .combine)
        .accessibilityLabel((dark ? "Dark" : "Light") + " preview")
    }
}

/// A colour as a circle, split on the diagonal when Light and Dark differ.
private struct ChatHueSwatch: View {
    var light: Color
    var dark: Color
    var size: CGFloat
    var selected = false

    var body: some View {
        Circle().fill(light)
            .overlay(ChatHueSwatchHalf().fill(dark))
            .clipShape(Circle())
            .overlay(Circle().strokeBorder(Color.primary.opacity(0.18), lineWidth: 0.5))
            .frame(width: size, height: size)
            .padding(2.5)
            .overlay(Circle().strokeBorder(selected ? HubTheme.Semantic.accentOnSurface : .clear, lineWidth: 1.5))
    }
}

/// The lower-right half of a square, cut on the diagonal.
private struct ChatHueSwatchHalf: Shape {
    func path(in rect: CGRect) -> Path {
        Path { path in
            path.move(to: CGPoint(x: rect.maxX, y: rect.minY))
            path.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY))
            path.addLine(to: CGPoint(x: rect.minX, y: rect.maxY))
            path.closeSubpath()
        }
    }
}

/// A slider drawn as the range it sets: a capsule of colour with a thumb in
/// the chosen colour. VoiceOver steps it in 2% increments.
private struct ChatHueTrack: View {
    var title: String
    /// 0 to 1 along the track.
    var value: Double
    var colours: [Color]
    var thumb: Color
    var valueText: String
    var set: (Double) -> Void

    var body: some View {
        HStack(spacing: 10) {
            Text(title).font(.system(size: 11)).foregroundStyle(HubTheme.Semantic.secondaryInk)
                .frame(width: 58, alignment: .leading)
            GeometryReader { proxy in
                let thumbSize: CGFloat = 16
                let span = max(proxy.size.width - thumbSize, 1)
                ZStack(alignment: .leading) {
                    Capsule().fill(LinearGradient(colors: colours, startPoint: .leading, endPoint: .trailing))
                        .frame(height: 8)
                        .overlay(Capsule().strokeBorder(HubTheme.Semantic.hairline, lineWidth: 0.5))
                        .padding(.horizontal, thumbSize / 2 - 2)
                    Circle().fill(thumb)
                        .overlay(Circle().strokeBorder(Color.white, lineWidth: 2))
                        .shadow(color: .black.opacity(0.35), radius: 1.5, y: 0.5)
                        .frame(width: thumbSize, height: thumbSize)
                        .offset(x: CGFloat(min(max(value, 0), 1)) * span)
                }
                .frame(maxHeight: .infinity)
                .contentShape(Rectangle())
                .gesture(DragGesture(minimumDistance: 0).onChanged { drag in
                    set(Double(min(max((drag.location.x - thumbSize / 2) / span, 0), 1)))
                })
            }
            .frame(height: 20)
            Text(valueText).font(.system(size: 10.5, design: .monospaced)).foregroundStyle(HubTheme.Semantic.secondaryInk)
                .frame(width: 36, alignment: .trailing)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(title)
        .accessibilityValue(valueText)
        .accessibilityAdjustableAction { direction in
            switch direction {
            case .increment: set(min(value + 0.02, 1))
            case .decrement: set(max(value - 0.02, 0))
            @unknown default: break
            }
        }
    }
}
