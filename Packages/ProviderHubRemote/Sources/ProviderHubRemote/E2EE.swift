import CryptoKit
import Foundation

/// TaskWraith `taskwraith-e2ee-v1` primitives.
/// Byte layout matches AGBench `src/shared/e2ee` and the 2026-06-11 golden vectors.
/// This is not an independent cryptosystem and does not speak the TaskWraith relay.
public enum RemoteE2EE {
    public static let protocolName = "taskwraith-e2ee-v1"
    public static let macToPhoneInfo = "taskwraith-e2ee-v1 mac->iphone"
    public static let phoneToMacInfo = "taskwraith-e2ee-v1 iphone->mac"

    public static func transcriptHash(
        sessionId: String,
        clientEphemeralPubKeyB64: String,
        serverEphemeralPubKeyB64: String,
        clientNonceB64: String,
        serverNonceB64: String,
        macIdentityPubKeyB64: String,
        iphoneIdentityPubKeyB64: String
    ) -> Data {
        let transcript = [
            protocolName, sessionId, clientEphemeralPubKeyB64, serverEphemeralPubKeyB64,
            clientNonceB64, serverNonceB64, macIdentityPubKeyB64, iphoneIdentityPubKeyB64,
        ].joined(separator: "|")
        return Data(SHA256.hash(data: Data(transcript.utf8)))
    }

    public static func confirmCode(_ transcript: Data) -> String {
        let bytes = [UInt8](transcript.prefix(4))
        precondition(bytes.count == 4)
        let value = (UInt32(bytes[0]) << 24) | (UInt32(bytes[1]) << 16) | (UInt32(bytes[2]) << 8) | UInt32(bytes[3])
        return String(format: "%06u", value % 1_000_000)
    }

    public static func sessionKeys(
        myEphemeral: Curve25519.KeyAgreement.PrivateKey,
        peerEphemeral: Data,
        clientNonce: Data,
        serverNonce: Data
    ) throws -> (macToPhone: SymmetricKey, phoneToMac: SymmetricKey) {
        let peer = try Curve25519.KeyAgreement.PublicKey(rawRepresentation: peerEphemeral)
        let shared = try myEphemeral.sharedSecretFromKeyAgreement(with: peer)
        let salt = Data(SHA256.hash(data: clientNonce + serverNonce))
        let mac = shared.hkdfDerivedSymmetricKey(using: SHA256.self, salt: salt,
                                                 sharedInfo: Data(macToPhoneInfo.utf8), outputByteCount: 32)
        let phone = shared.hkdfDerivedSymmetricKey(using: SHA256.self, salt: salt,
                                                   sharedInfo: Data(phoneToMacInfo.utf8), outputByteCount: 32)
        return (mac, phone)
    }

    public static func nonce(directionPrefix: UInt32, seq: UInt64) -> Data {
        var nonce = Data(count: 12)
        var prefix = directionPrefix.bigEndian
        var sequence = seq.bigEndian
        withUnsafeBytes(of: &prefix) { nonce.replaceSubrange(0..<4, with: $0) }
        withUnsafeBytes(of: &sequence) { nonce.replaceSubrange(4..<12, with: $0) }
        return nonce
    }

    public static func seal(key: SymmetricKey, directionPrefix: UInt32, sessionId: String, seq: UInt64, plaintext: Data) throws -> (nonce: Data, ct: Data, tag: Data) {
        let nonce = nonce(directionPrefix: directionPrefix, seq: seq)
        let aad = Data("\(sessionId):\(seq)".utf8)
        let box = try AES.GCM.seal(plaintext, using: key, nonce: AES.GCM.Nonce(data: nonce), authenticating: aad)
        return (nonce, box.ciphertext, box.tag)
    }

    public static func open(key: SymmetricKey, directionPrefix: UInt32, sessionId: String, seq: UInt64, nonce wireNonce: Data, ct: Data, tag: Data) throws -> Data {
        let expected = nonce(directionPrefix: directionPrefix, seq: seq)
        guard wireNonce == expected else { throw RemoteProtocolError.nonceMismatch }
        guard tag.count == 16 else { throw RemoteProtocolError.badTag }
        let aad = Data("\(sessionId):\(seq)".utf8)
        let box = try AES.GCM.SealedBox(nonce: AES.GCM.Nonce(data: expected), ciphertext: ct, tag: tag)
        return try AES.GCM.open(box, using: key, authenticating: aad)
    }
}

public enum RemoteProtocolError: Error {
    case nonceMismatch
    case badTag
    case gap
    case rejected
}
