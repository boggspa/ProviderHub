import SwiftUI

/// Tool glyphs from TaskWraith's
/// design-assets/tool-call-icons/tool-call-icons.catalog.svg. These are the
/// original 24×24 monoline paths, cached as native paths once. The curved
/// glyphs are copied as their path data and read by a parser that knows only
/// the M/L/H/V/C/Z commands those strings use; there is no image loading or
/// whole-catalogue runtime dependency. Callers tint them with the speaker's
/// provider accent so a Team member's calls are recognisable at a glance.
struct ChatToolGlyph: View {
    var name: String
    var size: CGFloat = 17
    var body: some View {
        GeometryReader { geometry in
            let scale = geometry.size.width / 24
            (Self.paths[name] ?? Self.paths["read_file"]!)
                .applying(CGAffineTransform(scaleX: scale, y: scale))
                .stroke(style: StrokeStyle(lineWidth: 1.65 * scale, lineCap: .round, lineJoin: .round))
        }.frame(width: size, height: size).accessibilityHidden(true)
    }

    static func has(_ name: String) -> Bool { paths[name] != nil }

    private static func lines(_ strokes: [[CGFloat]], closed: Set<Int> = []) -> Path {
        var path = Path()
        for (index, points) in strokes.enumerated() {
            path.move(to: CGPoint(x: points[0], y: points[1]))
            for i in stride(from: 2, to: points.count, by: 2) {
                path.addLine(to: CGPoint(x: points[i], y: points[i + 1]))
            }
            if closed.contains(index) { path.closeSubpath() }
        }
        return path
    }

    /// Absolute and relative M, L, H, V and C commands plus Z, as written in
    /// the catalogue; anything else is a transcription error and fails loudly
    /// in the native glyph test rather than drawing a wrong shape.
    static func svg(_ data: [String], circles: [(CGFloat, CGFloat, CGFloat)] = []) -> Path {
        var path = Path()
        for string in data {
            var tokens: [String] = []
            var number = ""
            for character in string {
                if character.isLetter {
                    if !number.isEmpty { tokens.append(number); number = "" }
                    tokens.append(String(character))
                } else if character == " " || character == "," {
                    if !number.isEmpty { tokens.append(number); number = "" }
                } else if character == "-", !number.isEmpty, number.last != "e" {
                    tokens.append(number); number = "-"
                } else { number.append(character) }
            }
            if !number.isEmpty { tokens.append(number) }
            var index = 0, command = "M", current = CGPoint.zero, start = CGPoint.zero
            func next() -> CGFloat { defer { index += 1 }; return CGFloat(Double(tokens[index]) ?? .nan) }
            while index < tokens.count {
                if let first = tokens[index].first, first.isLetter { command = tokens[index]; index += 1 }
                let relative = command == command.lowercased()
                func point() -> CGPoint {
                    let x = next(), y = next()
                    return relative ? CGPoint(x: current.x + x, y: current.y + y) : CGPoint(x: x, y: y)
                }
                switch command.uppercased() {
                case "M":
                    current = point(); start = current; path.move(to: current)
                    command = relative ? "l" : "L"
                case "L": current = point(); path.addLine(to: current)
                case "H": let x = next(); current.x = relative ? current.x + x : x; path.addLine(to: current)
                case "V": let y = next(); current.y = relative ? current.y + y : y; path.addLine(to: current)
                case "C":
                    let first = point(), second = point(), end = point()
                    path.addCurve(to: end, control1: first, control2: second); current = end
                // Z takes no numbers; any that follow it are malformed.
                case "Z": path.closeSubpath(); current = start; command = "?"
                default: return Path()
                }
            }
        }
        for (x, y, radius) in circles {
            path.addEllipse(in: CGRect(x: x - radius, y: y - radius, width: radius * 2, height: radius * 2))
        }
        return path
    }

    private static let paths: [String: Path] = {
        let file = lines([[7.2,3.3,15.7,3,20.2,7.4,20,20.8,6.7,20.5,6.3,4.2],
                          [15.4,3.2,15.8,7.7,20,7.4], [4.2,6.1,4.5,21.4,16.6,21.1],
                          [9.1,10.1,17.1,9.8], [9.2,13.3,16.4,13.2], [9.1,16.5,14.2,16.7]], closed: [0])
        let shell = lines([[3.7,5.1,20.4,4.8,20.9,18.7,3.2,19.1], [4.2,8.2,20.3,8],
                           [7.1,11.1,10.4,13.5,7.2,15.9], [12.8,16.2,17.4,16.1],
                           [6.2,6.6,6.2,6.7], [8.4,6.5,8.5,6.6]], closed: [0])
        var search = lines([[4.2,5.2,14.6,4.9], [4.1,8.4,11.4,8.2], [4.4,11.8,9.6,11.6],
                            [15.8,15.9,20,20.3], [10.7,12.1,13.8,11.9]])
        search.addEllipse(in: CGRect(x: 7.6, y: 7.7, width: 9.4, height: 9.4))
        let patch = lines([[5.2,4.1,15.8,3.9,19.2,7.3,19,20.2,5.1,20], [15.6,4.1,15.8,7.5,19,7.3],
                           [8,9.4,13.4,9.3], [8,16.2,15.9,16], [8.2,12.8,14.9,12.7],
                           [11.6,9.7,11.6,15.8], [18.1,12.1,20.8,14.4,18.1,16.8]], closed: [0])
        let memory = svg(["M5.4 4.4 18.4 4.1 18.7 19.8 5.6 20.1Z", "M14.8 4.2 14.8 10.3 12.4 8.9 10 10.4 10 4.3",
                          "M8.1 13.1 15.6 12.9", "M8.1 16.1 13.5 16"],
                         circles: [(8.2, 18, 0.9), (11.1, 18, 0.9), (14, 18, 0.9)])
        let delegate = svg(["M3.6 5.5 11.3 5.2 11.5 12.5 3.8 12.8Z", "M12.8 11.6 20.4 11.3 20.2 18.7 12.9 19Z",
                            "M8.1 13.9C9.8 16.6 10.5 16.8 12.7 15.8", "M10.9 14.1 12.8 15.8 10.8 17.2",
                            "M5.8 8 9.1 7.9", "M15 14.3 18.3 14.2"])
        let status = svg(["M4.5 16.2C5 9.7 8.2 5.7 12.1 5.7c4.1 0 7.1 4 7.5 10.5", "M6.6 17.1 17.4 17",
                          "M12.1 15.2 15.6 10.7", "M7.4 13.7 8.7 13.2", "M12 8.3V9.9", "M16.7 13.7 15.4 13.2"],
                         circles: [(12.1, 15.2, 0.9)])
        let browser = svg(["M3.8 5.2 20.6 5 20.3 18.6 4 18.9Z", "M4.1 8.3 20.2 8.1", "M6.4 6.7 6.5 6.8",
                           "M8.7 6.6 8.8 6.7", "M10.8 11.1 15.7 16.5 13.1 16 12.2 18.3 9.9 12.2Z",
                           "M16.4 10.9 18.8 10.9 18.7 13.2", "M17.4 11.9 18.8 10.9"])
        let handoff = svg(["M4.9 5.5 19.2 5.2 19 18.6 5.2 18.9Z", "M6.9 12.4C7.7 11.2 9.8 11.1 10.8 12.2",
                           "M13.8 12.3C14.7 11.2 16.6 11.1 17.6 12.1", "M8.3 15.6 14.9 15.5",
                           "M13.3 14.1 15 15.5 13.3 17", "M3.1 13.9C4.1 13.3 4.7 13.1 5.5 13.2",
                           "M20.8 10.8C20.1 11.6 19.6 12 18.9 12.2"],
                          circles: [(8.8, 9.3, 1.25), (15.7, 9.2, 1.25)])
        return ["read_file": file, "run_shell": shell, "search_files": search, "web_search": browser, "apply_patch": patch,
                "memory": memory, "delegate": delegate, "team_status": status, "handoff": handoff]
    }()
}
