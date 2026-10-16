import Foundation
import XCTest
@testable import ProviderHubRemote

final class PairingTests: XCTestCase {
    @MainActor
    func testLaunchRestoresSavedMacAndDisconnectPausesForegroundResume() throws {
        final class Transport: CompanionTransport {
            var endpoints: [String] = []
            var disconnects = 0
            func connect(text: String) { endpoints.append(text) }
            func disconnect() { disconnects += 1 }
            func snapshot() {}
            func send(_ command: RemoteCommand) {}
        }
        let suite = "companion-pairing-test-" + UUID().uuidString
        let preferences = UserDefaults(suiteName: suite)!
        defer { preferences.removePersistentDomain(forName: suite) }
        let transport = Transport()
        let model = CompanionModel(preferences: preferences, transport: transport)
        model.resumeSavedConnection()
        XCTAssertTrue(transport.endpoints.isEmpty)
        let saved = try payload(host: "100.99.131.73")
        model.connect(scannedCode: saved)
        XCTAssertEqual(preferences.string(forKey: "remote-bootstrap"), saved)

        // A new process/model reuses the saved Mac; no QR scan is needed.
        let reopened = CompanionModel(preferences: preferences, transport: transport)
        reopened.pairingText = "unsaved edit"
        reopened.resumeSavedConnection()
        XCTAssertEqual(transport.endpoints, [saved, saved])
        reopened.status = "Connected"
        reopened.resumeSavedConnection()
        XCTAssertEqual(transport.endpoints.count, 2)
        reopened.status = "Disconnected. Reconnecting…"
        reopened.resumeSavedConnection()
        XCTAssertEqual(transport.endpoints, [saved, saved, saved], "Foreground after a suspended socket reuses the saved Mac")
        reopened.disconnect()
        reopened.status = "Disconnected"
        reopened.resumeSavedConnection()
        XCTAssertEqual(transport.endpoints.count, 3)
        XCTAssertEqual(transport.disconnects, 1)
        XCTAssertEqual(preferences.string(forKey: "remote-bootstrap"), saved)
    }

    @MainActor
    func testInvalidSavedPairingDoesNotOpenAConnection() {
        final class Transport: CompanionTransport {
            var connects = 0
            func connect(text: String) { connects += 1 }
            func disconnect() {}
            func snapshot() {}
            func send(_ command: RemoteCommand) {}
        }
        let suite = "companion-invalid-pairing-test-" + UUID().uuidString
        let preferences = UserDefaults(suiteName: suite)!
        defer { preferences.removePersistentDomain(forName: suite) }
        preferences.set("not a pairing code", forKey: "remote-bootstrap")
        let transport = Transport()
        CompanionModel(preferences: preferences, transport: transport).resumeSavedConnection()
        XCTAssertEqual(transport.connects, 0)
    }

    @MainActor
    func testFirstChatSubscribesAndReconnectPreservesDraft() async {
        final class Link: CompanionLink {
            var commands: [RemoteCommand] = []
            func send(_ command: RemoteCommand) { commands.append(command) }
        }
        let link = Link()
        let model = CompanionModel()
        model.link = link
        let chats: [[String: Any]] = [["id":"fixture", "title":"Fixture", "status":"ready", "busy":false, "pendingApproval":false]]
        let empty: [String: Any] = ["method":"session.snapshot", "params":["cursor":1, "chats":chats, "selected":NSNull()]]
        model.receiveRemote(empty)
        XCTAssertEqual(model.selectedChatID, "fixture")
        XCTAssertEqual(link.commands.last?.method, "chat.select")
        XCTAssertEqual(link.commands.last?.chat, "fixture")
        model.draft = "Keep this draft"
        model.receiveRemote(empty)
        XCTAssertEqual(model.draft, "Keep this draft")
        XCTAssertEqual(link.commands.count, 2, "Reconnect without a subscription must rebind it")
        model.receiveRemote(["method":"session.snapshot", "params":["cursor":2, "chats":chats,
            "selected":["id":"fixture", "entries":[["role":"assistant", "text":"Connected"]]]]])
        XCTAssertEqual(link.commands.count, 3, "A full snapshot must rebind even when the saved selection matches")
        XCTAssertEqual(link.commands.last?.method, "chat.select")
        model.receiveRemote(["method":"session.delta", "params":["cursor":3, "event":["event":"snapshot", "chat":"fixture", "entries":[["role":"assistant", "text":"Connected"]]]]])
        XCTAssertEqual(link.commands.count, 3, "A subscription response must not send another subscription")
        XCTAssertTrue(model.store.transcript.contains("Connected"))
        XCTAssertEqual(model.draft, "Keep this draft")
    }

    private func payload(host: String = "192.168.1.23", port: Int = 8765, session: String = "providerhub-phone", key: String? = nil) throws -> String {
        let data = try JSONSerialization.data(withJSONObject: [
            "host": host, "port": port, "sessionId": session,
            "macIdentityPubKey": key ?? Data(repeating: 7, count: 32).base64EncodedString()
        ], options: [.sortedKeys])
        return String(decoding: data, as: UTF8.self)
    }

    func testPrivatePairingPayloadPreservesIdentity() throws {
        for address in ["192.168.1.23", "10.0.0.12", "172.16.0.4", "172.31.255.254", "100.64.0.1", "100.127.255.254", "fd7a:115c:a1e0::f501:834b", "FD7A:115C:A1E0::1", "127.0.0.1", "::1"] {
            let value = try RemoteBootstrap.decodePairingText("  \n" + payload(host: address) + "\n")
            XCTAssertEqual(value.host, address)
            XCTAssertEqual(value.port, 8765)
            XCTAssertEqual(value.sessionId, "providerhub-phone")
            XCTAssertEqual(value.macIdentityPubKey, Data(repeating: 7, count: 32).base64EncodedString())
        }
    }

    func testMalformedAndUnrelatedQRDataAreRejected() throws {
        for text in ["", "https://example.com", "providerhub://pair?token=unexpected", "{}", String(repeating: "x", count: 4097)] {
            XCTAssertThrowsError(try RemoteBootstrap.decodePairingText(text))
        }
        for address in ["0.0.0.0", "8.8.8.8", "example.com", "169.254.169.254", "100.63.255.255", "100.128.0.1", "172.32.0.1", "::", "fe80::1", "fd7a:115c:a1df::1", "fd7a:115c:a1e1::1", "fd00::1", "2606:4700:4700::1111", "user@192.168.1.1", "192.168.1.2/path"] {
            XCTAssertThrowsError(try RemoteBootstrap.decodePairingText(payload(host: address)), address)
        }
        for port in [0, -1, 65536] { XCTAssertThrowsError(try RemoteBootstrap.decodePairingText(payload(port: port))) }
        for session in ["", "has spaces", "bad|transcript", String(repeating: "a", count: 129)] {
            XCTAssertThrowsError(try RemoteBootstrap.decodePairingText(payload(session: session)))
        }
        for key in ["not-base64", Data(repeating: 0, count: 31).base64EncodedString()] {
            XCTAssertThrowsError(try RemoteBootstrap.decodePairingText(payload(key: key)))
        }
    }
}
