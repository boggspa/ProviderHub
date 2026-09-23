import Foundation

// The self-contained vector master is tracked beside this generator. See
// AppIcon.provenance.json for its TaskWraith ghost and catalogue glyph sources.
// Requires librsvg (rsvg-convert). From the repository root:
//   swift Source/make_icon.swift /tmp/ProviderHub.iconset
//   iconutil -c icns /tmp/ProviderHub.iconset -o Source/AppIcon.icns
//   rsvg-convert -w 1024 -h 1024 Source/AppIcon.svg -o Source/AppIcon.png

guard CommandLine.arguments.count == 2 else {
    fputs("Usage: swift make_icon.swift <output.iconset>\n", stderr)
    exit(2)
}

let directory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let source = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent()
    .appendingPathComponent("AppIcon.svg")

guard FileManager.default.fileExists(atPath: source.path) else {
    fputs("Missing AppIcon.svg beside make_icon.swift\n", stderr)
    exit(2)
}

try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let suffix = scale == 2 ? "@2x" : ""
        let target = directory.appendingPathComponent("icon_\(size)x\(size)\(suffix).png")
        let dimension = String(size * scale)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = [
            "rsvg-convert", "-w", dimension, "-h", dimension,
            "-o", target.path, source.path
        ]
        try process.run()
        process.waitUntilExit()
        guard process.terminationReason == .exit, process.terminationStatus == 0 else {
            fputs("Icon rendering failed; ensure rsvg-convert is installed.\n", stderr)
            exit(1)
        }
    }
}
