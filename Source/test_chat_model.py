"""Run the actual Swift ChatModel against a fake JSONL worker on macOS."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "Swift/AppKit state test needs macOS")
class ChatModelStateTests(unittest.TestCase):
    def test_drafts_acknowledgements_approval_and_split_transport(self):
        stubs = r'''
import AppKit
import SwiftUI
struct ProviderPresentation: Decodable { var accent: String; var runtimeProvider: String; var displayProvider: String; var color: Color { .blue } }
enum HubTheme {
    enum Semantic { static let ink = Color.primary; static let secondaryInk = Color.secondary; static let selection = Color.gray }
    enum Typography { static let detail = Font.system(size: 11) }
    enum Radius { static let row: CGFloat = 6 }
}
struct ProviderMark: View { var presentation: ProviderPresentation; var size: CGFloat; var body: some View { Text("provider") } }
struct ChatSettingsMenu: View { var model: ChatModel; var body: some View { Text("settings") } }
@MainActor final class BridgeModel {
    var python: String? = nil
    var helper = URL(fileURLWithPath: "/unused/gateway.py")
    var workerEnvironment: [String: String] = [:]
    func startGateway() async throws {}
}
'''
        cases = r'''
import AppKit
import SwiftUI
@main struct Cases {
    @MainActor static func main() throws {
        var commands: [[String: Any]] = []
        var clock: TimeInterval = 100
        var writable = true
        let model = ChatModel(sendCommand: { commands.append($0); return writable }, uptime: { clock })
        func send(_ event: [String: Any]) throws {
            model.consume(try JSONSerialization.data(withJSONObject: event) + Data([10]))
        }
        func check(_ yes: Bool, _ text: String) { if !yes { fatalError(text) } }
        let route: [String: Any] = ["id":"ollama/test|", "route":"ollama/test", "label":"Test", "provider":"Ollama",
            "account":"", "accountLabel":"Default", "scope":"scope", "efforts":["low","high"], "context":100000, "supportsTools":true]
        func summary(_ id: String) -> [String: Any] {
            ["id":id, "title":id, "updated":"today", "route":"ollama/test", "account":"", "workspace":"/tmp", "effort":""]
        }
        try send(["event":"catalogue", "models":[route], "folders":["/tmp"]])
        try send(["event":"chats", "chats":[summary("A"),summary("B")]])
        try send(["event":"ready"])
        try send(["event":"selected", "id":"A", "entries":[]])
        check(model.approvalMode == "manual", "old summaries must default to Manual")
        model.setApprovalMode("accept_edits")
        check(commands.last?["approvalMode"] as? String == "accept_edits", "permission selection not sent to worker")
        model.draft = "A's unsent draft"
        model.select("B")
        check(model.selectedID == "A" && model.draft == "A's unsent draft", "selection must wait for worker acknowledgement")
        try send(["event":"selected", "id":"B", "entries":[]])
        model.draft = "B's unsent draft"
        model.select("A")
        try send(["event":"selected", "id":"A", "entries":[]])
        check(model.draft == "A's unsent draft", "A draft lost across selection")
        model.select("B"); try send(["event":"selected", "id":"B", "entries":[]])
        check(model.draft == "B's unsent draft", "B draft lost across selection")
        model.send()
        check(model.busy && model.draft.isEmpty, "send did not begin")
        try send(["event":"error", "message":"rejected before acceptance"])
        check(model.draft == "B's unsent draft" && !model.busy, "rejected send lost submitted text")
        model.send()
        let accepted: [String: Any] = ["id":"user-1", "kind":"user", "text":"B's unsent draft", "route":"ollama/test", "isError":false, "changedFiles":[]]
        try send(["event":"entry", "chat":"B", "entry":accepted])
        try send(["event":"error", "message":"provider failed after acceptance"])
        check(model.draft.isEmpty && model.entries.count == 1, "accepted send should not reappear as duplicate draft")
        model.draft = String(repeating:"x", count:100001); model.send()
        try send(["event":"error", "message":"message too long"])
        check(model.draft.count == 100001, "large rejected paste lost")
        model.draft = ""
        try send(["event":"approval", "approval":["id":"approve-1", "summary":"Run echo ok", "workspace":"/tmp"]])
        model.decideApproval(allow:false)
        check(commands.last?["allow"] as? Bool == false && model.approval == nil, "approval response did not target pending request")
        let assistant: [String: Any] = ["id":"reply-1", "kind":"assistant", "text":"", "route":"ollama/test", "isError":false, "changedFiles":[]]
        try send(["event":"entry", "chat":"B", "entry":assistant])
        let data = try JSONSerialization.data(withJSONObject:["event":"delta", "chat":"B", "id":"reply-1", "text":"hello ☀︎"]) + Data([10])
        model.consume(data.prefix(17)); model.consume(data.dropFirst(17))
        check(model.entries.last?.text == "hello ☀︎", "fragmented worker output lost text")
        try send(["event":"delta", "chat":"A", "id":"reply-1", "text":"wrong chat"])
        check(model.entries.last?.text == "hello ☀︎", "cross-chat delta contaminated visible transcript")
        let attachmentURL = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".txt")
        try Data("attachment text".utf8).write(to: attachmentURL)
        defer { try? FileManager.default.removeItem(at: attachmentURL) }
        model.addAttachments([attachmentURL])
        check(model.attachments.count == 1 && model.canSend, "attachment-only message not sendable")
        model.send()
        check(model.attachments.isEmpty && (commands.last?["attachments"] as? [[String:Any]])?.count == 1, "attachments not sent")
        let commandCount = commands.count
        model.draft = "do not queue this"; model.send()
        check(commands.count == commandCount, "busy chat queued a message")
        try send(["event":"error", "message":"rejected attachment"])
        check(model.attachments.count == 1, "rejected attachment lost")
        model.select("A"); try send(["event":"selected", "id":"A", "entries":[]])
        check(model.attachments.isEmpty, "attachment leaked into other chat")
        model.select("B"); try send(["event":"selected", "id":"B", "entries":[]])
        check(model.attachments.count == 1, "attachment draft lost across chat selection")
        model.removeAttachment(model.attachments[0].id)
        check(model.attachments.isEmpty, "attachment removal failed")
        try send(["event":"state", "busy":true, "interrupting":false, "status":"Thinking…"])
        try send(["event":"approval", "approval":["id":"old-approval", "summary":"Pending patch", "workspace":"/tmp"]])
        model.draft = "Direct update"
        check(model.canInterrupt, "running turn should accept a direct interruption")
        model.send()
        check(commands.last?["command"] as? String == "steer" && model.interrupting, "update was not an interruption")
        check(model.approval != nil, "approval must remain until the worker accepts interruption")
        try send(["event":"state", "busy":true, "interrupting":true, "status":"Interrupting for your update…"])
        check(model.approval == nil, "stale approval remained visible after accepted interruption")
        model.draft = "Next draft"; let beforeSecond = commands.count; model.send()
        check(commands.count == beforeSecond && model.draft == "Next draft", "second update was queued")
        try send(["event":"rejected", "message":"still interrupting", "busy":true, "interrupting":true])
        check(model.draft.contains("Direct update") && model.draft.contains("Next draft"), "rejected update lost input")
        check(model.busy && model.interrupting, "rejection erased active run state")
        try send(["event":"git_status", "chat":"B", "status":["files":3,"added":12,"deleted":4,"ahead":2,"behind":1,"branch":"main","upstream":"origin/main"]])
        check(model.gitStatus?.files == 3, "git status not decoded")
        try send(["event":"git_status", "chat":"A", "status":NSNull()])
        check(model.gitStatus?.files == 3, "stale git status contaminated selected chat")
        try send(["event":"git_status", "chat":"B", "status":NSNull()])
        check(model.gitStatus == nil, "non-repository result should clear the indicator")
        try send(["event":"state", "busy":false, "status":"Ready"])
        check(model.turnStartedAt == nil && model.turnTimecode == "00:00", "idle clock was not reset")
        clock = 600; model.draft = "Clock run"; model.send()
        check(model.turnStartedAt == 600 && model.turnTimecode == "00:00", "send did not start the monotonic clock")
        try send(["event":"entry", "chat":"B", "entry":["id":"clock-user", "kind":"user", "text":"Clock run", "route":"ollama/test", "isError":false, "changedFiles":[]]])
        clock = 670
        try send(["event":"state", "busy":true, "status":"Working"])
        check(model.turnTimecode == "01:10", "stream state reset turn time")
        try send(["event":"approval", "approval":["id":"clock-approval", "summary":"Run echo ok", "workspace":"/tmp"]])
        clock = 675; model.draft = "Clock steer"; model.send()
        check(model.turnStartedAt == 600 && model.turnTimecode == "01:15", "steer reset turn time")
        try send(["event":"state", "busy":true, "interrupting":true, "status":"Interrupting"])
        try send(["event":"entry", "chat":"B", "entry":["id":"clock-steer", "kind":"user", "text":"Clock steer", "route":"ollama/test", "isError":false, "changedFiles":[]]])
        clock = 688
        try send(["event":"state", "busy":true, "interrupting":false, "status":"Working"])
        check(model.turnTimecode == "01:28", "provider restart reset turn time")
        clock = 715
        try send(["event":"rejected", "busy":true, "interrupting":false, "message":"Update rejected"])
        check(model.turnTimecode == "01:55", "rejected update reset active clock")
        clock = 4265
        check(model.turnTimecode == "01:01:05", "hour-long turn did not format correctly")
        model.stop()
        check(model.turnStartedAt == 600, "stop reset before worker acknowledged completion")
        try send(["event":"state", "busy":false, "status":"Stopped"])
        check(model.turnStartedAt == nil && model.turnTimecode == "00:00", "completed stop left a ticking clock")
        clock = 5000; model.retry()
        check(model.turnStartedAt == 5000, "retry did not begin a new clock")
        clock = 5010
        try send(["event":"error", "message":"Provider failed"])
        check(model.turnStartedAt == nil && model.turnTimecode == "00:00", "failed turn left a ticking clock")
        clock = 7000; model.draft = "Completion"; model.send()
        try send(["event":"state", "busy":false, "status":"Ready"])
        check(model.turnTimecode == "00:00", "successful completion did not reset clock")
        writable = false; model.retry()
        check(!model.busy && model.turnStartedAt == nil, "failed retry write left a ticking clock")
        model.draft = "Failed write"; model.send()
        check(!model.busy && model.turnStartedAt == nil, "failed send write left a ticking clock")
        writable = true
        model.newChat(in: "/tmp/another-project")
        check(commands.last?["workspace"] as? String == "/tmp/another-project", "workspace header created in the wrong folder")
        try send(["event":"state", "busy":true, "status":"Working"])
        let beforeBusyNew = commands.count; model.newChat(in: "/tmp/another-project")
        check(commands.count == beforeBusyNew, "workspace new-chat ignored busy guard")
        try send(["event":"state", "busy":false, "status":"Ready"])
        try send(["event":"chats", "chats":[]])
        try send(["event":"selected", "id":NSNull(), "entries":[]])
        check(model.selectedID == nil && model.entries.isEmpty, "last-chat deletion left a stale selection")
        model.newChat()
        check(commands.last?["workspace"] as? String == "/tmp", "empty sidebar lost recent workspace")
        let groupedJSON: [[String: Any]] = [
            ["id":"older", "title":"Older", "updated":"2026-10-08T10:00:00Z", "route":"ollama/test", "account":"", "workspace":"/tmp/one/project", "effort":""],
            ["id":"newer", "title":"Newer", "updated":"2026-10-08T12:00:00Z", "route":"ollama/test", "account":"", "workspace":"/tmp/one/./project", "effort":""],
            ["id":"other", "title":"Other", "updated":"2026-10-08T11:00:00Z", "route":"ollama/test", "account":"", "workspace":"/tmp/two/project", "effort":""]]
        try send(["event":"chats", "chats":groupedJSON])
        let groups = ChatWorkspaceGroup.groups(chats: model.chats, folders: ["/tmp/one/project", "/tmp/empty", "/tmp/one/./project"])
        check(groups.count == 3, "aliased workspace duplicated or legacy chat folder lost")
        check(groups[0].chats.map(\.id) == ["newer", "older"], "workspace chats not ordered by recency")
        check(groups[1].chats.isEmpty, "workspace without chats disappeared")
        check(groups[2].chats.map(\.id) == ["other"], "same-name workspace identities merged")
        try send(["event":"chats", "chats":[summary("A"), summary("B")]])
        try send(["event":"selected", "id":"A", "entries":[]])
        model.openSideChat(choice: "ollama/test|", effort: "high")
        let requestA = commands.last?["request"] as! String
        func side(_ id: String, _ text: String = "") -> [String: Any] {
            ["id":id, "route":"ollama/test", "account":"", "label":"Test", "effort":"high", "status":"ready", "busy":false,
             "entries": text.isEmpty ? [] : [["id":"side-user-1", "kind":"user", "text":text, "route":"ollama/test", "isError":false, "changedFiles":[]]]]
        }
        try send(["event":"side", "chat":"A", "request":requestA, "side":side("side-A")])
        check(model.sideChat?.id == "side-A", "side did not open")
        model.sideDraft = "A side draft"
        try send(["event":"selected", "id":"B", "entries":[]])
        check(model.sideChat == nil && model.sideDraft.isEmpty, "side leaked across parents")
        try send(["event":"side", "chat":"A", "request":requestA, "side":side("side-A")])
        check(model.sideChat == nil, "hidden side snapshot reached the wrong parent")
        try send(["event":"selected", "id":"A", "entries":[]])
        try send(["event":"side", "chat":"A", "request":requestA, "side":side("side-A")])
        check(model.sideDraft == "A side draft", "temporary side draft lost across selection")
        model.sendSide()
        check(model.pendingSideText == "A side draft" && !model.canSendSide, "side accepted queued input")
        try send(["event":"side", "chat":"A", "request":requestA, "side":side("side-A", "A side draft")])
        check(model.pendingSideText == nil && model.sideDraft.isEmpty, "side acknowledgement lost the input")
        model.closeSideChat()
        try send(["event":"side", "chat":"A", "request":requestA, "side":side("side-A")])
        check(model.sideChat == nil, "closed side revived from late snapshot")
        model.openSideChat(choice: "ollama/test|", effort: "")
        let requestB = commands.last?["request"] as! String
        try send(["event":"side", "chat":"A", "request":requestA, "sideID":"side-A", "side":NSNull()])
        check(model.sideOpening, "old close event cleared a newer fork")
        try send(["event":"side", "chat":"A", "request":requestB, "side":side("side-B")])
        check(model.sideChat?.id == "side-B", "replacement side snapshot was dropped")
        model.sideDraft = String(repeating: "Large question ", count: 1000)
        model.sendSide()
        try send(["event":"side_accepted", "chat":"A", "request":requestB, "side":"side-B", "id":"long-question"])
        check(model.sideDraft.isEmpty && model.pendingSideText == nil, "large accepted side input depended on truncated snapshot text")
        try send(["event":"side", "chat":"A", "request":requestB, "side":NSNull(), "notice":"Side closed"])
        check(model.sideNotice == "Side closed", "scoped close notice hidden")
        try send(["event":"git_changes", "chat":"B", "changes":["files":[], "truncated":false]])
        check(model.gitChanges == nil, "Git inspection leaked across chats")
        try send(["event":"git_changes", "chat":"A", "workspace":"/wrong", "changes":["files":[], "truncated":false]])
        check(model.gitChanges == nil, "stale workspace inspection displayed")
        try send(["event":"branch_state", "chat":"A", "busy":true])
        model.draft = "Wait for checkout"
        check(!model.canSend, "main send raced branch mutation")
        try send(["event":"branch_state", "chat":"A", "busy":false, "notice":"Workspace has uncommitted changes"])
        check(model.branchNotice.contains("uncommitted") && model.canSend, "branch failure lost or left chat disabled")
        let agentEntry: [String: Any] = ["id":"lane-reply", "kind":"assistant", "text":"", "route":"ollama/test", "isError":false, "changedFiles":[]]
        let agentRows = ["lane-a", "lane-b"].map { id -> [String: Any] in
            ["id":id, "route":"ollama/test", "account":"", "label":"Test", "task":id, "status":"working", "readOnly":true, "changedFiles":[], "entries":[agentEntry]]
        }
        try send(["event":"agents", "chat":"A", "agents":agentRows])
        try send(["event":"agent_delta", "chat":"A", "agent":"lane-b", "id":"lane-reply", "text":"Only B"])
        check(model.agents[0].entries[0].text.isEmpty && model.agents[1].entries[0].text == "Only B", "parallel lane text crossed identities")
        try send(["event":"agent_delta", "chat":"B", "agent":"lane-b", "id":"lane-reply", "text":"Wrong chat"])
        check(model.agents[1].entries[0].text == "Only B" && model.agents[1].readOnly == true, "parallel state escaped parent")
        try send(["event":"entry", "chat":"A", "entry":["id":"fanout-row", "kind":"tool", "text":"", "route":"ollama/test", "isError":false, "changedFiles":[], "tool":"delegate", "agentIDs":["lane-a","lane-b"]]])
        check(model.entries.last?.agentIDs == ["lane-a", "lane-b"], "lane chips lost their tool-call identity")
        print("ChatModel state transitions passed")
    }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Stubs.swift").write_text(stubs)
            (root / "Cases.swift").write_text(cases)
            binary = root / "chat-model-tests"
            compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-module-cache-path", str(root / "cache"), str(root / "Stubs.swift"),
                str(Path(__file__).with_name("ChatModel.swift")), str(Path(__file__).with_name("ChatInspectorModel.swift")), str(Path(__file__).with_name("ChatWorkspaces.swift")), str(root / "Cases.swift"),
                "-framework", "AppKit", "-framework", "SwiftUI", "-framework", "PDFKit", "-o", str(binary)], capture_output=True, text=True, timeout=90)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertIn("state transitions passed", ran.stdout)


if __name__ == "__main__": unittest.main()
