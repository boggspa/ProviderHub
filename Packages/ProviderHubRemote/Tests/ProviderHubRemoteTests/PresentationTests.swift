import Foundation
import XCTest
@testable import ProviderHubRemote

final class PresentationTests: XCTestCase {
    private func snapshot(_ entries: [[String: Any]] = [], live: String = "", liveID: String? = nil) -> [String: Any] {
        ["method":"session.snapshot", "params":["cursor":0, "chats":[
            ["id":"a", "title":"A", "status":"ready", "busy":false, "pendingApproval":false,
             "modelLabel":"Sol", "providerLabel":"Codex", "accent":"#705AFF", "approvalMode":"manual"]],
            "selected":["id":"a", "entries":entries, "live":live, "liveID":liveID as Any]]]
    }
    private func delta(_ cursor: Int, _ event: [String: Any]) -> [String: Any] {
        ["method":"session.delta", "params":["cursor":cursor, "event":event]]
    }
    func testStructuredRowsAndToolUpdatesKeepIdentity() throws {
        var store = CompanionStore()
        try store.receive(snapshot([["id":"u1", "role":"user", "text":"Keep my words"],
                                    ["id":"t1", "role":"tool", "text":"", "summary":"Run tests", "detail":"Running…", "tool":"run_shell"]]))
        XCTAssertEqual(store.chats.first?.modelLabel, "Sol")
        XCTAssertEqual(store.chats.first?.accent, "#705AFF")
        XCTAssertEqual(store.entries.map(\.role), ["user", "tool"])
        try store.receive(delta(1, ["event":"entry", "chat":"a", "entry":["id":"t1", "role":"tool", "text":"", "summary":"Run tests", "detail":"Tests passed", "tool":"run_shell"]]))
        XCTAssertEqual(store.entries.count, 2)
        XCTAssertEqual(store.entries.last?.detail, "Tests passed")
        XCTAssertEqual(store.entries.last?.id, "t1")
        XCTAssertFalse(store.entries.last?.isLive ?? true)
    }
    func testStreamingOffsetsSettleWithoutDuplicateReplies() throws {
        var store = CompanionStore()
        try store.receive(snapshot([["id":"a1", "role":"assistant", "text":""]]))
        try store.receive(delta(1, ["event":"delta", "chat":"a", "id":"a1", "offset":0, "text":"Hello ☀"] ))
        try store.receive(delta(2, ["event":"delta", "chat":"a", "id":"a1", "offset":0, "text":"Hello ☀"] ))
        XCTAssertEqual(store.entries.first?.text, "Hello ☀")
        XCTAssertTrue(store.entries.first?.isLive ?? false)
        try store.receive(delta(3, ["event":"entry", "chat":"a", "entry":["id":"a1", "role":"assistant", "text":"Hello ☀ final"]]))
        XCTAssertEqual(store.entries.count, 1)
        XCTAssertEqual(store.entries.first?.text, "Hello ☀ final")
        XCTAssertFalse(store.entries.first?.isLive ?? true)
        try store.receive(delta(4, ["event":"entry", "chat":"b", "entry":["id":"b1", "role":"user", "text":"Another chat"]]))
        XCTAssertEqual(store.entries.count, 1)
    }
    func testMidstreamReconnectAndLegacyEntries() throws {
        var store = CompanionStore()
        try store.receive(snapshot([["id":"a1", "role":"assistant", "text":""]], live:"Hello", liveID:"a1"))
        try store.receive(delta(1, ["event":"delta", "chat":"a", "id":"a1", "offset":5, "text":" again"]))
        XCTAssertFalse(store.needsSnapshot)
        XCTAssertEqual(store.entries.count, 1)
        XCTAssertEqual(store.entries.first?.text, "Hello again")
        try store.receive(delta(2, ["event":"delta", "chat":"a", "id":"a1", "offset":100, "text":"gap"]))
        XCTAssertTrue(store.needsSnapshot)
        try store.receive(snapshot([["role":"user", "text":"Legacy one"], ["role":"assistant", "text":"Legacy two"]]))
        XCTAssertEqual(Set(store.entries.map(\.id)).count, 2)
    }
    func testEntryBudgetAndProviderlessHistory() throws {
        var store = CompanionStore()
        try store.receive(snapshot((0..<35).map { ["id":"r\($0)", "role":"user", "text":"Row \($0)"] }))
        XCTAssertEqual(store.entries.count, 30)
        XCTAssertEqual(store.entries.first?.id, "r5")
        let legacy = try JSONDecoder().decode(RemoteChat.self, from: Data(#"{"id":"a","title":"A","status":"ready","busy":false,"pendingApproval":false}"#.utf8))
        XCTAssertEqual(legacy.displayModel, "Assistant")
        XCTAssertEqual(legacy.approvalLabel, "Manual")
    }

    func testBoundedUnicodeStreamDoesNotRequestSnapshotsForever() throws {
        var store = CompanionStore()
        let text = String(repeating: "😀", count: 8000)
        try store.receive(snapshot([["id":"a1", "role":"assistant", "text":text, "isLive":true]]))
        try store.receive(delta(1, ["event":"delta", "chat":"a", "id":"a1", "offset":8000, "text":""]))
        XCTAssertFalse(store.needsSnapshot)
        XCTAssertEqual(store.entries.first?.text, text)
        XCTAssertTrue(store.entries.first?.isLive ?? false)
        try store.receive(delta(2, ["event":"entry", "chat":"a", "entry":["id":"a1", "role":"assistant", "text":text]]))
        XCTAssertFalse(store.entries.first?.isLive ?? true)
    }
}
