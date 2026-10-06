"""The quick popover's worker reads only open, observed local rows."""
import json
import unittest
from unittest import mock

from codex_quick_bridge import QuickComposerBridge
from test_codex_accent import FakeTransport


def target(n=1, **changes):
    return {"threadId": f"00000000-0000-4000-8000-{n:012d}", "hostId": "local", "kind": "local", **changes}


def reply(transport, value):
    return {"id": transport.sent[-1]["id"], "result": {"result": {"value": value}}}


class QuickComposerBridgeTests(unittest.TestCase):
    def test_closed_composer_never_reads_transcripts_and_calls_are_origin_guarded(self):
        pipe, reader = FakeTransport(), mock.Mock()
        bridge = QuickComposerBridge(pipe, reader)
        bridge.refresh({"window"})
        expression = pipe.sent[-1]["params"]["expression"]
        self.assertIn("window !== window.top", expression)
        self.assertIn("app:", expression)
        self.assertIn("recentThreadTargets", expression)
        self.assertTrue(bridge.handle(reply(pipe, [])))
        reader.assert_not_called()
        self.assertEqual(len(pipe.sent), 1)

    def test_only_ten_observed_valid_local_targets_in_order_and_no_prompts_in_events(self):
        pipe, events = FakeTransport(), []
        reader = mock.Mock(return_value=[{**target(2), "preview": "latest\nassistant"},
                                         {**target(50), "preview": "unrequested"}])
        bridge = QuickComposerBridge(pipe, reader, events.append)
        bridge.refresh({"window"})
        observed = [target(2, title="Secret title"), target(1), target(2),
                    target(3, hostId="other"), target(4, kind="chatgpt"), target(5, threadId="bad")]
        observed += [target(n) for n in range(6, 20)]
        bridge.handle(reply(pipe, observed))
        expected = [target(n) for n in (2, 1, 6, 7, 8, 9)]
        reader.assert_called_once_with(expected)
        expression = pipe.sent[-1]["params"]["expression"]
        self.assertIn("latest assistant", expression)
        self.assertNotIn("Secret title", expression)
        self.assertNotIn("unrequested", expression)
        self.assertNotIn("latest", json.dumps(events))

    def test_reader_failure_explicitly_clears_previews_without_leaking_exception_text(self):
        pipe, events = FakeTransport(), []
        bridge = QuickComposerBridge(pipe, mock.Mock(side_effect=ValueError("private content")), events.append)
        bridge.refresh({"window"})
        bridge.handle(reply(pipe, [target()]))
        self.assertIn('"preview": null', pipe.sent[-1]["params"]["expression"])
        self.assertNotIn("private content", json.dumps(events))

    def test_detach_drops_pending_reads_and_late_replies_cannot_read_content(self):
        pipe, reader = FakeTransport(), mock.Mock()
        bridge = QuickComposerBridge(pipe, reader)
        bridge.refresh({"window"})
        late = reply(pipe, [target()])
        bridge.refresh(set())
        self.assertFalse(bridge.handle(late))
        reader.assert_not_called()

    def test_lost_reply_times_out_without_overlapping_reads(self):
        pipe = FakeTransport()
        bridge = QuickComposerBridge(pipe, mock.Mock())
        with mock.patch("codex_quick_bridge.time.monotonic", return_value=1):
            bridge.refresh({"window"})
        with mock.patch("codex_quick_bridge.time.monotonic", return_value=4):
            bridge.refresh({"window"})
        self.assertEqual(len(pipe.sent), 1)
        old = pipe.sent[-1]["id"]
        with mock.patch("codex_quick_bridge.time.monotonic", return_value=12):
            bridge.refresh({"window"})
        self.assertEqual(len(pipe.sent), 2)
        self.assertNotIn(old, bridge.pending)

    def test_renderer_exception_is_content_free_and_does_not_end_polling(self):
        pipe, reader, events = FakeTransport(), mock.Mock(), []
        bridge = QuickComposerBridge(pipe, reader, events.append)
        bridge.refresh({"window"})
        bridge.handle({"id": pipe.sent[-1]["id"], "result": {"exceptionDetails": {"text": "secret prompt"}}})
        reader.assert_not_called()
        self.assertNotIn("secret prompt", json.dumps(events))
        bridge.next_poll = 0
        bridge.refresh({"window"})
        self.assertEqual(len(pipe.sent), 2)
