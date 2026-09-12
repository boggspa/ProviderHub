import AppKit
import Foundation
let directory = CommandLine.arguments[1]
try FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true)
for size in [16, 32, 64, 128, 256, 512, 1024] {
    let image = NSImage(size: NSSize(width: size, height: size))
    image.lockFocus()
    let s = CGFloat(size)
    let rect = NSRect(x: s * 0.05, y: s * 0.05, width: s * 0.9, height: s * 0.9)
    NSColor(calibratedRed: 0.11, green: 0.12, blue: 0.14, alpha: 1).setFill()
    NSBezierPath(roundedRect: rect, xRadius: s * 0.2, yRadius: s * 0.2).fill()
    let path = NSBezierPath()
    path.move(to: NSPoint(x: s * 0.25, y: s * 0.30))
    path.line(to: NSPoint(x: s * 0.25, y: s * 0.64))
    path.curve(to: NSPoint(x: s * 0.75, y: s * 0.64), controlPoint1: NSPoint(x: s * 0.42, y: s * 0.43), controlPoint2: NSPoint(x: s * 0.58, y: s * 0.43))
    path.line(to: NSPoint(x: s * 0.75, y: s * 0.30))
    path.lineWidth = s * 0.075; path.lineCapStyle = .round; path.lineJoinStyle = .round
    NSColor(calibratedRed: 1.0, green: 0.43, blue: 0.18, alpha: 1).setStroke(); path.stroke()
    NSColor(calibratedWhite: 0.96, alpha: 1).setFill()
    for x in [0.25, 0.75] { NSBezierPath(ovalIn: NSRect(x: s * (x - 0.055), y: s * 0.645, width: s * 0.11, height: s * 0.11)).fill() }
    image.unlockFocus()
    let data = NSBitmapImageRep(data: image.tiffRepresentation!)!.representation(using: .png, properties: [:])!
    let name = size == 1024 ? "icon_512x512@2x.png" : "icon_\(size)x\(size).png"
    try data.write(to: URL(fileURLWithPath: directory).appendingPathComponent(name))
    if size > 16 && size <= 512 {
        try data.write(to: URL(fileURLWithPath: directory).appendingPathComponent("icon_\(size/2)x\(size/2)@2x.png"))
    }
}
