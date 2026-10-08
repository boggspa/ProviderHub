import SwiftUI

/// The file/search/patch/shell glyphs from TaskWraith's
/// design-assets/tool-call-icons/tool-call-icons.catalog.svg. These are the
/// original 24×24 monoline paths, cached as native paths once; no SVG parser,
/// image loading, or whole-catalogue runtime dependency is needed.
struct ChatToolGlyph: View {
    var name: String
    var body: some View {
        GeometryReader { geometry in
            let scale = geometry.size.width / 24
            (Self.paths[name] ?? Self.paths["read_file"]!)
                .applying(CGAffineTransform(scaleX: scale, y: scale))
                .stroke(style: StrokeStyle(lineWidth: 1.65 * scale, lineCap: .round, lineJoin: .round))
        }.frame(width: 17, height: 17).accessibilityHidden(true)
    }

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
        return ["read_file": file, "run_shell": shell, "search_files": search, "apply_patch": patch]
    }()
}
