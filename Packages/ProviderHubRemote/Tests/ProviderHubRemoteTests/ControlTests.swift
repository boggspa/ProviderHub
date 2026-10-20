import Foundation
import XCTest
import SwiftUI
@testable import ProviderHubRemote

final class ControlTests: XCTestCase {
    func testTurnSummariesRetainPriorTurnAndClearOnChatChange() throws {
        var store = CompanionStore()
        try store.receive(["method": "session.snapshot", "params": ["cursor": 0, "chats": [], "selected": ["id": "a", "entries": []]]])
        for (cursor, turn) in [(1, "turn1"), (2, "turn2")] {
            try store.receive(["method": "session.delta", "params": ["cursor": cursor,
                "event": ["event": "control", "chat": "a", "panel": "turnChanges",
                    "value": ["turnID": turn, "entryIDs": ["patch-" + turn], "files": [["path": turn + ".swift", "added": 1, "removed": 0]]]]]])
        }
        XCTAssertEqual(store.turnSummaries.map { $0["turnID"].text }, ["turn1", "turn2"])
        try store.receive(["method": "session.delta", "params": ["cursor": 3, "event": ["event": "snapshot", "chat": "b", "entries": []]]])
        XCTAssertTrue(store.turnSummaries.isEmpty)
    }
    @MainActor
    func testExtendedCapabilitiesSurviveSnapshotWithoutOptions() throws {
        let model = CompanionModel()
        model.receiveRemote(["method": "session.snapshot", "params": ["cursor": 1, "chats": [], "controls": ["version": 1, "extensionVersion": 3]]])
        XCTAssertTrue(model.extendedControlsAvailable)
    }
    /// A draft typed during a run steers it, as the Mac's composer does;
    /// a host that predates steering keeps the old send-between-turns rule.
    @MainActor
    func testDraftDuringRunSteersOnHostsThatAcceptUpdates() throws {
        final class Transport: CompanionTransport {
            var sent: [RemoteCommand] = []
            func connect(text: String) {}
            func disconnect() {}
            func snapshot() {}
            func send(_ command: RemoteCommand) { sent.append(command) }
        }
        let suite = "companion-steer-test-" + UUID().uuidString
        let preferences = UserDefaults(suiteName: suite)!
        defer { preferences.removePersistentDomain(forName: suite) }
        let transport = Transport()
        let model = CompanionModel(preferences: preferences, transport: transport)
        model.status = "Connected"
        func snapshot(extension version: Int, busy: Bool, interrupting: Bool = false) {
            model.receiveRemote(["method": "session.snapshot", "params": ["cursor": 1,
                "chats": [["id": "a", "title": "Notes", "status": busy ? "Working" : "Ready", "busy": busy, "pendingApproval": false]],
                "selected": ["id": "a", "entries": [], "busy": busy, "interrupting": interrupting],
                "controls": ["version": 1, "extensionVersion": version]]])
        }
        snapshot(extension: 4, busy: true)
        model.draft = "Use the other file"
        XCTAssertTrue(model.runIsOpen)
        XCTAssertFalse(model.canSteer, "a host without extension 5 cannot take an update")
        XCTAssertFalse(model.canSend)
        snapshot(extension: 5, busy: true, interrupting: true)
        XCTAssertFalse(model.canSteer, "the previous update is still interrupting the turn")
        snapshot(extension: 5, busy: true)
        XCTAssertTrue(model.canSteer)
        model.sendDraft()
        let steer = try XCTUnwrap(transport.sent.last)
        XCTAssertEqual(steer.method, "chat.steer")
        XCTAssertEqual(steer.text, "Use the other file")
        XCTAssertFalse(model.canSteer, "the same update is already on its way")
        model.receiveRemote(["method": "session.ack", "params": ["requestId": steer.requestId, "ok": true]])
        XCTAssertEqual(model.draft, "", "an accepted update leaves the composer")
        snapshot(extension: 5, busy: false)
        model.draft = "Next question"
        model.sendDraft()
        XCTAssertEqual(transport.sent.last?.method, "chat.send", "between turns a draft is an ordinary send")
    }
    func testPickerUsesConnectionBrandAndModelBrandIndependently() {
        let value: RemoteValue = .object(["id": .string("ollama/qwen|work"), "route": .string("ollama/qwen"), "label": .string("Qwen"),
            "accountLabel": .string("Work"), "context": .number(128000), "efforts": .array([.string("high"), .string("xhigh")]),
            "connectionPresentation": .object(["runtimeProvider": .string("ollama"), "displayProvider": .string("Local Ollama"), "accent": .string("#976C52")]),
            "presentation": .object(["displayProvider": .string("Qwen"), "accent": .string("#8C52EF")])])
        let choice = CompanionPickerChoice(row: value)
        XCTAssertEqual(choice.connectionID, "ollama")
        XCTAssertEqual(choice.connectionLabel, "Local Ollama")
        XCTAssertEqual(choice.presentation.provider, "Qwen")
        XCTAssertEqual(choice.efforts, ["high", "xhigh"])
        XCTAssertEqual(choice.context, 128000)
    }
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

    func testNoticesStayBoundedAndClearForTheNextAction() throws {
        var store = CompanionStore()
        for cursor in 1...25 {
            try store.receive(["method": "session.delta", "params": ["cursor": cursor, "event": ["event": "control", "panel": "browserEnabled", "value": true, "notice": "n\(cursor)"]]])
        }
        XCTAssertEqual(store.notices.count, 20)
        XCTAssertEqual(store.notices.first, "n6")
        XCTAssertEqual(store.notices.last, "n25")
        store.clearNotices()
        XCTAssertTrue(store.notices.isEmpty)
    }

    @MainActor
    func testTurnClockResumesFromTheOpeningMessageAndRestartsLive() throws {
        let model = CompanionModel()
        model.receiveRemote(["method": "session.snapshot", "params": ["cursor": 1, "chats": [], "selected": ["id": "a", "busy": true, "entries": [
            ["id": "u1", "role": "user", "text": "Go", "time": 1000.0], ["id": "a1", "role": "assistant", "text": "On it", "time": 1005.0]]]]])
        XCTAssertEqual(model.turnStartedAt, Date(timeIntervalSince1970: 1000))
        // A steer adds a user message without restarting the Mac's clock.
        model.receiveRemote(["method": "session.delta", "params": ["cursor": 2, "event": ["event": "entry", "chat": "a",
            "entry": ["id": "u2", "role": "user", "text": "Also this", "time": 1010.0]]]])
        XCTAssertEqual(model.turnStartedAt, Date(timeIntervalSince1970: 1000))
        model.receiveRemote(["method": "session.delta", "params": ["cursor": 3, "event": ["event": "state", "chat": "a", "busy": false, "status": "Ready"]]])
        XCTAssertNil(model.turnStartedAt)
        let before = Date()
        model.receiveRemote(["method": "session.delta", "params": ["cursor": 4, "event": ["event": "state", "chat": "a", "busy": true, "status": "Thinking…"]]])
        XCTAssertGreaterThanOrEqual(model.turnStartedAt ?? .distantPast, before)
        XCTAssertEqual(CompanionTurnTime.timecode(65), "01:05")
        XCTAssertEqual(CompanionTurnTime.timecode(3725), "01:02:05")
    }

    @MainActor
    func testHeaderAndFooterReadOneApprovalModeAndOnlyAMissingConnection() throws {
        let model = CompanionModel()
        model.receiveRemote(["method": "session.snapshot", "params": ["cursor": 1,
            "chats": [["id": "a", "title": "A", "status": "Ready", "busy": false, "pendingApproval": false, "approvalMode": "manual"]],
            "selected": ["id": "a", "entries": []], "controls": ["version": 1, "configuration": ["approvalMode": "yolo"]]]])
        XCTAssertEqual(model.approvalMode, "yolo")
        model.status = "Connected"
        XCTAssertNil(model.connectionPhase)
        model.status = "Not paired"
        XCTAssertNil(model.connectionPhase)
        model.status = "Disconnected. Reconnecting…"
        XCTAssertEqual(model.connectionPhase, "Connecting")
        model.status = "Disconnected"
        XCTAssertEqual(model.connectionPhase, "Offline")
        model.status = "Compare code and approve on your Mac"
        XCTAssertEqual(model.connectionPhase, "Connecting")
    }

    @MainActor
    func testATappedChatUsesItsOwnSummaryUntilTheMacBindsIt() throws {
        let model = CompanionModel()
        model.receiveRemote(["method": "session.snapshot", "params": ["cursor": 1,
            "chats": [["id": "a", "title": "A", "status": "Working…", "busy": true, "pendingApproval": false, "approvalMode": "yolo"],
                      ["id": "b", "title": "B", "status": "Ready", "busy": false, "pendingApproval": false, "approvalMode": "manual"]],
            "selected": ["id": "a", "busy": true, "entries": []],
            "controls": ["version": 1, "configuration": ["approvalMode": "yolo", "busy": true]]]])
        XCTAssertTrue(model.runIsOpen)
        XCTAssertEqual(model.approvalMode, "yolo")
        model.select("b")
        // The Mac has not answered yet: chat a's run and YOLO must not leak into b.
        XCTAssertFalse(model.runIsOpen)
        XCTAssertEqual(model.approvalMode, "manual")
        XCTAssertFalse(model.configurable)
    }

    func testEffortTitlesMatchOnBothShells() {
        XCTAssertEqual(ChatSharedEffort.title("xhigh"), "Extra high")
        XCTAssertEqual(ChatSharedEffort.title("high"), "High")
        XCTAssertEqual(ChatSharedEffort.title(""), "Default")
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
