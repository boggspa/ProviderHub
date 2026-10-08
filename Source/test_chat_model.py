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
        let model = ChatModel { commands.append($0); return true }
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
                str(Path(__file__).with_name("ChatModel.swift")), str(root / "Cases.swift"),
                "-framework", "AppKit", "-framework", "SwiftUI", "-framework", "PDFKit", "-o", str(binary)], capture_output=True, text=True, timeout=90)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertIn("state transitions passed", ran.stdout)


if __name__ == "__main__": unittest.main()
