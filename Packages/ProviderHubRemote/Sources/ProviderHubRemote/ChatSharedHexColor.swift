import SwiftUI
#if canImport(UIKit)
import UIKit
#else
import AppKit
#endif

// Canonical source shared with macOS through Source/ChatSharedHexColor.swift.

/// A `#RRGGBB` colour code in a draft or a message, found the way Codex
/// Desktop finds one: six hex digits at the start of the text or after
/// whitespace, not running on into a letter, digit or underscore, so URL
/// fragments, `a#b` and `#ff0000ff` are left alone. A code straight after
/// "issue" or "PR", or an all-digit code on a line that mentions a PR, is a
/// reference rather than a colour. The three- and eight-digit forms are left
/// out, as Codex leaves them: `#123`, `#bad` and `#fed` are issues and tags far
/// more often than colours.
struct ChatHexColor: Equatable {
    /// The code's UTF-16 range, `#` included.
    var range: NSRange
    /// 0xRRGGBB.
    var rgb: UInt32

    var cgColor: CGColor {
        CGColor(srgbRed: CGFloat(rgb >> 16 & 0xFF) / 255, green: CGFloat(rgb >> 8 & 0xFF) / 255,
                blue: CGFloat(rgb & 0xFF) / 255, alpha: 1)
    }

    private static let code = try! NSRegularExpression(pattern: #"(?<![^\s])#[0-9A-Fa-f]{6}(?![A-Za-z0-9_])"#)
    private static let pullRequest = try! NSRegularExpression(pattern: #"(?<![A-Za-z0-9_])[Pp][Rr][Ss]?(?![A-Za-z0-9_])"#)
    /// Transcripts ask again on every redraw, and most texts hold no `#`.
    private static let plainFinds = cache(), fencedFinds = cache()

    /// The codes in plain text. A Markdown draft skips its fenced blocks,
    /// which the transcript shows as code.
    static func find(in text: String, skippingFences: Bool = false) -> [ChatHexColor] {
        guard text.utf8.contains(UInt8(ascii: "#")) else { return [] }
        let finds = skippingFences ? fencedFinds : plainFinds
        let key = text as NSString
        if let known = finds.object(forKey: key) { return known.colors }
        let fences = skippingFences ? fenced(key) : []
        var colors: [ChatHexColor] = []
        for match in code.matches(in: text, range: NSRange(location: 0, length: key.length)) {
            let range = match.range
            guard !fences.contains(where: { NSLocationInRange(range.location, $0) }), !followsReference(key, at: range.location),
                  let rgb = UInt32(key.substring(with: NSRange(location: range.location + 1, length: 6)), radix: 16) else { continue }
            let digits = key.substring(with: range).dropFirst().allSatisfy(\.isNumber)
            if digits, pullRequest.firstMatch(in: text, range: key.lineRange(for: range)) != nil { continue }
            colors.append(ChatHexColor(range: range, rgb: rgb))
        }
        finds.setObject(Found(colors), forKey: key)
        return colors
    }

    /// The codes in rendered Markdown. A link's text is left alone, and an
    /// inline code span shows a swatch only when the code is all it holds.
    static func find(in text: AttributedString) -> [ChatHexColor] {
        find(in: String(text.characters)).filter { color in
            guard let span = Range(color.range, in: text) else { return false }
            let runs = text[span].runs
            if runs.contains(where: { $0.link != nil }) { return false }
            let code = runs.map { $0.inlinePresentationIntent?.contains(.code) == true }
            guard code.contains(true) else { return true }
            if code.contains(false) { return false }
            let before = span.lowerBound > text.startIndex ? text.characters.index(before: span.lowerBound) : nil
            let after = span.upperBound < text.endIndex ? text.characters.index(after: span.upperBound) : nil
            return !(before.map { isCode(text, $0..<span.lowerBound) } ?? false)
                && !(after.map { isCode(text, span.upperBound..<$0) } ?? false)
        }
    }

    private static func isCode(_ text: AttributedString, _ range: Range<AttributedString.Index>) -> Bool {
        text[range].runs.contains { $0.inlinePresentationIntent?.contains(.code) == true }
    }

    /// The word before the code, across spaces and punctuation on its line.
    private static func followsReference(_ string: NSString, at location: Int) -> Bool {
        func isWord(_ unit: unichar) -> Bool {
            (0x30...0x39).contains(unit) || (0x41...0x5A).contains(unit) || (0x61...0x7A).contains(unit) || unit == 0x5F
        }
        var end = location
        while end > 0 {
            let unit = string.character(at: end - 1)
            if unit == 0x0A || unit == 0x0D || isWord(unit) { break }
            end -= 1
        }
        var start = end
        while start > 0, isWord(string.character(at: start - 1)) { start -= 1 }
        guard end - start == 2 || end - start == 5 else { return false }
        let word = string.substring(with: NSRange(location: start, length: end - start)).lowercased()
        return word == "pr" || word == "issue"
    }

    private static func fenced(_ string: NSString) -> [NSRange] {
        guard string.range(of: "```").location != NSNotFound || string.range(of: "~~~").location != NSNotFound else { return [] }
        var ranges: [NSRange] = [], open: (mark: unichar, run: Int, start: Int)?
        string.enumerateSubstrings(in: NSRange(location: 0, length: string.length), options: [.byLines, .substringNotRequired]) { _, line, enclosing, _ in
            guard let fence = fence(string, line) else { return }
            if let current = open {
                if fence.mark == current.mark, fence.run >= current.run, fence.info.isEmpty {
                    ranges.append(NSRange(location: current.start, length: NSMaxRange(enclosing) - current.start)); open = nil
                }
            } else if fence.mark == 0x7E || !fence.info.contains("`") {
                open = (fence.mark, fence.run, enclosing.location)
            }
        }
        if let open { ranges.append(NSRange(location: open.start, length: string.length - open.start)) }
        return ranges
    }

    /// A fence line: up to three spaces, then three or more backticks or tildes.
    private static func fence(_ string: NSString, _ line: NSRange) -> (mark: unichar, run: Int, info: String)? {
        var index = line.location
        let end = NSMaxRange(line)
        while index < end, index - line.location < 4, string.character(at: index) == 0x20 { index += 1 }
        guard index - line.location <= 3, index < end else { return nil }
        let mark = string.character(at: index)
        guard mark == 0x60 || mark == 0x7E else { return nil }
        var run = index
        while run < end, string.character(at: run) == mark { run += 1 }
        guard run - index >= 3 else { return nil }
        return (mark, run - index, string.substring(with: NSRange(location: run, length: end - run)).trimmingCharacters(in: .whitespaces))
    }

    private final class Found: NSObject {
        let colors: [ChatHexColor]
        init(_ colors: [ChatHexColor]) { self.colors = colors }
    }

    private static func cache() -> NSCache<NSString, Found> {
        let cache = NSCache<NSString, Found>()
        cache.countLimit = 512
        return cache
    }
}

/// The swatch drawn after a code: Codex's 12-point rounded square beside
/// 14-point text, scaled with the text and held to whole points so a kerned
/// gap and a drawn image are exactly the same width.
enum ChatHexSwatch {
    static func side(_ size: CGFloat) -> CGFloat { max(6, (size * 0.82).rounded()) }
    static func lead(_ size: CGFloat) -> CGFloat { max(2, (size * 0.28).rounded()) }
    static func trail(_ size: CGFloat) -> CGFloat { max(1, (size * 0.14).rounded()) }
    /// The room after a code's last character: a gap, the square, a gap.
    static func advance(_ size: CGFloat) -> CGFloat { lead(size) + side(size) + trail(size) }
    /// How far the square's foot sits below the baseline.
    static func drop(_ size: CGFloat) -> CGFloat { (size * 0.08).rounded() }

    /// The square, with a rim of ink rather than a hairline, so a colour
    /// matching the background (`#121212` on the dark transcript, say) still
    /// shows its edge in either appearance.
    static func draw(_ color: ChatHexColor, in square: CGRect, dark: Bool, context: CGContext) {
        let radius = square.width * 0.25
        context.saveGState()
        context.addPath(CGPath(roundedRect: square, cornerWidth: radius, cornerHeight: radius, transform: nil))
        context.setFillColor(color.cgColor)
        context.fillPath()
        let rim = square.insetBy(dx: 0.5, dy: 0.5)
        context.addPath(CGPath(roundedRect: rim, cornerWidth: radius - 0.5, cornerHeight: radius - 0.5, transform: nil))
        context.setStrokeColor(CGColor(gray: dark ? 1 : 0, alpha: dark ? 0.36 : 0.3))
        context.setLineWidth(1)
        context.strokePath()
        context.restoreGState()
    }

    #if canImport(UIKit)
    typealias PlatformImage = UIImage
    #else
    typealias PlatformImage = NSImage
    #endif
    private static let images: NSCache<NSString, PlatformImage> = {
        let cache = NSCache<NSString, PlatformImage>()
        cache.countLimit = 256
        return cache
    }()

    /// The swatch and its gaps as one image, `advance` wide and `side` tall.
    static func image(_ color: ChatHexColor, size: CGFloat, dark: Bool) -> PlatformImage {
        let key = "\(color.rgb):\(size):\(dark)" as NSString
        if let image = images.object(forKey: key) { return image }
        let canvas = CGSize(width: advance(size), height: side(size))
        let square = CGRect(x: lead(size), y: 0, width: side(size), height: side(size))
        #if canImport(UIKit)
        let image = UIGraphicsImageRenderer(size: canvas).image { draw(color, in: square, dark: dark, context: $0.cgContext) }
        #else
        let image = NSImage(size: canvas, flipped: true) { _ in
            guard let context = NSGraphicsContext.current?.cgContext else { return false }
            draw(color, in: square, dark: dark, context: context)
            return true
        }
        #endif
        images.setObject(image, forKey: key)
        return image
    }
}

/// Text SwiftUI lays out, with a swatch after each colour code. Nothing is
/// added to the text: each code's last character is kerned to leave the
/// swatch's room, and a copy of the text with every glyph clear and the
/// swatches inline is laid over it. Selection, copy and VoiceOver read only
/// the original characters.
struct ChatSharedHexText: View {
    var text: AttributedString
    /// The point size the text is set in, which sizes the swatches.
    var size: CGFloat
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        let colors = ChatHexColor.find(in: text)
        if colors.isEmpty {
            Text(text).textSelection(.enabled)
        } else {
            // Kerning adds to the font's pair kerning, which an image between
            // a code and its comma interrupts, so the copy can run a fraction
            // of a point wider per code. A little more width than the text's
            // keeps it from wrapping a line the text kept, and it is never
            // cut to the text's height. It moves down to the swatches' foot
            // without laying out again.
            Text(Self.kerned(text, colors, size: size)).textSelection(.enabled)
                .overlay(alignment: .topLeading) {
                    Self.swatched(text, colors, size: size, dark: scheme == .dark)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.trailing, -size * 0.05 * CGFloat(min(colors.count, 8)))
                        .offset(y: ChatHexSwatch.drop(size))
                        .allowsHitTesting(false).accessibilityHidden(true)
                }
        }
    }

    static func kerned(_ text: AttributedString, _ colors: [ChatHexColor], size: CGFloat) -> AttributedString {
        var kerned = text
        for color in colors {
            guard let span = Range(color.range, in: kerned) else { continue }
            let last = kerned.characters.index(before: span.upperBound)
            kerned[last..<span.upperBound][AttributeScopes.SwiftUIAttributes.KerningAttribute.self] = advance(size)
        }
        return kerned
    }

    /// The same runs, fonts kept so every line breaks where the text's does,
    /// with nothing visible but the swatches standing in for the kerned room.
    /// An image sits on the baseline; lowering it inside the text would make
    /// each line taller than the text's.
    static func swatched(_ text: AttributedString, _ colors: [ChatHexColor], size: CGFloat, dark: Bool) -> Text {
        var ghost = text
        ghost.foregroundColor = Color.clear
        ghost.backgroundColor = Color.clear
        ghost.link = nil
        var result = Text(verbatim: ""), cursor = ghost.startIndex
        for color in colors {
            guard let span = Range(color.range, in: ghost), span.lowerBound >= cursor else { continue }
            #if canImport(UIKit)
            let swatch = Image(uiImage: ChatHexSwatch.image(color, size: size, dark: dark))
            #else
            let swatch = Image(nsImage: ChatHexSwatch.image(color, size: size, dark: dark))
            #endif
            // In the code's own font: an image in the surrounding font could
            // set a taller line than the text has.
            var image = Text(swatch.renderingMode(.original))
            if let font = ghost[span].runs.last?[AttributeScopes.SwiftUIAttributes.FontAttribute.self] { image = image.font(font) }
            result = result + Text(AttributedString(ghost[cursor..<span.upperBound])) + image
            cursor = span.upperBound
        }
        return result + Text(AttributedString(ghost[cursor..<ghost.endIndex]))
    }

    private static func advance(_ size: CGFloat) -> CGFloat { ChatHexSwatch.advance(size) }
}
