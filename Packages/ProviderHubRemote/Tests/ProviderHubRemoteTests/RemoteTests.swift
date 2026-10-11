import CryptoKit
import XCTest
@testable import ProviderHubRemote

final class RemoteVectorTests: XCTestCase {
    func testGoldenTaskWraithVectors() throws {
        let macEph = try Curve25519.KeyAgreement.PrivateKey(rawRepresentation: Data(repeating: 0x11, count: 32))
        let phoneEph = try Curve25519.KeyAgreement.PrivateKey(rawRepresentation: Data(repeating: 0x22, count: 32))
        let macID = try Curve25519.Signing.PrivateKey(rawRepresentation: Data(repeating: 0x33, count: 32))
        let phoneID = try Curve25519.Signing.PrivateKey(rawRepresentation: Data(repeating: 0x44, count: 32))
        XCTAssertEqual(macEph.publicKey.rawRepresentation.hex, "7b4e909bbe7ffe44c465a220037d608ee35897d31ef972f07f74892cb0f73f13")
        XCTAssertEqual(phoneEph.publicKey.rawRepresentation.hex, "0faa684ed28867b97f4a6a2dee5df8ce974e76b7018e3f22a1c4cf2678570f20")
        XCTAssertEqual(macID.publicKey.rawRepresentation.hex, "17cb79fb2b4120f2b1ec65e4198d6e08b28e813feb01e4a400839b85e18080ce")
        XCTAssertEqual(phoneID.publicKey.rawRepresentation.hex, "d759793bbc13a2819a827c76adb6fba8a49aee007f49f2d0992d99b825ad2c48")
        let clientNonce = Data(repeating: 0xAA, count: 16)
        let serverNonce = Data(repeating: 0xBB, count: 16)
        let digest = RemoteE2EE.transcriptHash(
            sessionId: "vector-session",
            clientEphemeralPubKeyB64: phoneEph.publicKey.rawRepresentation.base64EncodedString(),
            serverEphemeralPubKeyB64: macEph.publicKey.rawRepresentation.base64EncodedString(),
            clientNonceB64: clientNonce.base64EncodedString(),
            serverNonceB64: serverNonce.base64EncodedString(),
            macIdentityPubKeyB64: macID.publicKey.rawRepresentation.base64EncodedString(),
            iphoneIdentityPubKeyB64: phoneID.publicKey.rawRepresentation.base64EncodedString())
        XCTAssertEqual(digest.hex, "9b9764575b8a73a88377d24588d88331e84bc7969972b2a50019fe1c0fdb264f")
        XCTAssertEqual(RemoteE2EE.confirmCode(digest), "390103")
        let keys = try RemoteE2EE.sessionKeys(myEphemeral: macEph, peerEphemeral: phoneEph.publicKey.rawRepresentation,
                                              clientNonce: clientNonce, serverNonce: serverNonce)
        XCTAssertEqual(keys.macToPhone.data.hex, "c328ecb05aed3e0c14dc8b25c0f2a4abf4300495c6a158f586d0833bd0b30a0c")
        XCTAssertEqual(keys.phoneToMac.data.hex, "9a72c741f010d393760f6fab2eb408d047e8014d0d5bda52c50ffb2171a863f0")
        let plaintext = Data("{\"msgId\":1,\"method\":\"bridge.runEvent\",\"params\":{\"n\":42}}".utf8)
        let sealed = try RemoteE2EE.seal(key: keys.macToPhone, directionPrefix: 1, sessionId: "vector-session", seq: 0, plaintext: plaintext)
        XCTAssertEqual(sealed.nonce.hex, "000000010000000000000000")
        XCTAssertEqual(sealed.ct.hex, "9eaacd940b78937c9689aa5b22b9d8151f7da06583bb95e9b4f72329fe2ac832066a363840346e697e774e5bbea2ddaa4d5512d17ed28f6d")
        XCTAssertEqual(sealed.tag.hex, "e02aef7c8b154d6fce01878e3e6d649d")
        let opened = try RemoteE2EE.open(key: keys.macToPhone, directionPrefix: 1, sessionId: "vector-session", seq: 0,
                                         nonce: sealed.nonce, ct: sealed.ct, tag: sealed.tag)
        XCTAssertEqual(opened, plaintext)
        var tampered = sealed.ct
        tampered[tampered.startIndex] ^= 1
        XCTAssertThrowsError(try RemoteE2EE.open(key: keys.macToPhone, directionPrefix: 1, sessionId: "vector-session", seq: 0,
                                                 nonce: sealed.nonce, ct: tampered, tag: sealed.tag))
        let signature = try macID.signature(for: digest)
        let goldenHex = "7e293c15123713d7392c0e4902313b51aca0d7fb5bb37cee64ac7e76d1e556cf5e6067f5c380ddd7600a5e7bc44a40ebc5db72d0a5fe838d9997d3f2e5635708"
        let golden = Data(stride(from: 0, to: goldenHex.count, by: 2).map { offset in
            let start = goldenHex.index(goldenHex.startIndex, offsetBy: offset)
            return UInt8(goldenHex[start..<goldenHex.index(start, offsetBy: 2)], radix: 16)!
        })
        XCTAssertTrue(macID.publicKey.isValidSignature(golden, for: digest))
        XCTAssertTrue(macID.publicKey.isValidSignature(signature, for: digest))
    }
}

final class CompanionStoreTests: XCTestCase {
    func testSnapshotDeltaDedupAndGap() {
        var store = CompanionStore()
        let chat = RemoteChat(id: "ab", title: "Notes", status: "Ready", busy: false, pendingApproval: false)
        store.apply(method: "session.snapshot", snapshot: RemoteSnapshot(cursor: 2, chats: [chat], approval: nil, selectedID: "ab"))
        store.apply(method: "session.delta", delta: RemoteDelta(cursor: 2, event: "notice", message: "old"))
        XCTAssertEqual(store.notices, [])
        store.apply(method: "session.delta", delta: RemoteDelta(cursor: 3, event: "notice", message: "new"))
        XCTAssertEqual(store.cursor, 3)
        XCTAssertEqual(store.notices, ["new"])
        store.apply(method: "session.delta", delta: RemoteDelta(cursor: 5, event: "notice", message: "skipped"))
        XCTAssertTrue(store.needsSnapshot)
        XCTAssertEqual(store.cursor, 3)
        XCTAssertEqual(store.notices, ["new"])
        let approval = RemoteApproval(id: "12", summary: "Run tests", detail: "pytest")
        store.apply(method: "session.snapshot", snapshot: RemoteSnapshot(cursor: 5, chats: [chat], approval: approval, selectedID: "ab"))
        XCTAssertFalse(store.needsSnapshot)
        XCTAssertEqual(store.approval, approval)
    }

    func testCommandCarriesNoCredentialFields() throws {
        let command = RemoteCommand(method: "chat.approve", requestId: "r1", chat: "abab", approvalId: "1212", allow: false)
        let data = try JSONEncoder().encode(command)
        let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        XCTAssertEqual(object?["proto"] as? String, "providerhub-remote-app-v1")
        XCTAssertEqual(object?["method"] as? String, "chat.approve")
        XCTAssertEqual(object?["allow"] as? Bool, false)
        XCTAssertNil(object?["apiKey"])
        XCTAssertNil(object?["authorization"])
    }
}

private extension Data {
    var hex: String { map { String(format: "%02x", $0) }.joined() }
}

private extension SymmetricKey {
    var data: Data { withUnsafeBytes { Data($0) } }
}
