"""Each Team member keeps its own speed: validated per route, published, sent per turn."""
from pathlib import Path
import tempfile
import unittest

from chat_runtime import ChatService, ChatStore
from test_chat_runtime import FakeTransport, model, response

SPEEDS = [{"id": "fast", "label": "Fast", "detail": "2x speed, 2x usage"}]


class TeamSpeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ChatStore(self.root, lock=False)
        self.events = []

    def service(self, scripts):
        rows = [{**model("codex/gpt-6.2-sol"), "speeds": SPEEDS}, model("ollama/other", "work")]
        self.transports = [FakeTransport(script) for script in scripts]
        for transport in self.transports: transport.rows = rows
        self.primary = FakeTransport([]); self.primary.rows = rows
        available = iter(self.transports)
        parent = ChatService(self.store, self.primary, self.events.append, child_factory=lambda: next(available))
        parent.refresh(); parent.create(parent.models[0]["id"], str(self.root))
        return parent

    def configure(self, parent, speeds):
        specs = [{"choice": parent.models[i]["id"], "name": f"Member {i + 1}", "effort": "",
                  "speed": speed, "responsibility": ""} for i, speed in enumerate(speeds)]
        parent.handle({"command": "configure_team", "id": parent.chat["id"], "enabled": True,
                       "execution": {"mode": "contribution"}, "members": specs, "request": "r"})

    def test_member_speed_is_saved_published_and_sent_only_where_offered(self):
        parent = self.service([[response("One")], [response("Two")]])
        self.configure(parent, ["fast", ""])
        members = parent.chat["team"]["members"]
        self.assertEqual([m["speed"] for m in members], ["fast", ""])
        published = [e for e in self.events if e["event"] == "team"][-1]["team"]["members"]
        self.assertEqual([m["speed"] for m in published], ["fast", ""])
        parent.handle({"command": "send", "id": parent.chat["id"], "text": "Go"})
        parent.thread.join(4)
        self.assertEqual(self.transports[0].requests[0].get("speed"), "fast")
        self.assertNotIn("speed", self.transports[1].requests[0])

    def test_unoffered_speed_is_rejected_and_roster_unchanged(self):
        parent = self.service([])
        self.configure(parent, ["", "fast"])
        self.assertFalse(parent.chat.get("team"))

    def test_speed_change_keeps_member_history(self):
        import chat_team
        parent = self.service([])
        self.configure(parent, ["", ""])
        parent.chat["team"]["members"][0]["messages"] = ["kept"]
        specs = [{"id": m["id"], "choice": m["choice"], "name": m["name"], "effort": "", "speed": "fast" if i == 0 else "",
                  "responsibility": ""} for i, m in enumerate(parent.chat["team"]["members"])]
        members, archived = chat_team.validate_members(parent, specs)
        self.assertEqual(members[0]["messages"], ["kept"])
        self.assertEqual(members[0]["speed"], "fast")
        self.assertEqual(archived, [])


if __name__ == "__main__":
    unittest.main()
