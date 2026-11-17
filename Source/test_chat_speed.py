"""Chat's speed choice: validated per route, saved, carried only where offered, sent per turn."""
from pathlib import Path
import tempfile
import unittest

from chat_runtime import ChatService, ChatStore
from test_chat_runtime import FakeTransport, model, response

SPEEDS = [{"id": "fast", "label": "Fast", "detail": "2x speed, 2x usage"},
          {"id": "ultrafast", "label": "Ultrafast", "detail": "Up to 4.5x speed · 6x usage"}]


class ChatSpeedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = ChatStore(self.root, lock=False)
        self.events = []

    def service(self, replies=()):
        transport = FakeTransport(list(replies))
        transport.rows = [{**model("codex/gpt-6.2-sol"), "speeds": SPEEDS}, model("ollama/other", "work")]
        service = ChatService(self.store, transport, self.events.append)
        service.refresh(); service.create(transport.rows[0]["id"], str(self.root))
        return service, transport

    def turn(self, service):
        service.handle({"command": "send", "id": service.chat["id"], "text": "Go"})
        service.thread.join(3)

    def test_offered_speed_is_saved_published_and_sent(self):
        service, transport = self.service([response(), response()])
        service.handle({"command": "configure", "speed": "ultrafast"})
        self.assertEqual(service.chat["speed"], "ultrafast")
        self.assertEqual(service.summaries()[0]["speed"], "ultrafast")
        service.chat["entries"].append({"id": "x", "kind": "notice", "text": ""})  # not an empty chat any more
        self.turn(service)
        self.assertEqual(transport.requests[-1]["speed"], "ultrafast")
        service.handle({"command": "configure", "speed": ""})
        self.turn(service)
        self.assertNotIn("speed", transport.requests[-1])

    def test_unoffered_speed_is_rejected(self):
        service, _ = self.service()
        with self.assertRaises(ValueError):
            service.handle({"command": "configure", "speed": "warp"})
        service.handle({"command": "configure", "choice": "ollama/other|work"})
        with self.assertRaises(ValueError):
            service.handle({"command": "configure", "speed": "fast"})

    def test_switching_model_drops_a_speed_the_new_route_lacks(self):
        service, _ = self.service()
        service.handle({"command": "configure", "speed": "fast"})
        service.chat["entries"].append({"id": "x", "kind": "notice", "text": ""})
        service.handle({"command": "configure", "choice": "ollama/other|work"})
        self.assertEqual(service.chat.get("speed", ""), "")
        self.assertNotIn("speed", service.payload(service.choice()))

    def test_effort_change_keeps_the_saved_speed(self):
        service, _ = self.service()
        service.handle({"command": "configure", "speed": "fast"})
        service.chat["entries"].append({"id": "x", "kind": "notice", "text": ""})
        service.handle({"command": "configure", "effort": "high"})
        self.assertEqual(service.chat["speed"], "fast")
        self.assertEqual(service.payload(service.choice())["speed"], "fast")


if __name__ == "__main__":
    unittest.main()
