import Foundation
import CoreImage

/// Encodes only the public connection bootstrap. Pairing still requires the
/// matching code and device approval; the QR contains no private key or token.
@MainActor
enum ChatPairingCode {
    static func payload(_ bootstrap: [String: Any]) -> String? {
        guard let host = bootstrap["host"] as? String, !host.isEmpty,
              let port = bootstrap["port"] as? Int, (1...65535).contains(port),
              let session = bootstrap["sessionId"] as? String, !session.isEmpty,
              let key = bootstrap["macIdentityPubKey"] as? String,
              Data(base64Encoded: key)?.count == 32,
              let data = try? JSONSerialization.data(withJSONObject: [
                "host": host, "port": port, "sessionId": session, "macIdentityPubKey": key
              ], options: [.sortedKeys]), data.count <= 4096 else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func image(for payload: String) -> CGImage? {
        guard !payload.isEmpty, payload.utf8.count <= 4096,
              let filter = CIFilter(name: "CIQRCodeGenerator") else { return nil }
        filter.setValue(Data(payload.utf8), forKey: "inputMessage")
        filter.setValue("M", forKey: "inputCorrectionLevel")
        guard let code = filter.outputImage else { return nil }
        // Materialize the native one-pixel modules in software before scaling.
        // This avoids GPU/lazy CI compositing and unspecified output pixel formats
        // varying between headless macOS runners and the desktop renderer.
        let context = CIContext(options: [.useSoftwareRenderer: true])
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        guard let modules = context.createCGImage(code, from: code.extent,
                                                 format: .RGBA8, colorSpace: colorSpace) else { return nil }
        let scale = 6, border = 4
        let width = (modules.width + border * 2) * scale
        let height = (modules.height + border * 2) * scale
        guard let bitmap = CGContext(data: nil, width: width, height: height,
            bitsPerComponent: 8, bytesPerRow: width * 4, space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue) else { return nil }
        bitmap.setFillColor(CGColor(gray: 1, alpha: 1))
        bitmap.fill(CGRect(x: 0, y: 0, width: width, height: height))
        bitmap.interpolationQuality = .none
        bitmap.setShouldAntialias(false)
        bitmap.draw(modules, in: CGRect(x: border * scale, y: border * scale,
                                       width: modules.width * scale, height: modules.height * scale))
        return bitmap.makeImage()
    }
}
