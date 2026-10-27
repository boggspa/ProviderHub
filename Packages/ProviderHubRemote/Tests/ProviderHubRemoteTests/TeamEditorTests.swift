import Foundation
import XCTest
@testable import ProviderHubRemote

final class TeamEditorTests: XCTestCase {
    private let members: [RemoteValue] = [
        .object(["id": .string("sol"), "name": .string("Sol"), "choice": .string("codex/sol|model"), "effort": .string("max")]),
        .object(["id": .string("opus"), "name": .string("Opus"), "choice": .string("claude/opus|model"), "effort": .string("xhigh")])
    ]

    func testApplyAcceptsEveryHostProcessAllowanceIncludingSavedSixteen() {
        for processes in 1...16 {
            XCTAssertNil(CompanionTeamValidation.message(rows: members, context: "200000", processes: String(processes), minutes: "", tokens: ""),
                         "A saved Team with \(processes) processes must remain editable")
        }
        for processes in ["0", "17", "eight", ""] {
            XCTAssertEqual(CompanionTeamValidation.message(rows: members, context: "200000", processes: processes, minutes: "", tokens: ""),
                           "Concurrent processes must be between 1 and 16.")
        }
    }

    func testTwelveMembersAndNewContextPresetsAreAccepted() {
        let roster: [RemoteValue] = (1...12).map { number in
            .object(["name": .string("Member \(number)"), "choice": .string("codex/sol|model")])
        }
        for context in [120000, 325000, 650000] {
            XCTAssertTrue(CompanionTeamValidation.contextPresets.contains(context))
            XCTAssertNil(CompanionTeamValidation.message(rows: roster, context: String(context), processes: "8", minutes: "", tokens: ""))
        }
        XCTAssertEqual(CompanionTeamValidation.message(rows: roster + [members[0]], context: "200000", processes: "8", minutes: "", tokens: ""),
                       "A Team can have at most 12 members.")
    }

    func testInvalidFieldsExplainWhyApplyIsDisabled() {
        XCTAssertEqual(CompanionTeamValidation.message(rows: members, context: "15999", processes: "16", minutes: "", tokens: ""),
                       "Context tokens must be between 16,000 and 2,000,000.")
        XCTAssertEqual(CompanionTeamValidation.message(rows: members, context: "200000", processes: "16", minutes: "0", tokens: ""),
                       "Minutes must be between 1 and 10,080, or left empty.")
        XCTAssertEqual(CompanionTeamValidation.message(rows: members, context: "200000", processes: "16", minutes: "", tokens: "invalid"),
                       "Token limit must be between 1 and 2,000,000,000, or left empty.")
        var duplicate = members[1].object
        duplicate["name"] = .string(" SOL ")
        XCTAssertEqual(CompanionTeamValidation.message(rows: [members[0], .object(duplicate)], context: "200000", processes: "16", minutes: "", tokens: ""),
                       "Use a different name for each member.")
    }

    @MainActor
    func testMemberEditDuringRunPreservesSixteenProcessesAndMemberIdentity() throws {
        final class Transport: CompanionTransport {
            var sent: [RemoteCommand] = []
            func connect(text: String) {}
            func disconnect() {}
            func snapshot() {}
            func send(_ command: RemoteCommand) { sent.append(command) }
        }
        let suite = "companion-team-test-" + UUID().uuidString
        let preferences = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { preferences.removePersistentDomain(forName: suite) }
        let transport = Transport()
        let model = CompanionModel(preferences: preferences, transport: transport)
        let execution: RemoteValue = .object(["mode": .string("task"), "contextTokens": .number(200000), "processes": .number(16)])
        model.status = "Connected"
        model.receiveRemote(["method": "session.snapshot", "params": ["cursor": 1,
            "chats": [["id": "a", "title": "Team", "status": "Working", "busy": true, "pendingApproval": false]],
            "selected": ["id": "a", "entries": [], "busy": true],
            "controls": ["version": 1, "configuration": ["busy": true],
                         "team": ["enabled": true, "execution": try JSONSerialization.jsonObject(with: JSONEncoder().encode(execution))]]]])
        var changed = members[0].object
        changed["responsibility"] = .string("Review the remote controls")
        model.configureMembers([.object(changed), members[1]])
        let command = try XCTUnwrap(transport.sent.last)
        XCTAssertEqual(command.action, "configure_team")
        XCTAssertEqual(command.chat, "a")
        XCTAssertEqual(command.arguments?["execution"], execution)
        XCTAssertEqual(command.arguments?["members"]?.rows.first?["id"].text, "sol")
        XCTAssertEqual(command.arguments?["members"]?.rows.first?["responsibility"].text, "Review the remote controls")
    }
}
