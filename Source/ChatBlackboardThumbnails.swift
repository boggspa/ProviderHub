import AppKit
import AVFoundation
import ImageIO
import PDFKit
import UniformTypeIdentifiers

/// Small previews for Blackboard files, rendered off the main thread and cached
/// as PNGs (at most `maxEdge` pixels on a side) in the chat's private
/// `blackboard/thumbnails` folder, then in memory. Images use ImageIO's
/// thumbnail decoder, video a filmstrip of evenly spaced frames, audio a peak
/// waveform read with AVAssetReader, and PDFs their first page. A file that
/// cannot be read, including a truncated or corrupt one, yields nil and keeps
/// its glyph chip; nothing is cached for it. This file depends only on system
/// frameworks, so test_chat_blackboard_media.py compiles it on its own.
enum ChatBlackboardThumbnails {
    static let maxEdge = 360
    static let waveformBuckets = 200
    static let filmstripFrames = 5
    static let frameEdge = 160
    static let kinds: Set<String> = ["image", "video", "audio", "pdf"]
    private static let memory: NSCache<NSString, NSImage> = { let cache = NSCache<NSString, NSImage>(); cache.countLimit = 200; return cache }()
    private static let stats = Stats()

    /// Renders actually performed (cache misses), and how many of them ran on
    /// the main thread, which should always be none.
    static var renderCount: Int { stats.read().renders }
    static var mainThreadRenderCount: Int { stats.read().onMain }
    /// Drops the in-memory layer; the PNG cache on disk is kept.
    static func forgetMemory() { memory.removeAllObjects() }

    static func load(path: String, kind: String, id: String, directory: String) async -> NSImage? {
        guard kinds.contains(kind), !directory.isEmpty, !id.isEmpty,
              id.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber) }) else { return nil }
        let key = (directory + "/" + id) as NSString
        if let image = memory.object(forKey: key) { return image }
        let image = await Task.detached(priority: .utility) { () -> NSImage? in
            let folder = URL(fileURLWithPath: directory, isDirectory: true)
            let target = folder.appendingPathComponent(id + ".png")
            if let source = CGImageSourceCreateWithURL(target as CFURL, nil),
               let cached = CGImageSourceCreateImageAtIndex(source, 0, nil) { return points(cached) }
            stats.record(onMain: pthread_main_np() != 0)
            let rendered = await render(kind, URL(fileURLWithPath: path))
            stats.record(onMain: pthread_main_np() != 0, finished: true)
            guard let rendered else { return nil }
            store(rendered, at: target, in: folder)
            return points(rendered)
        }.value
        if let image { memory.setObject(image, forKey: key) }
        return image
    }

    static func render(_ kind: String, _ url: URL) async -> CGImage? {
        switch kind {
        case "image": return still(url)
        case "pdf": return page(url)
        case "video": return filmstrip(await frames(url))
        case "audio": return (await waveformBars(url)).flatMap(waveformImage)
        default: return nil
        }
    }

    static func still(_ url: URL) -> CGImage? {
        guard whole(url), let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              CGImageSourceGetStatusAtIndex(source, 0) == .statusComplete else { return nil }
        return CGImageSourceCreateThumbnailAtIndex(source, 0, [
            kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: maxEdge] as CFDictionary)
    }

    /// ImageIO decodes a truncated PNG, GIF or JPEG without error and reports
    /// it complete, filling the missing rows. Their end markers show whether
    /// the file is whole: PNG's IEND chunk, GIF's trailer byte, and JPEG's EOI
    /// near the end (some cameras append data after it). Other formats are
    /// left to ImageIO.
    static func whole(_ url: URL) -> Bool {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return false }
        defer { try? handle.close() }
        guard let head = try? handle.read(upToCount: 8), let end = try? handle.seekToEnd() else { return false }
        let span = min(end, 4096)
        guard (try? handle.seek(toOffset: end - span)) != nil, let tail = try? handle.read(upToCount: Int(span)) else { return false }
        if head.starts(with: [0x89, 0x50, 0x4E, 0x47]) { return tail.suffix(8).elementsEqual([0x49, 0x45, 0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82]) }
        if head.starts(with: [0x47, 0x49, 0x46, 0x38]) { return tail.last == 0x3B }
        if head.starts(with: [0xFF, 0xD8, 0xFF]) { return tail.range(of: Data([0xFF, 0xD9])) != nil }
        return true
    }

    static func page(_ url: URL) -> CGImage? {
        // A page does not retain its document; keep it alive while drawing.
        guard let document = PDFDocument(url: url), let page = document.page(at: 0) else { return nil }
        return withExtendedLifetime(document) { draw(page) }
    }

    private static func draw(_ page: PDFPage) -> CGImage? {
        let bounds = page.bounds(for: .cropBox)
        guard bounds.width > 0, bounds.height > 0 else { return nil }
        let scale = CGFloat(maxEdge) / max(bounds.width, bounds.height)
        let size = CGSize(width: max(1, (bounds.width * scale).rounded()), height: max(1, (bounds.height * scale).rounded()))
        guard let context = canvas(size) else { return nil }
        context.setFillColor(NSColor.white.cgColor); context.fill(CGRect(origin: .zero, size: size))
        context.scaleBy(x: scale, y: scale); context.translateBy(x: -bounds.minX, y: -bounds.minY)
        page.draw(with: .cropBox, to: context)
        return context.makeImage()
    }

    /// `filmstripFrames` evenly spaced frames, each at most `frameEdge` on a side.
    static func frames(_ url: URL) async -> [CGImage] {
        let asset = AVURLAsset(url: url)
        guard let duration = try? await asset.load(.duration), duration.seconds.isFinite, duration.seconds > 0 else { return [] }
        let generator = AVAssetImageGenerator(asset: asset)
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: frameEdge, height: frameEdge)
        let tolerance = CMTime(seconds: max(0.1, duration.seconds / Double(filmstripFrames * 4)), preferredTimescale: 600)
        generator.requestedTimeToleranceBefore = tolerance; generator.requestedTimeToleranceAfter = tolerance
        var frames: [CGImage] = []
        for index in 0..<filmstripFrames {
            if Task.isCancelled { return [] }
            let time = CMTime(seconds: duration.seconds * (Double(index) + 0.5) / Double(filmstripFrames), preferredTimescale: 600)
            if let frame = try? await generator.image(at: time).image { frames.append(frame) }
        }
        return frames
    }

    /// Frames side by side with a small gap, scaled to fit `maxEdge` wide.
    static func filmstrip(_ frames: [CGImage]) -> CGImage? {
        guard let first = frames.first, first.height > 0 else { return nil }
        let height = CGFloat(min(maxEdge / filmstripFrames * 3 / 2, first.height))
        let widths = frames.map { CGFloat($0.width) * height / CGFloat(max(1, $0.height)) }
        let gap: CGFloat = 2
        var total = widths.reduce(0, +) + gap * CGFloat(frames.count - 1)
        let shrink = min(1, CGFloat(maxEdge) / total)
        total = (total * shrink).rounded()
        guard let context = canvas(CGSize(width: max(1, total), height: max(1, (height * shrink).rounded()))) else { return nil }
        var x: CGFloat = 0
        for (frame, width) in zip(frames, widths) {
            context.draw(frame, in: CGRect(x: x, y: 0, width: width * shrink, height: height * shrink))
            x += (width + gap) * shrink
        }
        return context.makeImage()
    }

    /// `waveformBuckets` peak amplitudes (0...1) from 16-bit PCM. Peaks are
    /// first gathered per fixed run of samples, so the duration need not be
    /// known, then reduced to buckets. Nil when nothing could be read.
    static func waveformBars(_ url: URL) async -> [Float]? {
        let asset = AVURLAsset(url: url)
        guard let track = try? await asset.loadTracks(withMediaType: .audio).first,
              let reader = try? AVAssetReader(asset: asset) else { return nil }
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
            AVFormatIDKey: kAudioFormatLinearPCM, AVLinearPCMBitDepthKey: 16, AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false, AVLinearPCMIsNonInterleaved: false])
        output.alwaysCopiesSampleData = false
        guard reader.canAdd(output) else { return nil }
        reader.add(output)
        guard reader.startReading() else { return nil }
        let run = 2048
        var peaks: [Float] = [], peak: Int32 = 0, count = 0
        var samples: [Int16] = []
        while let buffer = output.copyNextSampleBuffer() {
            if Task.isCancelled || peaks.count > 4_000_000 { reader.cancelReading(); return nil }
            guard let block = CMSampleBufferGetDataBuffer(buffer) else { continue }
            let length = CMBlockBufferGetDataLength(block) / 2
            guard length > 0 else { continue }
            if samples.count < length { samples = [Int16](repeating: 0, count: length) }
            let copied = samples.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length * 2, destination: $0.baseAddress!) }
            guard copied == kCMBlockBufferNoErr else { continue }
            for index in 0..<length {
                let value = abs(Int32(samples[index]))
                if value > peak { peak = value }
                count += 1
                if count == run { peaks.append(Float(peak) / 32768); peak = 0; count = 0 }
            }
        }
        if count > 0 { peaks.append(Float(peak) / 32768) }
        guard reader.status == .completed, !peaks.isEmpty else { return nil }
        let buckets = waveformBuckets
        var bars = [Float](repeating: 0, count: buckets)
        for bucket in 0..<buckets {
            let start = bucket * peaks.count / buckets, end = max(start + 1, (bucket + 1) * peaks.count / buckets)
            bars[bucket] = peaks[min(start, peaks.count - 1)..<min(end, peaks.count)].max() ?? 0
        }
        return bars
    }

    /// Bars scaled to the loudest one across `maxEdge` pixels; silence draws a flat line.
    static func waveformImage(_ bars: [Float]) -> CGImage? {
        guard !bars.isEmpty else { return nil }
        let loudest = max(bars.max() ?? 0, 0.001)
        let size = CGSize(width: maxEdge, height: 72), step = size.width / CGFloat(bars.count)
        guard let context = canvas(size) else { return nil }
        context.setFillColor(NSColor.black.cgColor)
        for (index, bar) in bars.enumerated() {
            let height = max(1.5, CGFloat(bar / loudest) * (size.height - 4))
            context.fill(CGRect(x: CGFloat(index) * step, y: (size.height - height) / 2, width: step * 0.7, height: height))
        }
        return context.makeImage()
    }

    /// Thumbnails are rendered at twice their drawn size, for Retina displays.
    private static func points(_ image: CGImage) -> NSImage {
        NSImage(cgImage: image, size: NSSize(width: image.width / 2, height: image.height / 2))
    }

    private static func canvas(_ size: CGSize) -> CGContext? {
        CGContext(data: nil, width: Int(size.width), height: Int(size.height), bitsPerComponent: 8, bytesPerRow: 0,
                  space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
    }

    private static func store(_ image: CGImage, at target: URL, in folder: URL) {
        // Private like the rest of the chat's storage; a cache that cannot be
        // written only costs a re-render next time.
        guard (try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true,
                                                         attributes: [.posixPermissions: 0o700])) != nil else { return }
        let data = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(data, UTType.png.identifier as CFString, 1, nil) else { return }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else { return }
        try? (data as Data).write(to: target, options: [.atomic])
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: target.path)
    }

    private final class Stats: @unchecked Sendable {
        private let lock = NSLock()
        private var renders = 0, onMain = 0
        func record(onMain main: Bool, finished: Bool = false) {
            lock.lock(); defer { lock.unlock() }
            if !finished { renders += 1 }
            if main { onMain += 1 }
        }
        func read() -> (renders: Int, onMain: Int) { lock.lock(); defer { lock.unlock() }; return (renders, onMain) }
    }
}
