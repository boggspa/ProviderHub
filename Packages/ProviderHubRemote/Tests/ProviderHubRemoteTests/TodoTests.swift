import Foundation
import XCTest
@testable import ProviderHubRemote

final class TodoTests: XCTestCase {
    private func panel(_ value: Any) throws -> [String: Any] {
        ["method": "session.delta", "params": ["cursor": 1, "event": ["event": "control", "chat": "a", "panel": "todos", "value": value]]]
    }

    func testTeamPanelKeepsRosterOrderSharedListAndTheHostsCounts() throws {
        var store = CompanionStore()
        try store.receive(["method": "session.snapshot", "params": ["cursor": 0, "chats": [], "selected": ["id": "a", "entries": []]]])
        try store.receive(panel(["team": true, "lanes": [
            ["id": "m1", "name": "Sol", "route": "claude/sonnet", "done": 3, "total": 7, "items": [
                ["id": "read", "content": "Read the config", "status": "completed"],
                ["id": "parser", "content": "Write the parser", "status": "in_progress"],
                ["id": "odd", "content": "Unknown state", "status": "blocked"]]],
            ["id": "m2", "name": "Luna", "route": "grok/grok-4.7", "done": 0, "total": 0, "items": []]],
            "shared": ["id": "shared", "name": "Shared", "done": 1, "total": 2, "revision": 4, "items": [
                ["id": "api", "content": "Wire the API", "status": "in_progress", "ownerName": "Luna"],
                ["id": "docs", "content": "Docs", "status": "completed", "ownerName": "Sol"]]],
            "newerHostKey": ["ignored": true]]))
        let lists = try XCTUnwrap(CompanionTodoLists(store.controls["todos"]))
        XCTAssertTrue(lists.team)
        XCTAssertEqual(lists.lanes.map(\.name), ["Sol", "Luna"])
        // A long list may arrive shortened, so progress is the host's count.
        XCTAssertEqual(lists.lanes[0].done, 3); XCTAssertEqual(lists.lanes[0].total, 7)
        XCTAssertEqual(lists.lanes[0].items.map(\.status), [.completed, .inProgress], "an unknown status is dropped, not guessed")
        XCTAssertEqual(lists.shared?.items.first?.owner, "Luna")
        XCTAssertEqual(lists.all.reduce(0) { $0 + $1.total }, 9)
        XCTAssertFalse(lists.isEmpty)
    }

    func testOlderHostsAndClearedChatsShowNoSection() throws {
        XCTAssertNil(CompanionTodoLists(nil), "a host without to-dos never sends the panel")
        XCTAssertNil(CompanionTodoLists(.null), "a chat without lists clears the panel")
        let solo = try XCTUnwrap(CompanionTodoLists(.object(["team": .bool(false), "shared": .null, "lanes": .array([
            .object(["id": .string("main"), "name": .string(""), "route": .string("ollama/test"), "items": .array([])])])])))
        XCTAssertNil(solo.shared)
        XCTAssertTrue(solo.isEmpty)
        XCTAssertEqual(solo.lanes.first?.total, 0)
    }

    func testToolResultParsesAsAChecklistAndTheRowSaysWhatChanged() {
        let detail = """
        Saved the shared Team to-dos (revision 2; 1/3 completed).
        - [completed by Sol] read: Read the config (v2)
        - [in_progress by Luna] parser: Write the parser
        - [pending] tests: Add tests
        """
        let items = ChatTodoChecklist.parse(detail)
        XCTAssertEqual(items?.map(\.id), ["read", "parser", "tests"])
        XCTAssertEqual(items?.first?.content, "Read the config (v2)", "parentheses in content are not an owner")
        XCTAssertEqual(items?.map(\.owner), ["Sol", "Luna", ""])
        XCTAssertEqual(ChatTodoChecklist.progress(items ?? []).done, 1)
        XCTAssertNil(ChatTodoChecklist.parse("Cleared your to-dos."))
        XCTAssertNil(ChatTodoChecklist.parse("{\"todos\": []}"))

        let own = ChatToolDisplay.describe(tool: "todo_write", summary: "To-dos: 1/3 done\nWrite the parser")
        XCTAssertEqual(own, ChatToolDisplay(verb: "Updated to-dos", subject: "1/3 done", context: "Write the parser"))
        let shared = ChatToolDisplay.describe(tool: "todo_write", summary: "Shared to-dos: 0/2 done", live: false)
        XCTAssertEqual(shared.verb, "Updated shared to-dos"); XCTAssertEqual(shared.subject, "0/2 done")
        XCTAssertEqual(ChatToolDisplay.describe(tool: "todo_write", summary: "Update to-dos", live: true).verb, "Updating to-dos")
        XCTAssertEqual(ChatToolDisplay.describe(tool: "todo_write", summary: "todo_write").subject, "", "a failed call shows no progress")
        XCTAssertEqual(ChatToolDisplay.glyph("todo_write"), "todo")
        XCTAssertTrue(ChatToolGlyph.has(ChatToolDisplay.glyph("todo_read")))
        XCTAssertEqual(ChatToolDisplay.activity(["todo_write", "todo_write", "read_file"]), "2 to-do updates, 1 read")
    }
}
