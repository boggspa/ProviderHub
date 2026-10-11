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
        let border: CGFloat = 4
        let extent = CGRect(x: 0, y: 0, width: code.extent.width + border * 2, height: code.extent.height + border * 2)
        let white = CIImage(color: CIColor(red: 1, green: 1, blue: 1)).cropped(to: extent)
        let framed = code.transformed(by: CGAffineTransform(translationX: border, y: border)).composited(over: white)
        let scaled = framed.transformed(by: CGAffineTransform(scaleX: 6, y: 6))
        return CIContext().createCGImage(scaled, from: scaled.extent)
    }
}
