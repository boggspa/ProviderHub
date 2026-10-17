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

    private func targets(_ raw: Any) -> [CompanionMentionTarget] {
        (raw as! [[String: Any]]).map {
            CompanionMentionTarget(id: $0["id"] as! String, name: $0["name"] as! String, route: $0["route"] as! String,
                                   label: $0["name"] as! String, accentHex: "#705AFF")
        }
    }

    private func number(_ raw: Any) -> Int { (raw as? NSNumber)?.intValue ?? (raw as? Int) ?? -1 }
}
