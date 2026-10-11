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
 @MainActor static func main() throws {
  let key = Data(repeating: 7, count: 32).base64EncodedString()
  let bootstrap: [String: Any] = ["host":"192.168.1.23", "port":8765,
      "sessionId":"providerhub-phone", "macIdentityPubKey":key,
      "privateKey":"MUST_NOT_BE_ENCODED", "token":"MUST_NOT_BE_ENCODED"]
  guard let text = ChatPairingCode.payload(bootstrap), let image = ChatPairingCode.image(for: text) else { fatalError("QR generation failed") }
  let request = VNDetectBarcodesRequest()
  request.symbologies = [.qr]
  try VNImageRequestHandler(cgImage:image,options:[:]).perform([request])
  guard request.results?.first?.payloadStringValue == text else { fatalError("QR did not roundtrip") }
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
