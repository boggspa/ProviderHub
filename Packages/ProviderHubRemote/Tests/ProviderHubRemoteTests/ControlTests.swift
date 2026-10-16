import Foundation
import XCTest
import SwiftUI
@testable import ProviderHubRemote

final class ControlTests: XCTestCase {
    func testDiffKeepsCopyableTextAndUsesDistinctAdditionRemovalColors() {
        let text = "@@ -1 +1 @@\n-old line\n+new line\n context"
        let rendered = ChatSharedPatchText(text: text, onDark: true).attributed
        XCTAssertEqual(String(rendered.characters), text)
        let colors = rendered.runs.compactMap { $0.foregroundColor }
        XCTAssertTrue(colors.contains(ChatSharedPatchText.added))
        XCTAssertTrue(colors.contains(ChatSharedPatchText.removed))
        let plain = ChatSharedPatchText(text: "-ordinary prose", onDark: false).attributed
        XCTAssertFalse(plain.runs.contains { $0.foregroundColor == ChatSharedPatchText.removed })
    }
    func testBundledBrandingResolvesTheDesktopLogoVariants() throws {
        let sol = CompanionBranding.presentation(route: "codex/gpt-6.1-sol", label: "Sol")
        XCTAssertTrue(sol.logo["light"].text.contains("codex-cloud"))
        XCTAssertEqual(sol.logo["template"], .bool(true))
        let claude = CompanionBranding.presentation(route: "claude/opus", label: "Opus")
        XCTAssertTrue(claude.logo["light"].text.contains("claude-spark"))
        let qwen = CompanionBranding.presentation(route: "qwen-token-plan/qwen", label: "Qwen")
        XCTAssertNotNil(qwen.logo["leadingMarkAspectRatio"].number)
    }

    func testTranscriptUsesTheSharedDesktopSpeakerGrouping() {
        let first = RemoteEntry.decode(["id": "a", "role": "assistant", "memberID": "m1", "memberName": "Sol", "route": "codex/sol", "text": "One", "time": 1000.0], fallback: "a")
        let second = RemoteEntry.decode(["id": "b", "role": "tool", "memberID": "m1", "memberName": "Sol", "route": "codex/sol", "tool": "read_file", "summary": "Read file"], fallback: "b")
        let third = RemoteEntry.decode(["id": "c", "role": "assistant", "memberID": "m2", "memberName": "Opus", "route": "claude/opus", "text": "Two"], fallback: "c")
        let outline = ChatTranscriptSegment.outline([first, second, third].map(RemoteTranscriptItem.init), isLive: { $0.entry.isLive })
        XCTAssertEqual(outline.filter(\.isSpeaker).count, 2)
        XCTAssertEqual(first.time, 1000)
        XCTAssertEqual(first.memberID, "m1")
    }
    func testControlsRestoreAndWrongChatEventsCannotReplaceTeam() throws {
        var store = CompanionStore()
        try store.receive(["method": "session.snapshot", "params": ["cursor": 0, "chats": [], "selected": ["id": "a", "entries": []],
            "controls": ["version": 1, "team": ["members": [["id": "m", "name": "Sol"]]]]]])
        XCTAssertEqual(store.controls["team"]?["members"].rows.first?["name"].text, "Sol")
        try store.receive(["method": "session.delta", "params": ["cursor": 1, "event": ["event": "control", "chat": "b", "panel": "team", "value": ["members": []]]]])
        XCTAssertEqual(store.controls["team"]?["members"].rows.count, 1)
        try store.receive(["method": "session.delta", "params": ["cursor": 2, "event": ["event": "snapshot", "chat": "b", "entries": []]]])
        XCTAssertNil(store.controls["team"])
        XCTAssertEqual(store.controls["version"]?.int, 1)
    }

    func testScalarControlValuesAndNoticesDecode() throws {
        var store = CompanionStore()
        try store.receive(["method": "session.delta", "params": ["cursor": 1, "event": ["event": "control", "panel": "browserEnabled", "value": true, "notice": "Connected"]]])
        XCTAssertEqual(store.controls["browserEnabled"], .bool(true))
        XCTAssertEqual(store.notices.last, "Connected")
    }

    func testControlCommandRoundTripKeepsExactFields() throws {
        var command = RemoteCommand(method: "chat.control", requestId: "r", chat: "a")
        command.action = "configure_team"
        command.arguments = ["enabled": .bool(true), "members": .array([.object(["choice": .string("route|id"), "name": .string("Sol")])])]
        let decoded = try JSONDecoder().decode(RemoteCommand.self, from: JSONEncoder().encode(command))
        XCTAssertEqual(decoded, command)
        XCTAssertEqual(decoded.arguments?["members"]?.rows.first?["choice"].text, "route|id")
    }
}
