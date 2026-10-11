"""Render the production QR code and decode it with Apple Vision."""
from pathlib import Path
import platform
import subprocess
import tempfile
import unittest


@unittest.skipUnless(platform.system() == "Darwin", "requires Core Image and Vision")
class PairingCodeTests(unittest.TestCase):
    def test_qr_roundtrip_encodes_only_public_bootstrap(self):
        fixture = r'''
import Foundation
import CoreImage
import Vision
@main struct Test {
 static func fail(_ message: String) -> Never {
  FileHandle.standardError.write(Data((message + "\n").utf8))
  exit(1)
 }
 static func decode(_ image: CGImage, expected: String, label: String) throws {
  let request = VNDetectBarcodesRequest()
  request.symbologies = [.qr]
  request.usesCPUOnly = true
  try VNImageRequestHandler(cgImage:image,orientation:.up,options:[:]).perform([request])
  let values = request.results?.compactMap(\.payloadStringValue) ?? []
  guard values.contains(expected) else {
   let detector = CIDetector(ofType: CIDetectorTypeQRCode,
       context: CIContext(options:[.useSoftwareRenderer:true]),
       options:[CIDetectorAccuracy:CIDetectorAccuracyHigh])
   let independent = detector?.features(in:CIImage(cgImage:image)).compactMap {
       ($0 as? CIQRCodeFeature)?.messageString
   } ?? []
   fail("QR roundtrip failed (\(label)): OS=\(ProcessInfo.processInfo.operatingSystemVersionString) Vision revision=\(request.revision) image=\(image.width)x\(image.height) bpp=\(image.bitsPerPixel) alpha=\(image.alphaInfo.rawValue) Vision=\(values) CoreImage=\(independent)")
  }
 }
 @MainActor static func main() throws {
  let key = Data(repeating: 7, count: 32).base64EncodedString()
  let bootstrap: [String: Any] = ["host":"192.168.1.23", "port":8765,
      "sessionId":"providerhub-phone", "macIdentityPubKey":key,
      "privateKey":"MUST_NOT_BE_ENCODED", "token":"MUST_NOT_BE_ENCODED"]
  guard let text = ChatPairingCode.payload(bootstrap), let image = ChatPairingCode.image(for: text) else { fatalError("QR generation failed") }
  precondition(image.bitsPerComponent == 8 && image.bitsPerPixel == 32)
  precondition(image.alphaInfo == .noneSkipLast)
  // Verify the quiet zone is opaque white, not transparent/uninitialized.
  let bytes = image.dataProvider!.data! as Data
  for y in 0..<24 {
   for x in 0..<image.width {
    let offset = y * image.bytesPerRow + x * 4
    precondition(bytes[offset] == 255 && bytes[offset+1] == 255 && bytes[offset+2] == 255)
   }
  }
  try decode(image, expected:text, label:"native bitmap")
  // Devices displays at 168 points; also cover a larger presentation size.
  for size in [168, 220] {
   let display = CGContext(data:nil,width:size,height:size,bitsPerComponent:8,
       bytesPerRow:size*4,space:CGColorSpaceCreateDeviceRGB(),
       bitmapInfo:CGImageAlphaInfo.noneSkipLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue)!
   display.interpolationQuality = .none
   display.setShouldAntialias(false)
   display.draw(image,in:CGRect(x:0,y:0,width:size,height:size))
   try decode(display.makeImage()!, expected:text, label:"\(size)-point display")
  }
  let decoded = try JSONSerialization.jsonObject(with:Data(text.utf8)) as! [String:Any]
  precondition(Set(decoded.keys) == Set(["host","port","sessionId","macIdentityPubKey"]))
  precondition(decoded["macIdentityPubKey"] as? String == key)
  precondition(ChatPairingCode.payload(["host":"192.168.1.23"]) == nil)
  precondition(ChatPairingCode.image(for:String(repeating:"a",count:4097)) == nil)
  print("PAIRING_QR_ROUNDTRIP_OK")
 }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Main.swift"
            source.write_text(fixture)
            executable = root / "pairing-code"
            built = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                str(Path(__file__).with_name("ChatPairingCode.swift")), str(source), "-framework", "CoreImage",
                "-framework", "Vision", "-o", str(executable)], capture_output=True, text=True, timeout=90)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PAIRING_QR_ROUNDTRIP_OK", result.stdout)


if __name__ == "__main__": unittest.main()
