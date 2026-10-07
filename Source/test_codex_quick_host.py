"""The Hub-facing channel: stdin commands, private events, send relay."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from codex_quick_host import PRIVATE_EVENTS, HostInput, QuickHost
from test_codex_accent import FakeTransport

TID = "00000000-0000-4000-8000-000000000001"
ROW = {"threadId": TID, "hostId": "local", "kind": "local", "title": "Fix the tests", "supported": True,
       "active": True, "activeAccent": "#705AFF", "preview": "latest assistant reply"}


def sent(pipe, method):
    return [m for m in pipe.sent if m["method"] == method]


class HostInputTests(unittest.TestCase):
    def test_reads_json_lines_without_blocking_and_notices_eof(self):
        read_fd, write_fd = os.pipe()
        try:
            source = HostInput(read_fd)
            self.assertEqual(source.poll(), [])  # nothing yet, and no wait
            os.write(write_fd, b'{"command":"hello"}\n{"command":"quick-watch",')
            self.assertEqual(source.poll(), [{"command": "hello"}])
            os.write(write_fd, b'"active":true}\nnot json\n[1,2]\n')
            self.assertEqual(source.poll(), [{"command": "quick-watch", "active": True}])
            os.write(write_fd, b'{"command":"quick-send","requestId":"r1"}')  # no newline, then EOF
            os.close(write_fd); write_fd = None
            # The partial line waits for a newline; EOF on the next poll flushes it.
            self.assertEqual(source.poll() + source.poll(), [{"command": "quick-send", "requestId": "r1"}])
            self.assertTrue(source.closed)
            self.assertEqual(source.poll(), [])
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)

    def test_dev_null_counts_as_no_hub(self):
        fd = os.open(os.devnull, os.O_RDONLY)
        try:
            source = HostInput(fd)
            self.assertEqual(source.poll(), [])
            self.assertTrue(source.closed)
        finally:
            os.close(fd)

    def test_an_oversized_line_is_dropped(self):
        read_fd, write_fd = os.pipe()
        try:
            source = HostInput(read_fd)
            # Beyond a megabyte without a newline the buffer is abandoned
            # rather than grown. Writes stay under the pipe's own buffer.
            for _ in range(40):
                os.write(write_fd, b"z" * 32768)
                source.poll()
            self.assertLessEqual(len(source.buffer), 1 << 20)
            os.write(write_fd, b'\n{"command":"hello"}\n')
            self.assertEqual(source.poll(), [{"command": "hello"}])
        finally:
            os.close(read_fd); os.close(write_fd)


class QuickHostTests(unittest.TestCase):
    def setUp(self):
        self.pipe, self.events = FakeTransport(), []
        self.host = QuickHost(self.events.append, self.pipe)

    def test_rows_flow_only_while_a_connected_hub_watches_and_only_when_changed(self):
        self.host.update("main", [ROW])
        self.assertEqual(self.events, [])
        self.assertFalse(self.host.request_open())
        self.host.command({"command": "quick-watch", "active": True})
        self.host.update("main", [ROW])
        self.assertEqual(self.events, [])  # watching but not connected
        self.host.command({"command": "hello"})
        self.assertEqual(self.events[-1], {"event": "host", "connected": True})
        self.assertTrue(self.host.is_watching())
        self.host.update("main", [ROW])
        self.assertEqual(self.events[-1], {"event": "quick-rows", "rows": [ROW]})
        self.host.update("main", [ROW])
        self.assertEqual(len(self.events), 2)  # unchanged rows are not repeated
        self.host.update("main", [])
        self.assertEqual(self.events[-1], {"event": "quick-rows", "rows": []})
        self.assertTrue(self.host.request_open())
        self.assertEqual(self.events[-1], {"event": "quick-open"})
        # Re-watching resends the current rows once, even if unchanged.
        self.host.command({"command": "quick-watch", "active": False})
        self.assertFalse(self.host.is_watching())
        self.host.update("main", [])
        self.assertEqual(self.events[-1], {"event": "quick-open"})
        self.host.command({"command": "quick-watch", "active": True})
        self.host.update("main", [])
        self.assertEqual(self.events[-1], {"event": "quick-rows", "rows": []})
        self.assertIn("quick-rows", PRIVATE_EVENTS)
        self.assertIn("quick-result", PRIVATE_EVENTS)

    def test_send_is_relayed_to_the_composer_session_and_the_result_returned(self):
        self.host.attach_main("other")
        self.host.command({"command": "hello"})
        self.host.update("main", [ROW])
        self.host.command({"command": "quick-send", "requestId": "r1", "threadId": TID, "prompt": "secret prompt"})
        call = sent(self.pipe, "Runtime.evaluate")[-1]
        self.assertEqual(call["sessionId"], "main")
        self.assertTrue(call["params"]["awaitPromise"])
        self.assertIn("sendFromHost(", call["params"]["expression"])
        self.assertIn("secret prompt", call["params"]["expression"])
        self.assertFalse(self.host.handle({"id": call["id"] + 1000, "result": {}}))
        self.assertTrue(self.host.handle({"id": call["id"], "result": {"result": {"value": {"ok": True, "reason": "Sent."}}}}))
        self.assertEqual(self.events[-1], {"event": "quick-result", "requestId": "r1", "threadId": TID, "ok": True, "reason": "Sent."})
        self.assertNotIn("secret", json.dumps(self.events))
        for outcome, expected in (({"error": {"message": "boom"}}, "Send failed. Your draft is kept."),
                                  ({"result": {"result": {"value": None}}}, "Sending is unavailable in this Desktop version. Draft kept."),
                                  ({"result": {"result": {"value": {"ok": False, "reason": "Send failed: Desktop rejected the send. Your draft is kept."}}}},
                                   "Send failed: Desktop rejected the send. Your draft is kept.")):
            self.host.command({"command": "quick-send", "requestId": "r2", "threadId": TID, "prompt": "again"})
            call = sent(self.pipe, "Runtime.evaluate")[-1]
            self.host.handle({"id": call["id"], **outcome})
            self.assertEqual(self.events[-1]["ok"], False)
            self.assertEqual(self.events[-1]["reason"], expected)
        # Without a composer session the first Codex window is used.
        self.host.detach("main")
        self.host.command({"command": "quick-send", "requestId": "r3", "threadId": TID, "prompt": "hello"})
        self.assertEqual(sent(self.pipe, "Runtime.evaluate")[-1]["sessionId"], "other")

    def test_bad_sends_are_answered_without_touching_codex(self):
        self.host.command({"command": "hello"})
        before = len(self.pipe.sent)
        self.host.command({"command": "quick-send", "threadId": TID, "prompt": "hi"})  # no request id: nothing to answer
        self.host.command({"command": "quick-send", "requestId": "r1", "threadId": "nope", "prompt": "hi"})
        self.assertEqual(self.events[-1], {"event": "quick-result", "requestId": "r1", "threadId": "nope", "ok": False,
                                           "reason": "A valid local thread is required."})
        self.host.command({"command": "quick-send", "requestId": "r2", "threadId": TID, "prompt": "  "})
        self.assertEqual(self.events[-1]["reason"], "A non-empty prompt of at most 64 KiB is required.")
        self.host.command({"command": "quick-send", "requestId": "r3", "threadId": TID, "prompt": "x" * 65537})
        self.assertEqual(self.events[-1]["reason"], "A non-empty prompt of at most 64 KiB is required.")
        self.host.command({"command": "quick-send", "requestId": "r4", "threadId": TID, "prompt": "hello"})
        self.assertEqual(self.events[-1]["reason"], "Codex's window is not available. Your draft is kept.")
        self.assertEqual(len(self.pipe.sent), before)
        self.host.command({"command": "something-else"})
        self.host.command({})

    def test_disconnect_stops_rows_and_open_requests(self):
        self.host.command({"command": "hello"})
        self.host.command({"command": "quick-watch", "active": True})
        self.host.disconnected()
        self.assertEqual(self.events[-1], {"event": "host", "connected": False})
        self.assertFalse(self.host.is_watching())
        self.assertFalse(self.host.request_open())
        self.host.update("main", [ROW])
        self.assertEqual(self.events[-1], {"event": "host", "connected": False})
        self.host.disconnected()  # idempotent, no second event
        self.assertEqual(len([e for e in self.events if e["event"] == "host"]), 2)


if __name__ == "__main__":
    unittest.main()
