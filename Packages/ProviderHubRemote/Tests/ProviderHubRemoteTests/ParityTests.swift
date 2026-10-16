import Foundation
import XCTest
@testable import ProviderHubRemote

final class ParityTests: XCTestCase {
    func testHistoryPrependsWithoutDuplicatesAndSurvivesNewMessages() throws {
        var store = CompanionStore()
        try store.receive(["method": "session.snapshot", "params": ["cursor": 0, "chats": [], "selected": ["id": "a", "hasMore": true,
            "entries": [["id": "later", "role": "user", "text": "Later"]]]]])
        XCTAssertTrue(store.hasOlder)
        try store.receive(["method": "session.delta", "params": ["cursor": 1, "event": ["event": "history", "chat": "a", "before": "later", "hasMore": false,
            "entries": [["id": "early", "role": "user", "text": "Early"], ["id": "later", "role": "user", "text": "Duplicate"]]]]])
        XCTAssertEqual(store.visibleEntries.map(\.id), ["early", "later"])
        XCTAssertFalse(store.hasOlder)
        try store.receive(["method": "session.delta", "params": ["cursor": 2, "event": ["event": "history", "chat": "b", "before": "early", "hasMore": true,
            "entries": [["id": "wrong-chat", "role": "user", "text": "Wrong"]]]]])
        XCTAssertEqual(store.visibleEntries.map(\.id), ["early", "later"])
        try store.receive(["method": "session.snapshot", "params": ["cursor": 3, "chats": [], "selected": ["id": "a", "hasMore": true,
            "entries": [["id": "later", "role": "user", "text": "Later"]]]]])
        XCTAssertEqual(store.visibleEntries.map(\.id), ["early", "later"])
        XCTAssertFalse(store.hasOlder)
        try store.receive(["method": "session.delta", "params": ["cursor": 4, "event": ["event": "snapshot", "chat": "b", "entries": []]]])
        XCTAssertTrue(store.visibleEntries.isEmpty)
    }

    func testAttachmentMetadataHasNoDependencyOnMacPaths() {
        let row = RemoteEntry.decode(["id": "e", "role": "user", "attachments": [["id": "file", "name": "photo.jpg", "kind": "image", "size": 1234]]], fallback: "e")
        XCTAssertEqual(row.attachments.first?.name, "photo.jpg")
        XCTAssertEqual(row.attachments.first?.size, 1234)
    }

    func testAdvancingSnapshotKeepsReadHistoryOnlyAcrossKnownOverlap() throws {
        var store = CompanionStore()
        func snapshot(_ cursor: Int, _ ids: [String]) -> [String: Any] {
            ["method": "session.snapshot", "params": ["cursor": cursor, "chats": [], "selected": ["id": "a", "hasMore": true,
                "entries": ids.map { ["id": $0, "role": "user", "text": $0] }]]]
        }
        try store.receive(snapshot(0, ["b", "c"]))
        try store.receive(["method": "session.delta", "params": ["cursor": 1, "event": ["event": "history", "chat": "a", "before": "b", "hasMore": false,
            "entries": [["id": "a", "role": "user", "text": "a"]]]]])
        try store.receive(snapshot(2, ["c", "d"]))
        XCTAssertEqual(store.visibleEntries.map(\.id), ["a", "b", "c", "d"])
        try store.receive(snapshot(3, ["x", "y"]))
        XCTAssertEqual(store.visibleEntries.map(\.id), ["x", "y"])
        XCTAssertTrue(store.hasOlder)
    }

    @MainActor
    func testAttachmentUploadWaitsForChunkAcksAndClearsOnlyAfterAcceptedSend() async throws {
        final class Link: CompanionLink {
            var handle: ((RemoteCommand) -> Void)?
            func send(_ command: RemoteCommand) { handle?(command) }
        }
        let model = CompanionModel()
        let link = Link()
        model.link = link
        model.selectedChatID = "a"
        model.status = "Connected"
        let bytes = Data(repeating: 42, count: 50000)
        try model.addAttachment(name: "source.txt", data: bytes)
        model.draft = "Read source"
        var chunks: [Data] = []
        var actions: [String] = []
        link.handle = { command in
            guard let action = command.action else { return }
            actions.append(action)
            var result: [String: Any] = [:]
            if action == "remote_upload_begin" { result["offset"] = 0 }
            if action == "remote_upload_chunk" {
                let arguments = command.arguments ?? [:]
                let chunk = Data(base64Encoded: arguments["data"]?.text ?? "")!
                XCTAssertLessThanOrEqual(chunk.count, 18 * 1024)
                chunks.append(chunk)
                result["offset"] = (arguments["offset"]?.int ?? 0) + chunk.count
            }
            model.receiveRemote(["method": "session.ack", "params": ["requestId": command.requestId, "ok": true, "result": result]])
        }
        await model.sendWithAttachments()
        XCTAssertEqual(chunks.reduce(Data(), +), bytes)
        XCTAssertEqual(actions.last, "remote_send")
        XCTAssertTrue(model.attachmentDrafts["a"]?.isEmpty ?? false)
        XCTAssertEqual(model.draft, "")

        try model.addAttachment(name: "keep.txt", data: Data("keep".utf8))
        model.draft = "Keep this"
        link.handle = { command in model.receiveRemote(["method": "session.ack", "params": ["requestId": command.requestId, "ok": false, "error": "Host rejected upload"]]) }
        await model.sendWithAttachments()
        XCTAssertEqual(model.draft, "Keep this")
        XCTAssertEqual(model.attachmentDrafts["a"]?.count, 1)
        XCTAssertEqual(model.composeNotice, "Host rejected upload")
    }
}
