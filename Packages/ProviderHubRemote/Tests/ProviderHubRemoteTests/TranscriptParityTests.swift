import Foundation
import XCTest
@testable import ProviderHubRemote

final class TranscriptParityTests: XCTestCase {
    private typealias Segment = ChatTranscriptSegment<RemoteTranscriptItem>

    private func entry(_ id: String, _ role: String = "tool", member: String = "sol", text: String = "",
                       tool: String = "run_shell", live: Bool = false, detail: String = "Done") -> RemoteEntry {
        RemoteEntry.decode(["id": id, "role": role, "text": text, "tool": role == "tool" ? tool : "",
            "detail": role == "tool" ? detail : "", "route": "codex/" + member,
            "memberID": member, "memberName": member, "contributionID": "current", "isLive": live], fallback: id)
    }

    private func activity(_ entry: RemoteEntry) -> CompanionActivity {
        CompanionActivity(id: entry.memberID, name: entry.memberName, route: entry.route, entryID: entry.id,
            description: .describe(status: "working", kind: entry.role, tool: entry.tool,
                summary: entry.summary, runningTool: entry.detail == "Running…", streaming: entry.isLive))
    }

    private func outline(_ entries: [RemoteEntry], open: Bool = true, activities: [CompanionActivity] = []) -> [Segment] {
        let ids = CompanionTranscriptState.liveEntryIDs(entries: entries, runIsOpen: open, activities: activities)
        return Segment.outline(entries.map(RemoteTranscriptItem.init), open: open, isLive: { ids.contains($0.id) })
    }

    private func folds(_ segments: [Segment]) -> [[String]] {
        segments.compactMap { if case .fold(let items) = $0.content { return items.map(\.id) }; return nil }
    }

    func testOldStreamingPlaceholdersDoNotSplitAnActiveMembersToolRun() {
        let reply = entry("reply", "assistant", text: "Restoring the existing controls.")
        let tools = [entry("edit", tool: "apply_patch"), entry("run1"), entry("run2", detail: "Running…")]
        let entries = [reply, tools[0], entry("gap1", "assistant", live: true), tools[1],
            entry("gap2", "assistant", live: true), tools[2]]
        let rows = outline(entries, activities: [activity(tools[2])])
        XCTAssertEqual(folds(rows), [["edit", "run1", "run2"]])
        XCTAssertEqual(rows.filter(\.isSpeaker).map(\.id), ["speaker-reply"])
        XCTAssertFalse(rows.contains { $0.id == "gap1" || $0.id == "gap2" })
        let live = CompanionTranscriptState.liveEntryIDs(entries: entries, runIsOpen: true, activities: [activity(tools[2])])
        XCTAssertEqual(live, ["run2"])
    }

    func testOnlyCurrentThinkingPlaceholderRemainsAndCompletedRunHasNone() {
        let entries = [entry("t1"), entry("old", "assistant", live: true), entry("t2"), entry("t3"),
            entry("current", "assistant", live: true)]
        let active = outline(entries, activities: [activity(entries.last!)])
        XCTAssertEqual(folds(active), [["t1", "t2", "t3"]])
        XCTAssertEqual(active.compactMap { row -> String? in
            guard case .entry(let item) = row.content, item.kind == "assistant" else { return nil }
            return item.id
        }, ["current"])
        XCTAssertEqual(folds(outline(entries, open: false)), [["t1", "t2", "t3"]])
        XCTAssertFalse(outline(entries, open: false).contains { $0.id == "old" || $0.id == "current" })
    }

    func testInterleavedMembersKeepDesktopSectionOrderAndSeparateFolds() {
        let sol = entry("sol-reply", "assistant", text: "Working on controls")
        let luna = entry("luna-reply", "assistant", member: "luna", text: "Checking the UI")
        let entries = [sol, entry("s1"), luna, entry("l1", member: "luna"),
            entry("gap-s", "assistant", live: true), entry("s2"), entry("l2", member: "luna"),
            entry("gap-l", "assistant", member: "luna", live: true), entry("s3"), entry("l3", member: "luna")]
        let activities = [activity(entries[8]), activity(entries[9])]
        let rows = outline(entries, activities: activities)
        XCTAssertEqual(folds(rows), [["s1", "s2", "s3"], ["l1", "l2", "l3"]])
        XCTAssertEqual(rows.filter(\.isSpeaker).map(\.id), ["speaker-sol-reply", "speaker-luna-reply"])
    }

    func testSteeringAndVisibleRepliesKeepDistinctFoldsWithStableIDsAsTheyGrow() {
        var entries = [entry("t1"), entry("gap", "assistant", live: true), entry("t2"), entry("t3")]
        let initial = outline(entries, activities: [activity(entries.last!)])
        let initialID = initial.first { if case .fold = $0.content { return true }; return false }?.id
        entries.append(entry("t4"))
        XCTAssertEqual(outline(entries, activities: [activity(entries.last!)]).first {
            if case .fold = $0.content { return true }; return false
        }?.id, initialID, "New events must update the existing disclosure, preserving user expansion")
        entries += [entry("steer", "user", member: "", text: "Also check the HUD"), entry("t5"), entry("t6"), entry("t7"),
            entry("reply", "assistant", text: "The checks passed"), entry("t8"), entry("t9"), entry("t10")]
        XCTAssertEqual(folds(outline(entries, activities: [activity(entries.last!)])),
            [["t1", "t2", "t3", "t4"], ["t5", "t6", "t7"], ["t8", "t9", "t10"]])
    }

    func testSoloOlderHostUsesOnlyItsNewestRowAndOutsideTailActivityDoesNotReviveHistory() {
        let solo = [entry("old", "assistant", member: "", live: true), entry("t1", member: ""),
            entry("t2", member: ""), entry("t3", member: "")]
        XCTAssertEqual(CompanionTranscriptState.liveEntryIDs(entries: solo, runIsOpen: true, activities: []), ["t3"])
        XCTAssertEqual(folds(outline(solo)), [["t1", "t2", "t3"]])
        let outside = activity(entry("outside-tail"))
        XCTAssertEqual(CompanionTranscriptState.liveEntryIDs(entries: solo, runIsOpen: true, activities: [outside]), ["outside-tail"])
        XCTAssertFalse(outline(solo, activities: [outside]).contains { $0.id == "old" })
        XCTAssertTrue(CompanionTranscriptState.liveEntryIDs(entries: solo, runIsOpen: false, activities: [outside]).isEmpty)
    }

    func testSharedFoldSummaryIncludesOrderedGlyphsFileCountsPatchTotalsAndFailures() {
        let patch = "--- a/Controls.swift\n+++ b/Controls.swift\n@@ -1 +1,2 @@\n-old\n+new\n+extra"
        var edit = entry("edit", tool: "apply_patch", detail: patch)
        edit.changedFiles = ["Controls.swift"]
        var second = entry("edit2", tool: "apply_patch", detail: patch)
        second.changedFiles = ["Controls.swift", "HUD.swift"]
        var failed = entry("shell", detail: "Running…")
        failed.summary = "Run in /workspace:\nswift test"
        failed.isError = true
        let rows = [edit, second, entry("read", tool: "read_file"), entry("search", tool: "web_search"), failed]
        let summary = ChatToolFoldSummary.collect(rows.map(RemoteTranscriptItem.init))
        XCTAssertEqual(summary.glyphs, ["apply_patch", "read_file", "web_search"].map { ChatToolDisplay.glyph($0) })
        XCTAssertEqual(summary.files, 2)
        XCTAssertEqual(summary.stats, ChatPatchStats(added: 4, removed: 2))
        XCTAssertTrue(summary.failed)
        XCTAssertTrue(summary.activity.contains("2 edits"))
        XCTAssertTrue(summary.latest.contains("Running"))
        XCTAssertTrue(summary.latest.contains("swift test"))
        let shellDiff = ChatToolFoldSummary.collect([RemoteTranscriptItem(entry: entry("shell-diff", detail: patch))])
        XCTAssertNil(shellDiff.stats, "Shell output resembling a diff is not a recorded patch")
    }
}
