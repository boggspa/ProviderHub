"""Real media through the Blackboard thumbnail code, on macOS.

Fixtures are made at test time and never committed: Python writes WAV files
with the wave module; the Swift harness writes a PNG, a one-page PDF with
CoreGraphics and a five-frame H.264 clip with AVAssetWriter, then exercises
ChatBlackboardThumbnails.swift exactly as the app compiles it.
"""
from array import array
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave

CASES = r'''
import AppKit
import AVFoundation
import CoreVideo
import ImageIO
import UniformTypeIdentifiers

@main struct Cases {
    static func check(_ yes: Bool, _ text: @autoclosure () -> String) { if !yes { fatalError(text()) } }

    /// Opaque dark pixels: drawn ink on a page, frame or waveform.
    static func ink(_ image: CGImage) -> Int {
        let width = image.width, height = image.height
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        let drawn = pixels.withUnsafeMutableBytes { bytes -> Bool in
            guard let context = CGContext(data: bytes.baseAddress, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
                                          space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
            context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height)); return true
        }
        check(drawn, "could not inspect pixels")
        var count = 0
        for i in stride(from: 0, to: pixels.count, by: 4) where pixels[i + 3] > 128 {
            let sum: Int = Int(pixels[i]) + Int(pixels[i + 1]) + Int(pixels[i + 2])
            if sum < 300 { count += 1 }
        }
        return count
    }

    static func writePNG(_ url: URL, width: Int, height: Int) {
        let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                                space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        context.setFillColor(CGColor(red: 0.2, green: 0.5, blue: 0.9, alpha: 1)); context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        let destination = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil)!
        CGImageDestinationAddImage(destination, context.makeImage()!, nil)
        check(CGImageDestinationFinalize(destination), "PNG fixture not written")
    }

    static func writePDF(_ url: URL) {
        var box = CGRect(x: 0, y: 0, width: 612, height: 792)
        let context = CGContext(url as CFURL, mediaBox: &box, nil)!
        context.beginPDFPage(nil)
        context.setFillColor(CGColor(gray: 0, alpha: 1)); context.fill(CGRect(x: 100, y: 150, width: 400, height: 500))
        context.endPDFPage(); context.closePDF()
    }

    static func writeMovie(_ url: URL, frames: Int, width: Int, height: Int) async {
        let writer = try! AVAssetWriter(outputURL: url, fileType: .mov)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height])
        input.expectsMediaDataInRealTime = false
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height])
        writer.add(input)
        check(writer.startWriting(), "movie writer did not start: \(String(describing: writer.error))")
        writer.startSession(atSourceTime: .zero)
        for index in 0..<frames {
            while !input.isReadyForMoreMediaData { try? await Task.sleep(nanoseconds: 2_000_000) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, adaptor.pixelBufferPool!, &buffer)
            let frame = buffer!
            CVPixelBufferLockBaseAddress(frame, [])
            let row = CVPixelBufferGetBytesPerRow(frame), base = CVPixelBufferGetBaseAddress(frame)!.assumingMemoryBound(to: UInt8.self)
            let shade = UInt8(40 + index * 40)
            for y in 0..<height { for x in 0..<width { let p = base + y * row + x * 4; p[0] = shade; p[1] = shade; p[2] = 255 - shade; p[3] = 255 } }
            CVPixelBufferUnlockBaseAddress(frame, [])
            check(adaptor.append(frame, withPresentationTime: CMTime(value: Int64(index), timescale: 5)), "frame \(index) not appended")
        }
        input.markAsFinished()
        writer.endSession(atSourceTime: CMTime(value: Int64(frames), timescale: 5))
        await withCheckedContinuation { (done: CheckedContinuation<Void, Never>) in writer.finishWriting { done.resume() } }
        check(writer.status == .completed, "movie fixture failed: \(String(describing: writer.error))")
    }

    static func pixelSize(_ url: URL) -> (Int, Int)? {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? Int, let height = properties[kCGImagePropertyPixelHeight] as? Int else { return nil }
        return (width, height)
    }

    @MainActor static func main() async {
        typealias T = ChatBlackboardThumbnails
        let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let thumbnails = root.appendingPathComponent("thumbnails", isDirectory: true)
        func file(_ name: String) -> URL { root.appendingPathComponent(name) }
        func thumbnail(_ id: String) -> URL { thumbnails.appendingPathComponent(id + ".png") }
        func load(_ url: URL, _ kind: String, _ id: String) async -> NSImage? {
            await T.load(path: url.path, kind: kind, id: id, directory: thumbnails.path)
        }
        check(pthread_main_np() != 0, "the harness must ask from the main thread")
        writePNG(file("photo.png"), width: 1200, height: 800)
        writePDF(file("page.pdf"))
        await writeMovie(file("clip.mov"), frames: 5, width: 320, height: 240)

        // Image: ImageIO's thumbnail, bounded to maxEdge on the long side.
        let still = T.still(file("photo.png"))
        check(still?.width == T.maxEdge && still?.height == 240, "image thumbnail \(String(describing: still?.width))x\(String(describing: still?.height))")

        // PDF: the first page is drawn, not blank.
        guard let page = T.page(file("page.pdf")) else { fatalError("PDF page not rendered") }
        check(max(page.width, page.height) == T.maxEdge, "PDF page not bounded")
        let pageInk = ink(page)
        check(pageInk > page.width * page.height / 5 && pageInk < page.width * page.height * 9 / 10, "PDF page blank or solid: \(pageInk)")

        // Video: five frames at the generator's bound, then a filmstrip.
        let frames = await T.frames(file("clip.mov"))
        check(frames.count == T.filmstripFrames, "filmstrip has \(frames.count) frames")
        check(frames.allSatisfy { $0.width == T.frameEdge && $0.height == 120 }, "frame sizes \(frames.map { "\($0.width)x\($0.height)" })")
        guard let strip = T.filmstrip(frames) else { fatalError("filmstrip not composed") }
        check(strip.width <= T.maxEdge && strip.width > strip.height * 3, "filmstrip \(strip.width)x\(strip.height)")

        // Audio: a tone has amplitude everywhere; silence is flat and draws differently.
        guard let tone = await T.waveformBars(file("tone.wav")), let silence = await T.waveformBars(file("silence.wav")) else {
            fatalError("waveform not read")
        }
        check(tone.count == T.waveformBuckets && silence.count == T.waveformBuckets, "bucket counts \(tone.count), \(silence.count)")
        check(tone.min()! > 0.5 && tone.max()! < 0.65, "tone peaks \(tone.min()!)...\(tone.max()!)")
        check(silence.allSatisfy { $0 == 0 }, "silence has amplitude")
        let toneInk = ink(T.waveformImage(tone)!), silenceInk = ink(T.waveformImage(silence)!)
        check(toneInk > silenceInk * 10 && silenceInk > 0, "tone and silence waveforms alike: \(toneInk) vs \(silenceInk)")

        // The full load path for each kind, caching a bounded PNG on disk.
        let start = T.renderCount
        for (url, kind, id) in [(file("photo.png"), "image", "photo"), (file("page.pdf"), "pdf", "page"),
                                (file("clip.mov"), "video", "clip"), (file("tone.wav"), "audio", "tone")] {
            check(await load(url, kind, id) != nil, "\(kind) load failed")
            guard let size = pixelSize(thumbnail(id)) else { fatalError("\(kind) thumbnail not cached") }
            check(max(size.0, size.1) <= T.maxEdge, "\(kind) cache \(size) over bound")
        }
        check(T.renderCount == start + 4, "renders \(T.renderCount - start), expected 4")

        // A second request is a cache hit: memory first, then the PNG on disk.
        let cached = thumbnail("photo"), before = try! FileManager.default.attributesOfItem(atPath: cached.path)
        let first = await load(file("photo.png"), "image", "photo"), again = await load(file("photo.png"), "image", "photo")
        check(first != nil && first === again, "memory cache missed")
        T.forgetMemory()
        check(await load(file("photo.png"), "image", "photo") != nil, "disk cache unreadable")
        let after = try! FileManager.default.attributesOfItem(atPath: cached.path)
        check(T.renderCount == start + 4, "a cached thumbnail was regenerated")
        check(after[.modificationDate] as? Date == before[.modificationDate] as? Date
              && after[.systemFileNumber] as? Int == before[.systemFileNumber] as? Int, "the cached PNG was rewritten")

        // Truncated or corrupt files keep the chip: nil, no cache, no crash.
        let photo = try! Data(contentsOf: file("photo.png")), movie = try! Data(contentsOf: file("clip.mov"))
        let wav = try! Data(contentsOf: file("tone.wav"))
        let broken: [(String, String, Data)] = [
            ("image", "png", photo.prefix(photo.count / 2)), ("image", "png", Data("\u{89}PNG\r\n\u{1a}\nnot an image".utf8)),
            ("pdf", "pdf", Data("%PDF-1.4\nthis is not a PDF".utf8)), ("pdf", "pdf", Data("%PDF-".utf8)),
            // The clip is about a kilobyte; half of it ends inside its index (moov).
            ("video", "mov", movie.prefix(movie.count / 2)), ("video", "mov", Data(repeating: 7, count: 4096)),
            ("audio", "wav", wav.prefix(44)), ("audio", "wav", Data("RIFF....WAVEgarbage".utf8))]
        for (index, (kind, suffix, data)) in broken.enumerated() {
            let url = file("broken\(index).\(suffix)"); try! data.write(to: url)
            let image = await load(url, kind, "broken\(index)")
            check(image == nil, "broken \(kind) #\(index) produced a thumbnail")
            check(!FileManager.default.fileExists(atPath: thumbnail("broken\(index)").path), "broken \(kind) #\(index) was cached")
        }
        check(await load(file("missing.png"), "image", "missing") == nil, "a missing file produced a thumbnail")
        let unsupported = await load(file("photo.png"), "file", "other"), unsafe = await load(file("photo.png"), "image", "../escape")
        check(unsupported == nil && unsafe == nil, "an unsupported kind or unsafe id was rendered")

        check(T.renderCount > start + 4, "broken files were never attempted")
        check(T.mainThreadRenderCount == 0, "\(T.mainThreadRenderCount) renders ran on the main thread")
        print("Blackboard thumbnail checks passed")
    }
}
'''


def write_wav(path, amplitude, seconds=0.5, rate=44100):
    samples = array("h", (int(amplitude * 32767 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(int(seconds * rate))))
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(rate)
        stream.writeframes(samples.tobytes())


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "Thumbnail rendering needs macOS")
class BlackboardThumbnailTests(unittest.TestCase):
    def test_real_media_thumbnails_cache_and_fail_gracefully(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = root / "fixtures"; fixtures.mkdir()
            write_wav(fixtures / "tone.wav", 0.6)
            write_wav(fixtures / "silence.wav", 0)
            (root / "Cases.swift").write_text(CASES)
            binary = root / "thumbnail-tests"
            compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-module-cache-path", str(root / "cache"), str(Path(__file__).with_name("ChatBlackboardThumbnails.swift")),
                str(root / "Cases.swift"), "-framework", "AppKit", "-framework", "AVFoundation", "-framework", "PDFKit",
                "-framework", "CoreMedia", "-framework", "CoreVideo", "-o", str(binary)], capture_output=True, text=True, timeout=180)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary), str(fixtures)], capture_output=True, text=True, timeout=60)
            self.assertEqual(ran.returncode, 0, ran.stderr + ran.stdout)
            self.assertIn("thumbnail checks passed", ran.stdout)


if __name__ == "__main__": unittest.main()
