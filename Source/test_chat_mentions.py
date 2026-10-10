"""One @Name contract: the composer's preview (Swift) and the worker's routing (Python).

Both resolvers read the same cases, so a tag the composer tints is the member
the worker routes to. Team scheduling for tagged messages is in test_chat_team.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import chat_team

CASES = Path(__file__).with_name("test_chat_mentions_cases.json")


def fixture():
    """The shared cases, plus one long message: its first 64 tags and each member's first."""
    data = json.loads(CASES.read_text(encoding="utf-8"))
    data["cases"].append({"text": "@Sol " * 70 + "@Kimi",
                          "tags": [["s", 5 * index, 4] for index in range(64)] + [["k", 350, 5]]})
    return data


SWIFT_CASES = r'''
import AppKit
import SwiftUI

@main struct Cases {
    static func main() throws {
        var failures: [String] = []
        func check(_ yes: Bool, _ text: String) { if !yes { failures.append(text) } }
        let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [String: Any]
        func roster(_ rows: Any) -> [ChatMentionTarget] {
            (rows as! [[String: Any]]).map { ChatMentionTarget(id: $0["id"] as! String, name: $0["name"] as! String, route: $0["route"] as! String) }
        }
        let members = roster(fixture["members"]!)
        for item in fixture["cases"] as! [[String: Any]] {
            let text = item["text"] as! String
            let found = ChatMentions.resolve(text, members: item["members"].map(roster) ?? members)
                .map { "\($0.target.id):\($0.range.location):\($0.range.length)" }
            let expected = (item["tags"] as! [[Any]]).map { "\($0[0]):\($0[1]):\($0[2])" }
            check(found == expected, "resolve \(text.debugDescription): \(found) != \(expected)")
        }
        check(ChatMentions.addressees(ChatMentions.resolve("@Kimi @Sol @kimi", members: members)) == ["k", "s"],
              "addressees lost tag order or repeated a member")

        func typing(_ text: String, _ caret: Int) -> String {
            ChatMentions.typing(text, caret: caret).map { "\($0.start):\($0.query)" } ?? "none"
        }
        for (text, caret, expected) in [("@", 1, "0:"), ("hi @So", 6, "3:So"), ("(@Ki", 4, "1:Ki"), ("a@b", 3, "none"),
                                         ("@Sol x", 6, "none"), ("```\n@So", 7, "none"), ("`@So", 4, "none"), ("@So", 0, "none")] {
            check(typing(text, caret) == expected, "typing \(text.debugDescription) at \(caret): \(typing(text, caret))")
        }
        check(ChatMentions.candidates("o", members: members).map(\.id) == ["o", "o2", "s", "z"], "candidates: prefix first, then contains")
        check(ChatMentions.candidates("", members: members).map(\.id) == ["s", "o", "o2", "k", "z"], "a bare @ lists the roster in order")
        check(ChatMentions.candidates("", members: roster((fixture["cases"] as! [[String: Any]]).last { $0["members"] != nil }!["members"]!)).map(\.id) == ["k"],
              "a name two members share was offered")

        let menu = ChatMentionMenu()
        menu.update(text: "@", caret: 1, members: members)
        check(menu.state?.candidates.count == 5 && menu.current?.id == "s", "the list did not open on @")
        menu.move(-1); check(menu.current?.id == "z", "up did not wrap to the last member")
        menu.move(1); check(menu.current?.id == "s", "down did not wrap to the first member")
        menu.select(2)
        menu.update(text: "@o", caret: 2, members: members)
        check(menu.current?.id == "o2", "narrowing lost the highlighted member")
        menu.close(); check(menu.state == nil, "Escape kept the list open")
        menu.update(text: "@op", caret: 3, members: members)
        check(menu.state == nil, "a dismissed tag reopened while typing")
        menu.update(text: "@op @", caret: 5, members: members)
        check(menu.state?.start == 4, "a new tag stayed dismissed")
        menu.update(text: "@op @", caret: nil, members: members)
        check(menu.state == nil, "marked text or a selection kept the list")
        menu.update(text: "@", caret: 1, members: [])
        check(menu.state == nil, "a chat without a Team opened the list")

        let message = "Hi @Sol and @Opus"
        let marked = ChatMentions.marked(message, mentions: [
            ChatMention(id: "s", name: "Sol", route: "r", start: 3, length: 4),
            ChatMention(id: "o", name: "Kimi", route: "r", start: 12, length: 5),
            ChatMention(id: "o", name: "Opus", route: "r", start: 40, length: 5),
            ChatMention(id: "o", name: "Opus", route: "r", start: -1, length: 5)], accent: { _ in .red })
        let tinted = marked.runs.filter { $0[ChatMentionAttribute.self] != nil }.map { String(marked[$0.range].characters) }
        check(tinted == ["@Sol"] && String(marked.characters) == message, "marked \(tinted): only the matching recorded tag may be tinted")
        let decoded = try JSONDecoder().decode([ChatMention].self, from: Data(#"[{"id":"s","name":"Sol","route":"r","start":3,"length":4},{"start":"x"},7]"#.utf8))
        check(decoded.count == 3 && decoded[0] == ChatMention(id: "s", name: "Sol", route: "r", start: 3, length: 4) &&
              decoded[1].start == -1 && decoded[2].id.isEmpty, "a malformed tag record failed its entry")

        if !failures.isEmpty {
            FileHandle.standardError.write(Data(failures.joined(separator: "\n").utf8))
            exit(1)
        }
        print("mention cases passed")
    }
}
'''


class MentionResolverTests(unittest.TestCase):
    def test_worker_resolves_every_shared_case(self):
        data = fixture()
        for case in data["cases"]:
            with self.subTest(text=case["text"][:40]):
                team = {"members": case.get("members", data["members"])}
                found = [[tag["id"], tag["start"], tag["length"]] for tag in chat_team.mentions(team, case["text"])]
                self.assertEqual(found, case["tags"])

    def test_records_keep_name_and_route_as_sent(self):
        team = {"members": [{"id": "s", "name": "Sol", "route": "codex/sol"}]}
        self.assertEqual(chat_team.mentions(team, "ask @SOL"),
                         [{"id": "s", "name": "Sol", "route": "codex/sol", "start": 4, "length": 4}])
        self.assertEqual(chat_team.mentions(None, "@Sol"), [])
        self.assertEqual(chat_team.mentions(team, "no tags"), [])
        self.assertEqual(chat_team.addressees({"mentions": chat_team.mentions(team, "@Sol @sol")}), ["s"])
        self.assertEqual(chat_team.addressees(None), [])

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "Swift resolver test needs macOS")
    def test_composer_resolves_every_shared_case_and_completes_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cases.json").write_text(json.dumps(fixture()), encoding="utf-8")
            (root / "Cases.swift").write_text(SWIFT_CASES)
            binary = root / "mentions"
            compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-module-cache-path", str(root / "cache"), str(Path(__file__).with_name("ChatMentions.swift")),
                str(root / "Cases.swift"), "-framework", "AppKit", "-framework", "SwiftUI", "-o", str(binary)],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary), str(root / "cases.json")], capture_output=True, text=True, timeout=15)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn("mention cases passed", ran.stdout)


if __name__ == "__main__": unittest.main()
