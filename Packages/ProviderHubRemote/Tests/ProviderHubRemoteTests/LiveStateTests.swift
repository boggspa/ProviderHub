import Foundation
import XCTest
@testable import ProviderHubRemote

final class LiveStateTests: XCTestCase {
    private let patch: [String: Any] = ["id": "patch", "role": "tool", "tool": "apply_patch", "memberID": "m1", "memberName": "Sol", "contributionID": "current", "route": "codex/sol", "summary": "Edit source", "changedFiles": ["File.swift"], "detail": "--- a/File.swift\n+++ b/File.swift\n@@ -1 +1 @@\n-old\n+new"]

    func testCloseoutWaitsForRunEndAndSteeredUserClosesOnlyPriorTurn() {
        let edit = RemoteTranscriptItem(entry: RemoteEntry.decode(patch, fallback: "patch"))
        func changes(_ items: [RemoteTranscriptItem], open: Bool) -> Int {
            ChatTranscriptSegment.outline(items, open: open, isLive: { $0.entry.isLive }).filter { if case .changes = $0.content { return true }; return false }.count
        }
        XCTAssertEqual(changes([edit], open: true), 0, "A live Team must not display its closeout")
        XCTAssertEqual(changes([edit], open: false), 1)
        let steer = RemoteTranscriptItem(entry: RemoteEntry.decode(["id": "steer", "role": "user", "text": "Update the task"], fallback: "steer"))
        var nextPatch = patch; nextPatch["id"] = "patch2"
        let next = RemoteTranscriptItem(entry: RemoteEntry.decode(nextPatch, fallback: "patch2"))
        XCTAssertEqual(changes([edit, steer, next], open: true), 1, "Steering closes the previous boundary, not the ongoing work")
        XCTAssertEqual(changes([edit, steer, next], open: false), 2)
    }

    func testAuthoritativeSelectedStateWinsOverLateRecentListAndSurvivesReconnect() throws {
        var store = CompanionStore()
        try store.receive(["method": "session.snapshot", "params": ["cursor": 0, "chats": [], "selected": ["id": "a", "busy": true, "status": "Thinking…", "entries": [patch]]]])
        XCTAssertEqual(store.selectedBusy, true)
        try store.receive(["method": "session.delta", "params": ["cursor": 1, "event": ["event": "state", "chat": "other", "busy": false]]])
        XCTAssertEqual(store.selectedBusy, true)
        try store.receive(["method": "session.delta", "params": ["cursor": 2, "event": ["event": "chats", "chats": []]]])
        XCTAssertEqual(store.selectedBusy, true, "A recent-list update cannot end the selected run")
        try store.receive(["method": "session.delta", "params": ["cursor": 3, "event": ["event": "state", "chat": "a", "busy": true, "interrupting": true, "status": "Interrupting for your update…"]]])
        XCTAssertTrue(store.selectedInterrupting)
        XCTAssertEqual(store.selectedStatus, "Interrupting for your update…")
        try store.receive(["method": "session.snapshot", "params": ["cursor": 3, "chats": [], "selected": ["id": "a", "busy": true, "status": "Working…", "entries": [patch]]]])
        XCTAssertEqual(store.selectedBusy, true)
        try store.receive(["method": "session.delta", "params": ["cursor": 4, "event": ["event": "state", "chat": "a", "busy": false, "status": "Ready"]]])
        XCTAssertEqual(store.selectedBusy, false)
    }

    func testMembersUseCurrentContributionAndExactWorkingWording() {
        let members: [RemoteValue] = [
            .object(["id": .string("m1"), "name": .string("Sol"), "route": .string("codex/sol"), "status": .string("working"), "contributionID": .string("current")]),
            .object(["id": .string("m2"), "name": .string("Flash"), "route": .string("minimax/flash"), "status": .string("working"), "contributionID": .string("next")]),
            .object(["id": .string("m3"), "name": .string("Luna"), "route": .string("codex/luna"), "status": .string("waiting"), "waitReason": .string("Background process p1")])]
        var running = patch; running["detail"] = "Running…"; running["tool"] = "run_shell"; running["summary"] = "Run in /workspace:\nswift build"
        let current = RemoteEntry.decode(running, fallback: "patch")
        let old = RemoteEntry.decode(["id": "old", "role": "tool", "memberID": "m2", "contributionID": "prior", "tool": "run_shell", "summary": "Old command", "detail": "Running…"], fallback: "old")
        let rows = CompanionActivity.current(entries: [current, old], members: members, teamEnabled: true, busy: true)
        XCTAssertEqual(rows.map(\.name), ["Sol", "Flash", "Luna"])
        XCTAssertEqual(rows[0].description.verb, "Running")
        XCTAssertTrue(rows[0].description.subject.contains("swift build"))
        XCTAssertEqual(rows[1].description.verb, "Thinking…", "A previous contribution's tool is not current activity")
        XCTAssertEqual(rows[2].description.verb, "Waiting")
        XCTAssertFalse(rows[2].description.live)
        XCTAssertTrue(CompanionActivity.current(entries: [current], members: members, teamEnabled: true, busy: false).isEmpty)
        let stopping = CompanionActivity.current(entries: [current], members: members, teamEnabled: true, busy: true, transition: "Stopping…")
        XCTAssertTrue(stopping.allSatisfy { $0.description.verb == "Stopping…" && $0.description.subject.isEmpty })
    }

    func testAuthoritativeActivityCanDescribeAnEntryOutsideLoadedTail() {
        let activity: RemoteValue = .object(["rows": .array([.object(["id": .string("m"), "name": .string("Sol"), "route": .string("codex/sol"), "entryID": .string("earlier"), "kind": .string("tool"), "tool": .string("run_shell"), "summary": .string("Run in /workspace:\nswift build"), "status": .string("working"), "runningTool": .bool(true)])])])
        let rows = CompanionActivity.current(entries: [], members: [], teamEnabled: true, busy: true, authoritative: activity)
        XCTAssertEqual(rows.first?.entryID, "earlier")
        XCTAssertEqual(rows.first?.description.verb, "Running")
        XCTAssertEqual(rows.first?.description.code, true)
    }

    func testActivityBucketsDoNotTreatEveryToolAsACommand() {
        let members: [RemoteValue] = [
            .object(["id": .string("m1"), "name": .string("Sol"), "route": .string("codex/sol"), "status": .string("working"), "contributionID": .string("current")]),
            .object(["id": .string("m2"), "name": .string("Flash"), "route": .string("minimax/flash"), "status": .string("working"), "contributionID": .string("next")]),
            .object(["id": .string("m3"), "name": .string("Luna"), "route": .string("codex/luna"), "status": .string("waiting"), "waitReason": .string("Background process p1"), "contributionID": .string("wait")]),
            .object(["id": .string("m4"), "name": .string("Opus"), "route": .string("claude/opus"), "status": .string("working"), "contributionID": .string("think")]),
            .object(["id": .string("m5"), "name": .string("Ivy"), "route": .string("claude/ivy"), "status": .string("continuing"), "contributionID": .string("cont")]),
            .object(["id": .string("m6"), "name": .string("Nova"), "route": .string("codex/nova"), "status": .string("working"), "contributionID": .string("reply")]),
        ]
        let shell = RemoteEntry.decode(["id": "shell", "role": "tool", "memberID": "m1", "contributionID": "current", "tool": "run_shell", "summary": "Run in /workspace:\nswift build", "detail": "Running…"], fallback: "shell")
        let edit = RemoteEntry.decode(["id": "edit", "role": "tool", "memberID": "m2", "contributionID": "next", "tool": "apply_patch", "summary": "Apply patch:\nUpdate: main.swift", "detail": "Running…"], fallback: "edit")
        let reply = RemoteEntry.decode(["id": "reply", "role": "assistant", "memberID": "m6", "contributionID": "reply", "text": "Hello", "isLive": true], fallback: "reply")
        let rows = CompanionActivity.current(entries: [shell, edit, reply], members: members, teamEnabled: true, busy: true)
        let buckets = ChatActivityBuckets.group(rows.map {
            ChatActivityBuckets.Row(id: $0.id, name: $0.name, route: $0.route, verb: $0.description.verb, live: $0.description.live)
        })
        XCTAssertEqual(buckets.map(\.title), ["Running a command", "Editing", "Waiting", "Thinking", "Continuing", "Replying"])
        XCTAssertEqual(buckets[0].participants.map(\.name), ["Sol"])
        XCTAssertEqual(buckets[1].participants.map(\.name), ["Flash"])
        XCTAssertEqual(buckets[2].participants.map(\.name), ["Luna"])
        XCTAssertFalse(buckets[2].live)
        XCTAssertEqual(buckets[5].participants.map(\.id), ["m6"])
        let stopping = CompanionActivity.current(entries: [shell, edit, reply], members: members, teamEnabled: true, busy: true, transition: "Stopping…")
        let stopped = ChatActivityBuckets.group(stopping.map {
            ChatActivityBuckets.Row(id: $0.id, name: $0.name, route: $0.route, verb: $0.description.verb, live: $0.description.live)
        })
        XCTAssertEqual(stopped.map(\.title), ["Stopping"])
        XCTAssertEqual(stopped[0].participants.map(\.name), ["Sol", "Flash", "Luna", "Opus", "Ivy", "Nova"])
    }
}
