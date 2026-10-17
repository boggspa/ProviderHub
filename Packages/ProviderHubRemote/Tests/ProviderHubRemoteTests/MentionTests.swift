import Foundation
import XCTest
@testable import ProviderHubRemote

final class MentionTests: XCTestCase {
    func testSharedCasesMatchTheDesktopContract() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: root.appendingPathComponent("Source/test_chat_mentions_cases.json"))
        let fixture = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        let defaults = targets(fixture["members"]!)
        var cases = fixture["cases"] as! [[String: Any]]
        cases.append(["text": String(repeating: "@Sol ", count: 70) + "@Kimi",
                      "tags": (0..<64).map { ["s", $0 * 5, 4] } + [["k", 350, 5]]])
        for item in cases {
            let text = item["text"] as! String
            let members = (item["members"].map { targets($0) }) ?? defaults
            let want = (item["tags"] as! [[Any]]).map { (id: $0[0] as! String, start: number($0[1]), length: number($0[2])) }
            let got = CompanionMentions.resolve(text, members: members).map { (id: $0.target.id, start: $0.range.location, length: $0.range.length) }
            XCTAssertEqual(got.map(\.id), want.map(\.id), text)
            XCTAssertEqual(got.map(\.start), want.map(\.start), text)
            XCTAssertEqual(got.map(\.length), want.map(\.length), text)
        }
    }

    func testTypingAndCandidateOrder() {
        let members = [
            CompanionMentionTarget(id: "s", name: "Sol", route: "r", label: "Sol", accentHex: "#705AFF"),
            CompanionMentionTarget(id: "k", name: "Kimi", route: "r", label: "Kimi", accentHex: "#111111"),
            CompanionMentionTarget(id: "o", name: "Opus", route: "r", label: "Opus", accentHex: "#222222"),
        ]
        XCTAssertEqual(CompanionMentions.typing("@So", caret: 3)?.query, "So")
        XCTAssertEqual(CompanionMentions.typing("@So", caret: 3)?.start, 0)
        XCTAssertNil(CompanionMentions.typing("mail a@sol", caret: 10))
        XCTAssertEqual(CompanionMentions.candidates("k", members: members).map(\.id), ["k"])
        XCTAssertEqual(CompanionMentions.candidates("", members: members).map(\.id), ["s", "k", "o"])
        let dupes = members + [CompanionMentionTarget(id: "s2", name: "SOL", route: "r2", label: "SOL", accentHex: "#333")]
        XCTAssertFalse(CompanionMentions.resolve("@Sol go", members: dupes).contains { $0.target.id == "s" })
    }

    func testRecordedChipsTintOnlyWhereTheTextStillReadsTheName() {
        let entry = RemoteEntry.decode([
            "id": "u1", "role": "user", "text": "@Sol go",
            "mentions": [["id": "s", "name": "Sol", "route": "codex/x", "start": 0, "length": 4],
                          ["id": "no", "name": "Nope", "route": "r", "start": 2, "length": 5]],
        ], fallback: "legacy")
        XCTAssertEqual(entry.mentions.count, 2)
        let marked = CompanionMentions.marked(entry.text, mentions: entry.mentions, accent: { _ in .red }, size: 13)
        let tinted = marked.runs.filter { $0.foregroundColor != nil }.map { String(marked[$0.range].characters) }
        XCTAssertEqual(tinted, ["@Sol"])
    }

    /// Uploads await the Mac several times, long enough for the Team to change.
    /// The send must carry the chips resolved when Send was pressed, so a
    /// rename makes the Mac refuse a stale tag instead of the phone dropping
    /// it and widening the message to the whole Team.
    @MainActor
    func testAttachmentSendKeepsTheChipsAddressedBeforeUploading() async throws {
        final class Transport: CompanionTransport {
            var sent: [RemoteCommand] = []
            func connect(text: String) {}
            func disconnect() {}
            func snapshot() {}
            func send(_ command: RemoteCommand) { sent.append(command) }
        }
        let suite = "companion-mention-upload-test-" + UUID().uuidString
        let preferences = UserDefaults(suiteName: suite)!
        defer { preferences.removePersistentDomain(forName: suite) }
        let transport = Transport()
        let model = CompanionModel(preferences: preferences, transport: transport)
        model.status = "Connected"
        func team(_ first: String) -> [String: Any] {
            ["enabled": true, "members": [["id": "s", "name": first, "route": "codex/sol"],
                                          ["id": "k", "name": "Kimi", "route": "kimi/k2"]]]
        }
        model.receiveRemote(["method": "session.snapshot", "params": ["cursor": 1, "chats": [],
            "selected": ["id": "a", "entries": []], "controls": ["version": 1, "extensionVersion": 4, "team": team("Sol")]]])
        XCTAssertEqual(model.selectedChatID, "a")
        model.draft = "@Sol inspect the attachment"
        XCTAssertEqual(model.mentionChips(in: model.draft).map(\.id), ["s"])
        try model.addAttachment(name: "notes.txt", data: Data("hello".utf8))
        let sending = Task { @MainActor in await model.sendWithAttachments() }

        var answered: Set<String> = []
        func answer(_ action: String, _ result: [String: Any]) async throws -> RemoteCommand {
            var found: RemoteCommand?
            for _ in 0..<1000 where found == nil {
                found = transport.sent.first { $0.action == action && !answered.contains($0.requestId) }
                if found == nil { await Task.yield() }
            }
            let command = try XCTUnwrap(found, "The phone never sent " + action)
            answered.insert(command.requestId)
            model.receiveRemote(["method": "session.ack", "params": ["requestId": command.requestId, "ok": true, "result": result]])
            return command
        }

        _ = try await answer("remote_upload_begin", ["offset": 0])
        // Sol is renamed on the Mac while the attachment is still uploading.
        model.receiveRemote(["method": "session.delta", "params": ["cursor": 2,
            "event": ["event": "control", "chat": "a", "panel": "team", "value": team("Sun")]]])
        XCTAssertTrue(model.mentionChips(in: model.draft).isEmpty, "the rename did not reach the roster")
        _ = try await answer("remote_upload_chunk", ["offset": 5])
        _ = try await answer("remote_upload_finish", [:])
        let send = try await answer("remote_send", [:])
        await sending.value

        let chips = send.arguments?["mentions"]?.rows ?? []
        XCTAssertEqual(chips.map { $0["id"].text }, ["s"])
        XCTAssertEqual(chips.map { $0["name"].text }, ["Sol"])
        XCTAssertEqual(chips.first?["start"].int, 0)
        XCTAssertEqual(chips.first?["length"].int, 4)
        XCTAssertEqual(model.composeNotice, "")
    }

    private func targets(_ raw: Any) -> [CompanionMentionTarget] {
        (raw as! [[String: Any]]).map {
            CompanionMentionTarget(id: $0["id"] as! String, name: $0["name"] as! String, route: $0["route"] as! String,
                                   label: $0["name"] as! String, accentHex: "#705AFF")
        }
    }

    private func number(_ raw: Any) -> Int { (raw as? NSNumber)?.intValue ?? (raw as? Int) ?? -1 }
}
