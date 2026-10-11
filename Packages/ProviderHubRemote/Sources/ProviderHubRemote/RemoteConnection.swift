import CryptoKit
import Foundation
import Network
import Security

public struct RemoteBootstrap: Codable {
    public var host: String
    public var port: UInt16
    public var sessionId: String
    public var macIdentityPubKey: String
}

/// TaskWraith v1 client handshake and frame codec, independently testable.
public final class RemoteChannel {
    let bootstrap: RemoteBootstrap
    let identity: Curve25519.Signing.PrivateKey
    let ephemeral = Curve25519.KeyAgreement.PrivateKey()
    let nonce = Data((0..<16).map { _ in UInt8.random(in: 0...255) })
    var transcript: Data?
    var keys: (macToPhone: SymmetricKey, phoneToMac: SymmetricKey)?
    var sent: UInt64 = 0
    var received: UInt64 = 0
    var messageID = 0
    public private(set) var established = false
    public private(set) var confirmCode: String?
    public var emit: (([String: Any]) throws -> Void)?
    public var onMessage: (([String: Any]) throws -> Void)?
    public init(bootstrap: RemoteBootstrap, identity: Curve25519.Signing.PrivateKey) {
        self.bootstrap = bootstrap
        self.identity = identity
    }
    public func start() throws {
        try emit?(["t": "clientHello", "protocol": RemoteE2EE.protocolName,
                   "sessionId": bootstrap.sessionId, "role": "iphone",
                   "ephemeralPubKey": ephemeral.publicKey.rawRepresentation.base64EncodedString(),
                   "nonce": nonce.base64EncodedString()])
    }
    func bytes(_ frame: [String: Any], _ key: String, count: Int? = nil) throws -> Data {
        guard let text = frame[key] as? String, let data = Data(base64Encoded: text),
              count == nil || data.count == count else { throw RemoteProtocolError.rejected }
        return data
    }
    public func receive(_ frame: [String: Any]) throws {
        guard frame["sessionId"] as? String == bootstrap.sessionId else { throw RemoteProtocolError.rejected }
        switch frame["t"] as? String {
        case "serverHello":
            guard keys == nil, frame["protocol"] as? String == RemoteE2EE.protocolName,
                  frame["macIdentityPubKey"] as? String == bootstrap.macIdentityPubKey else { throw RemoteProtocolError.rejected }
            let serverEphemeral = try bytes(frame, "ephemeralPubKey", count: 32)
            let serverNonce = try bytes(frame, "nonce", count: 16)
            keys = try RemoteE2EE.sessionKeys(myEphemeral: ephemeral, peerEphemeral: serverEphemeral, clientNonce: nonce, serverNonce: serverNonce)
            let digest = RemoteE2EE.transcriptHash(sessionId: bootstrap.sessionId,
                clientEphemeralPubKeyB64: ephemeral.publicKey.rawRepresentation.base64EncodedString(),
                serverEphemeralPubKeyB64: serverEphemeral.base64EncodedString(),
                clientNonceB64: nonce.base64EncodedString(), serverNonceB64: serverNonce.base64EncodedString(),
                macIdentityPubKeyB64: bootstrap.macIdentityPubKey,
                iphoneIdentityPubKeyB64: identity.publicKey.rawRepresentation.base64EncodedString())
            transcript = digest
            confirmCode = RemoteE2EE.confirmCode(digest)
            try emit?(["t": "clientAuth", "sessionId": bootstrap.sessionId,
                       "iphoneIdentityPubKey": identity.publicKey.rawRepresentation.base64EncodedString(),
                       "confirmCode": confirmCode!, "transcriptSig": try identity.signature(for: digest).base64EncodedString()])
        case "serverAuth":
            guard !established, let transcript, let raw = Data(base64Encoded: bootstrap.macIdentityPubKey) else { throw RemoteProtocolError.rejected }
            let key = try Curve25519.Signing.PublicKey(rawRepresentation: raw)
            guard key.isValidSignature(try bytes(frame, "transcriptSig", count: 64), for: transcript) else { throw RemoteProtocolError.rejected }
            established = true
            try send(method: "session.resume", params: ["afterCursor": -1])
        case "enc":
            guard established, let keys, let seq = frame["seq"] as? UInt64, seq == received else { throw RemoteProtocolError.gap }
            let body = try RemoteE2EE.open(key: keys.macToPhone, directionPrefix: 1, sessionId: bootstrap.sessionId, seq: seq,
                nonce: bytes(frame, "nonce", count: 12), ct: bytes(frame, "ct"), tag: bytes(frame, "tag", count: 16))
            guard let message = try JSONSerialization.jsonObject(with: body) as? [String: Any],
                  message["v"] as? Int == 1, message["proto"] as? String == "providerhub-remote-app-v1" else { throw RemoteProtocolError.rejected }
            received += 1
            try onMessage?(message)
        default: throw RemoteProtocolError.rejected
        }
    }
    public func send(method: String, params: [String: Any]) throws {
        guard established, let keys else { throw RemoteProtocolError.rejected }
        messageID += 1
        let body = try JSONSerialization.data(withJSONObject: ["v": 1, "proto": "providerhub-remote-app-v1", "msgId": messageID, "method": method, "params": params])
        let box = try RemoteE2EE.seal(key: keys.phoneToMac, directionPrefix: 2, sessionId: bootstrap.sessionId, seq: sent, plaintext: body)
        try emit?(["t": "enc", "sessionId": bootstrap.sessionId, "seq": sent,
                   "nonce": box.nonce.base64EncodedString(), "ct": box.ct.base64EncodedString(), "tag": box.tag.base64EncodedString()])
        sent += 1
    }
}

/// Persistent phone identity in Keychain. Pairing text pins the Mac identity.
public final class RemoteConnection: CompanionLink {
    private let queue = DispatchQueue(label: "providerhub.companion")
    private var connection: NWConnection?
    private var channel: RemoteChannel?
    private var buffer = Data()
    private var generation = UUID()
    public var onMessage: (([String: Any]) -> Void)?
    public var onStatus: ((String, String?) -> Void)?
    public init() {}
    private func identity() throws -> Curve25519.Signing.PrivateKey {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "ProviderHubCompanion", kSecAttrAccount as String: "identity-v1"]
        var item: CFTypeRef?
        var read = query
        read[kSecReturnData as String] = true
        let result = SecItemCopyMatching(read as CFDictionary, &item)
        if result == errSecSuccess, let data = item as? Data { return try .init(rawRepresentation: data) }
        guard result == errSecItemNotFound else { throw RemoteProtocolError.rejected }
        let key = Curve25519.Signing.PrivateKey()
        var write = query
        write[kSecValueData as String] = key.rawRepresentation
        write[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        guard SecItemAdd(write as CFDictionary, nil) == errSecSuccess else { throw RemoteProtocolError.rejected }
        return key
    }
    public func connect(text: String) {
        queue.async {
            do {
                let bootstrap = try JSONDecoder().decode(RemoteBootstrap.self, from: Data(text.utf8))
                guard bootstrap.port > 0, Data(base64Encoded: bootstrap.macIdentityPubKey)?.count == 32,
                      !bootstrap.host.isEmpty else { throw RemoteProtocolError.rejected }
                self.open(bootstrap, identity: try self.identity())
            } catch { self.onStatus?("Connection failed: \(error)", nil) }
        }
    }
    private func open(_ bootstrap: RemoteBootstrap, identity: Curve25519.Signing.PrivateKey) {
        connection?.cancel()
        generation = UUID()
        let current = generation
        buffer.removeAll()
        let channel = RemoteChannel(bootstrap: bootstrap, identity: identity)
        self.channel = channel
        let connection = NWConnection(host: .init(bootstrap.host), port: .init(rawValue: bootstrap.port)!, using: .tcp)
        self.connection = connection
        channel.emit = { [weak connection] frame in
            var data = try JSONSerialization.data(withJSONObject: frame)
            data.append(10)
            connection?.send(content: data, completion: .contentProcessed { _ in })
        }
        channel.onMessage = { [weak self] message in self?.onMessage?(message) }
        connection.stateUpdateHandler = { [weak self] state in
            guard let self, self.generation == current else { return }
            switch state {
            case .ready:
                do { try channel.start(); self.read(connection, current: current) }
                catch { self.onStatus?("Handshake failed", nil); connection.cancel() }
            case .failed(let error):
                self.onStatus?("Disconnected: \(error). Reconnecting…", nil)
                self.queue.asyncAfter(deadline: .now() + 3) {
                    if self.generation == current { self.open(bootstrap, identity: identity) }
                }
            default: break
            }
        }
        onStatus?("Connecting…", nil)
        connection.start(queue: queue)
    }
    private func read(_ connection: NWConnection, current: UUID) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, done, error in
            guard let self, self.generation == current else { return }
            do {
                if let data { self.buffer.append(data) }
                guard self.buffer.count <= 262144 else { throw RemoteProtocolError.rejected }
                while let end = self.buffer.firstIndex(of: 10) {
                    let line = self.buffer.prefix(upTo: end)
                    self.buffer.removeSubrange(...end)
                    guard let frame = try JSONSerialization.jsonObject(with: line) as? [String: Any] else { throw RemoteProtocolError.rejected }
                    try self.channel?.receive(frame)
                    self.onStatus?(self.channel?.established == true ? "Connected" : "Compare code and approve on your Mac", self.channel?.confirmCode)
                }
                if done || error != nil {
                    connection.cancel()
                    self.onStatus?("Disconnected. Reconnecting…", nil)
                    self.queue.asyncAfter(deadline: .now() + 3) {
                        if self.generation == current, let channel = self.channel { self.open(channel.bootstrap, identity: channel.identity) }
                    }
                } else { self.read(connection, current: current) }
            } catch { self.onStatus?("Secure connection rejected: \(error)", nil); self.generation = UUID(); connection.cancel() }
        }
    }
    public func send(_ command: RemoteCommand) {
        queue.async {
            do {
                var params: [String: Any] = ["requestId": command.requestId]
                if let value = command.chat { params["chat"] = value }
                if let value = command.text { params["text"] = value }
                if let value = command.approvalId { params["approvalId"] = value }
                if let value = command.allow { params["allow"] = value }
                try self.channel?.send(method: command.method, params: params)
            } catch { self.onStatus?("Command failed: \(error)", nil) }
        }
    }
    public func snapshot() { queue.async { try? self.channel?.send(method: "session.resume", params: ["afterCursor": -1]) } }
    public func disconnect() { queue.async { self.generation = UUID(); self.connection?.cancel(); self.channel = nil; self.onStatus?("Disconnected", nil) } }
}
