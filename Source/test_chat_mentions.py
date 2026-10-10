"""One @Name contract: the composer decides every tag, and the worker keeps every chip it draws.

The composer's resolver (ChatMentions.swift) must draw the shared cases, and the
worker (chat_team.claimed) must keep each of those chips unchanged. A generated
corpus, mixing names and characters whose case, composition or Unicode version
differs between Swift and Python, checks that the worker keeps everything the
composer draws and that the transcript tints all of it again. Team scheduling
for tagged messages is in test_chat_team.
"""
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest

import chat_team

CASES = Path(__file__).with_name("test_chat_mentions_cases.json")
# What made the two runtimes disagree before: case beyond ASCII, both encodings
# of an accent, combining marks in either order, astral characters, and letters
# newer than CPython's Unicode data (U+1C89, U+1C8A).
NAMES = ["Sol", "SOL", "Opus", "Opus 2", "Kimi", "kimi", "Zoë", "Zoë", "ZOË", "SS", "ß",
         "\U0001f44d", "ᾴ", "ạ́", "Ᲊ", "Grok-4", "Ünal", "中文"]
PIECES = [" ", " ", "\n", "\t", ",", ".", "(", ")", "*", "`", "```\n", "~~~\n", "@", "_", "2", "'", "\"",
          " ", "​", "“", "‘", "＠", "\u0085", " ", "ë", "ë", "Ë", "ß",
          "ss", "α", "ι", "ͅ", "́", "̣", "\U0001f3fd", "\U0001f44d", "ᲊ", "ſ",
          "K", "İ", "ı", "x", "go", "and"]


def fixture():
    """The shared cases, plus one long message: its first 64 tags and each member's first."""
    data = json.loads(CASES.read_text(encoding="utf-8"))
    data["cases"].append({"text": "@Sol " * 70 + "@Kimi",
                          "tags": [["s", 5 * index, 4] for index in range(64)] + [["k", 350, 5]]})
    return data


def corpus(count=3000, seed=29):
    """Messages for small rosters that tag names as written and as case-changed,
    composed or decomposed variants, among awkward neighbouring characters."""
    rng = random.Random(seed)
    cases = []
    for _ in range(count):
        names = rng.sample(NAMES, rng.randint(1, 4))
        pieces = []
        for _ in range(rng.randint(1, 10)):
            if rng.random() < .45:
                name = rng.choice(names)
                pieces.append("@" + rng.choice([name, name, name.upper(), name.lower(), name.swapcase(),
                                                unicodedata.normalize("NFC", name), unicodedata.normalize("NFD", name)]))
            else:
                pieces.append(rng.choice(PIECES))
        cases.append({"text": "".join(pieces),
                      "members": [{"id": f"m{i}", "name": name, "route": f"r{i}"} for i, name in enumerate(names)]})
    return cases


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
        func tinted(_ marked: AttributedString) -> [[UInt16]] {
            marked.runs.filter { $0[ChatMentionAttribute.self] != nil }.map { Array(String(marked[$0.range].characters).utf16) }
        }
        let members = roster(fixture["members"]!)
        for item in fixture["cases"] as! [[String: Any]] {
            let text = item["text"] as! String
            let found = ChatMentions.resolve(text, members: item["members"].map(roster) ?? members)
                .map { "\($0.target.id):\($0.range.location):\($0.range.length)" }
            let expected = (item["tags"] as! [[Any]]).map { "\($0[0]):\($0[1]):\($0[2])" }
            check(found == expected, "resolve \(text.debugDescription): \(found) != \(expected)")
        }
        let chip = ChatMentions.resolve("hi @sol", members: members).map(\.wire)
        check(chip.count == 1 && chip[0]["id"] as? String == "s" && chip[0]["name"] as? String == "Sol" &&
              chip[0]["route"] as? String == "codex/gpt-6.1-sol" && chip[0]["start"] as? Int == 3 && chip[0]["length"] as? Int == 4,
              "a chip's record lost its member, name or range")

        // Each chip drawn for the corpus is tinted again from its record, as
        // the transcript draws it, and written out for the worker to check.
        var drawn: [[[String: Any]]] = []
        for item in fixture["corpus"] as? [[String: Any]] ?? [] {
            let text = item["text"] as! String, string = text as NSString
            let matches = ChatMentions.resolve(text, members: roster(item["members"]!))
            drawn.append(matches.map(\.wire))
            let records = matches.map { ChatMention(id: $0.target.id, name: $0.target.name, route: $0.target.route,
                                                    start: $0.range.location, length: $0.range.length) }
            let history = tinted(ChatMentions.marked(text, mentions: records, accent: { _ in .red }))
            let expected = matches.map { Array(string.substring(with: ChatMentions.display($0.range, in: string)).utf16) }
            check(history == expected, "history did not tint what the composer drew in \(text.debugDescription)")
        }
        if CommandLine.arguments.count > 2 {
            try JSONSerialization.data(withJSONObject: drawn).write(to: URL(fileURLWithPath: CommandLine.arguments[2]))
        }

        func typing(_ text: String, _ caret: Int) -> String {
            ChatMentions.typing(text, caret: caret).map { "\($0.start):\($0.query)" } ?? "none"
        }
        for (text, caret, expected) in [("@", 1, "0:"), ("hi @So", 6, "3:So"), ("(@Ki", 4, "1:Ki"), ("a@b", 3, "none"),
                                         ("@Sol x", 6, "none"), ("```\n@So", 7, "none"), ("`@So", 4, "none"), ("@So", 0, "none")] {
            check(typing(text, caret) == expected, "typing \(text.debugDescription) at \(caret): \(typing(text, caret))")
        }
        check(ChatMentions.candidates("o", members: members).map(\.id) == ["o", "o2", "s", "z"], "candidates: prefix first, then contains")
        check(ChatMentions.candidates("", members: members).map(\.id) == ["s", "o", "o2", "k", "z"], "a bare @ lists the roster in order")
        let shared = (fixture["cases"] as! [[String: Any]]).first { $0["text"] as? String == "@Sol and @Kimi" }!
        check(ChatMentions.candidates("", members: roster(shared["members"]!)).map(\.id) == ["k"],
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

        // Literal identity: only the name as written reads as its tag, and a
        // rename between two encodings of one name repaints the composer.
        let zoe = ChatMentionTarget(id: "z", name: "Zo\u{EB}", route: "r")
        check(ChatMentions.resolve("@zo\u{EB} @ZO\u{CB} @Zoe\u{308}", members: [zoe]).map(\.range.location) == [0],
              "a tag matched beyond the case of ASCII letters")
        check(zoe != ChatMentionTarget(id: "z", name: "Zoe\u{308}", route: "r") && zoe == ChatMentionTarget(id: "z", name: "Zo\u{EB}", route: "r"),
              "member identity is not literal")
        check(ChatMentions.same("Sol", "Sol") && !ChatMentions.same("Zo\u{EB}", "Zoe\u{308}"), "same() is not literal")

        let message = "Hi @Sol and @Opus"
        let marked = ChatMentions.marked(message, mentions: [
            ChatMention(id: "s", name: "Sol", route: "r", start: 3, length: 4),
            ChatMention(id: "o", name: "Kimi", route: "r", start: 12, length: 5),
            ChatMention(id: "o", name: "Opus", route: "r", start: 40, length: 5),
            ChatMention(id: "o", name: "Opus", route: "r", start: -1, length: 5)], accent: { _ in .red })
        check(tinted(marked) == [Array("@Sol".utf16)] && String(marked.characters) == message, "only the matching recorded tag may be tinted")
        let exact = ChatMentions.marked("@ZO\u{CB} and @zo\u{EB}", mentions: [
            ChatMention(id: "z", name: "Zo\u{EB}", route: "r", start: 0, length: 4),
            ChatMention(id: "z", name: "Zo\u{EB}", route: "r", start: 9, length: 4)], accent: { _ in .red })
        check(tinted(exact) == [Array("@zo\u{EB}".utf16)], "history tinted a record that does not read its name")
        let thumbs = ChatMentions.marked("@👍🏽 ok", mentions: [ChatMention(id: "t", name: "👍", route: "r", start: 0, length: 3)],
                                         accent: { _ in .red })
        check(tinted(thumbs) == [Array("@👍🏽".utf16)], "a tag ending inside a composed character was not drawn whole")
        let damaged = ChatMentions.marked(message, mentions: [
            ChatMention(id: "s", name: "Sol", route: "r", start: Int.max, length: 4),
            ChatMention(id: "s", name: "Sol", route: "r", start: 3, length: Int.max),
            ChatMention(id: "s", name: "Sol", route: "r", start: Int.max - 1, length: Int.max),
            ChatMention(id: "s", name: "", route: "r", start: 3, length: 1)], accent: { _ in .red })
        check(tinted(damaged).isEmpty && String(damaged.characters) == message, "an out-of-range tag record was drawn")
        let decoded = try JSONDecoder().decode([ChatMention].self, from: Data(#"[{"id":"s","name":"Sol","route":"r","start":3,"length":4},{"start":"x"},7]"#.utf8))
        check(decoded.count == 3 && decoded[0] == ChatMention(id: "s", name: "Sol", route: "r", start: 3, length: 4) &&
              decoded[1].start == -1 && decoded[2].id.isEmpty, "a malformed tag record failed its entry")

        if !failures.isEmpty {
            FileHandle.standardError.write(Data(failures.prefix(20).joined(separator: "\n").utf8))
            exit(1)
        }
        print("mention cases passed")
    }
}
'''


class MentionContractTests(unittest.TestCase):
    def test_worker_keeps_every_shared_case_as_drawn(self):
        data = fixture()
        for case in data["cases"]:
            with self.subTest(text=ascii(case["text"][:40])):
                members = case.get("members", data["members"])
                by_id = {m["id"]: m for m in members}
                chips = [{"id": i, "name": by_id[i]["name"], "route": by_id[i]["route"], "start": start, "length": length}
                         for i, start, length in case["tags"]]
                self.assertEqual(chat_team.claimed({"members": members}, case["text"], chips), chips)

    def test_worker_refuses_chips_that_do_not_read_as_drawn(self):
        team = {"members": [{"id": "t", "name": "\U0001f44d", "route": "r"}, {"id": "s", "name": "Sol", "route": "r"},
                            {"id": "z", "name": "Zoë", "route": "r"}]}
        text = "@\U0001f44d @SOL @zoë"
        thumb = {"id": "t", "name": "\U0001f44d", "route": "r", "start": 0, "length": 3}
        sol = {"id": "s", "name": "Sol", "route": "r", "start": 4, "length": 4}
        zoe = {"id": "z", "name": "Zoë", "route": "r", "start": 9, "length": 4}
        self.assertEqual(chat_team.claimed(team, text, [thumb, sol, zoe]), [thumb, sol, zoe])
        self.assertEqual(chat_team.claimed(None, text, []), [])
        self.assertEqual(chat_team.addressees({"mentions": [sol, {**sol, "start": 9}]}), ["s"])
        self.assertEqual(chat_team.addressees(None), [])
        # Out of order, overlapping, splitting a surrogate pair, outside the
        # text, too many, or not chips at all; with no Team; or over the name
        # written another way: case beyond ASCII, or another accent encoding.
        refused = [(team, text, chips) for chips in (
            [sol, thumb], [sol, sol], [{**thumb, "length": 2}], [{**sol, "length": 9}], [{**thumb, "start": -1}],
            [{**sol, "start": 10 ** 30}], [{**sol, "start": 4.0}], [thumb] + [sol] * 68, "chips", [None])]
        refused += [(None, text, [sol]), (team, "@ZOË", [{**zoe, "start": 0}]), (team, "@Zoë", [{**zoe, "start": 0}]),
                    (team, "@Zoë", [{**zoe, "name": "Zoë", "start": 0, "length": 5}])]
        for roster, message, chips in refused:
            with self.subTest(text=ascii(message), chips=ascii(chips)[:80]), self.assertRaisesRegex(ValueError, "no longer match"):
                chat_team.claimed(roster, message, chips)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "Swift resolver test needs macOS")
    def test_composer_draws_the_shared_cases_and_the_worker_keeps_all_it_draws(self):
        data = {**fixture(), "corpus": corpus()}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cases.json").write_text(json.dumps(data), encoding="utf-8")
            (root / "Cases.swift").write_text(SWIFT_CASES)
            binary = root / "mentions"
            compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-module-cache-path", str(root / "cache"), str(Path(__file__).with_name("ChatMentions.swift")),
                str(root / "Cases.swift"), "-framework", "AppKit", "-framework", "SwiftUI", "-o", str(binary)],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary), str(root / "cases.json"), str(root / "drawn.json")],
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn("mention cases passed", ran.stdout)
            drawn = json.loads((root / "drawn.json").read_text(encoding="utf-8"))
        self.assertEqual(len(drawn), len(data["corpus"]))
        refused = []
        for case, chips in zip(data["corpus"], drawn):
            try: kept = chat_team.claimed({"members": case["members"]}, case["text"], chips)
            except ValueError: kept = None
            if kept != chips: refused.append(ascii(case["text"]))
        self.assertEqual(refused[:5], [], f"the worker refused {len(refused)} messages as the composer drew them")
        # The corpus reached the tags that diverged before: accented, combining and astral names.
        names = [chip["name"] for chips in drawn for chip in chips]
        self.assertGreater(len(names), 1000)
        self.assertGreater(sum(any(ord(c) > 127 for c in name) for name in names), 200)


if __name__ == "__main__": unittest.main()
