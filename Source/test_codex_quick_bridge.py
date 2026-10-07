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

    def test_send_report_is_logged_once_without_content_and_targets_still_read(self):
        pipe, events = FakeTransport(), []
        reader = mock.Mock(return_value=[])
        bridge = QuickComposerBridge(pipe, reader, events.append)
        bridge.refresh({"window"})
        expression = pipe.sent[-1]["params"]["expression"]
        self.assertIn("recentThreadTargets", expression)
        self.assertIn("takeSendReport", expression)
        report = {"outcome": "failed", "code": "scope", "at": 1700000000000.0, "threadId": target()["threadId"],
                  "observedBundle": "app-initial-69cd8dbddec5.js", "moduleError": "DesktopActionError",
                  "scopeError": "scope", "scopeSearch": {"fibers": 4242, "truncated": False},
                  "prompt": "secret prompt", "message": "secret text", "extra": {"nested": "secret"}}
        bridge.handle(reply(pipe, {"targets": [target()], "report": report}))
        reader.assert_called_once_with([target()])
        self.assertEqual(events, [{"event": "quick-composer", "stage": "send", "session": "window",
                                   "outcome": "failed", "code": "scope", "observedBundle": "app-initial-69cd8dbddec5.js",
                                   "moduleError": "DesktopActionError", "scopeError": "scope",
                                   "threadId": target()["threadId"],
                                   "scopeSearch": {"fibers": 4242, "truncated": False}, "at": 1700000000000}])
        self.assertNotIn("secret", json.dumps(events))
        # A bare list (older composer) and a cycle without a report log nothing.
        bridge.next_poll = 0
        bridge.refresh({"window"})
        bridge.handle(reply(pipe, [target()]))
        bridge.next_poll = 0
        bridge.refresh({"window"})
        bridge.handle(reply(pipe, {"targets": [], "report": None}))
        self.assertEqual(len(events), 1)
        # An invalid outcome or a non-UUID thread id is dropped, never logged.
        bridge.next_poll = 0
        bridge.refresh({"window"})
        bridge.handle(reply(pipe, {"targets": [], "report": {"outcome": "weird", "code": "x"}}))
        bridge.next_poll = 0
        bridge.refresh({"window"})
        bridge.handle(reply(pipe, {"targets": [], "report": {"outcome": "sent", "threadId": "not-a-uuid"}}))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1], {"event": "quick-composer", "stage": "send", "session": "window", "outcome": "sent"})

    def test_window_mode_polls_with_the_window_state_opens_on_request_and_relays_rows(self):
        pipe, events = FakeTransport(), []
        reader = mock.Mock(return_value=[{**target(1), "preview": "latest reply"}])
        window = mock.Mock()
        window.is_open.return_value = False
        bridge = QuickComposerBridge(pipe, reader, events.append, window=window)
        bridge.refresh({"window"})
        expression = pipe.sent[-1]["params"]["expression"]
        self.assertIn("pollHost({ windowOpen: false })", expression)
        self.assertIn("recentThreadTargets", expression)
        # The launcher asked for the window; nothing was open, so no rows went anywhere.
        bridge.handle(reply(pipe, {"targets": [], "report": None, "windowRequest": True}))
        window.open.assert_called_once_with()
        window.update.assert_not_called()
        reader.assert_not_called()
        # With the window open, the poll says so, previews are read for the rows
        # and the window gets the rows with their titles, activity and previews.
        window.is_open.return_value = True
        bridge.next_poll = 0
        bridge.refresh({"window"})
        self.assertIn("pollHost({ windowOpen: true })", pipe.sent[-1]["params"]["expression"])
        rows = [{**target(1), "title": "First", "supported": True, "active": True, "activeAccent": "#705aff"},
                {**target(2, hostId="box", kind="remote"), "title": "Remote", "supported": False},
                {**target(3), "title": "", "supported": True, "activeAccent": "red"}]
        bridge.handle(reply(pipe, {"targets": rows, "report": None, "windowRequest": False}))
        reader.assert_called_once_with([target(1), target(3)])
        window.update.assert_called_once_with("window", [
            {**target(1), "title": "First", "supported": True, "active": True, "activeAccent": "#705aff", "preview": "latest reply"},
            {**target(2, hostId="box", kind="remote"), "title": "Remote", "supported": False, "active": False, "activeAccent": None, "preview": None},
            {**target(3), "title": "Untitled chat", "supported": True, "active": False, "activeAccent": None, "preview": None}])
        self.assertNotIn("latest reply", json.dumps(events))
        # An empty sidebar while the window is open clears the window too.
        bridge.handle(reply(pipe, {"applied": 2}))  # the previews write
        bridge.next_poll = 0
        bridge.refresh({"window"})
        bridge.handle(reply(pipe, {"targets": [], "report": None}))
        self.assertEqual(window.update.call_args_list[-1], mock.call("window", []))

    def test_the_hub_panel_takes_precedence_over_the_helper_window(self):
        pipe, events = FakeTransport(), []
        reader = mock.Mock(return_value=[])
        window, host = mock.Mock(), mock.Mock()
        window.is_open.return_value = False
        host.is_watching.return_value = True
        host.request_open.return_value = True
        bridge = QuickComposerBridge(pipe, reader, events.append, window=window, host=host)
        bridge.refresh({"window"})
        self.assertIn("pollHost({ windowOpen: true })", pipe.sent[-1]["params"]["expression"])
        bridge.handle(reply(pipe, {"targets": [{**target(1), "title": "First", "supported": True}], "report": None, "windowRequest": True}))
        host.request_open.assert_called_once_with()
        window.open.assert_not_called()
        host.update.assert_called_once_with("window", [{**target(1), "title": "First", "supported": True, "active": False,
                                                        "activeAccent": None, "preview": None}])
        window.update.assert_not_called()
        # With no hub listening the helper's own window takes the request.
        host.is_watching.return_value = False
        host.request_open.return_value = False
        bridge.handle(reply(pipe, {"applied": 1}))
        bridge.next_poll = 0
        bridge.refresh({"window"})
        self.assertIn("pollHost({ windowOpen: false })", pipe.sent[-1]["params"]["expression"])
        bridge.handle(reply(pipe, {"targets": [], "report": None, "windowRequest": True}))
        window.open.assert_called_once_with()

    def test_only_the_page_that_supplied_rows_may_clear_them(self):
        pipe, events = FakeTransport(), []
        reader = mock.Mock(return_value=[])
        host = mock.Mock()
        host.is_watching.return_value = True
        host.request_open.return_value = True
        bridge = QuickComposerBridge(pipe, reader, events.append, window=None, host=host)
        bridge.refresh({"main", "hidden"})
        sent = {m["sessionId"]: m["id"] for m in pipe.sent}
        bridge.handle({"id": sent["main"], "result": {"result": {"value": {"targets": [target(1)], "report": None}}}})
        self.assertEqual(host.update.call_args_list[-1][0][0], "main")
        self.assertEqual(len(host.update.call_args_list[-1][0][1]), 1)
        # A page without a sidebar answers with nothing: the rows stay.
        bridge.handle({"id": sent["hidden"], "result": {"result": {"value": {"targets": [], "report": None}}}})
        self.assertEqual(len(host.update.call_args_list), 1)
        # The sidebar page itself reporting nothing does clear them.
        bridge.handle(reply(pipe, {"applied": 1}))  # the previews write for main
        bridge.next_poll = 0
        bridge.refresh({"main", "hidden"})
        sent = {m["sessionId"]: m["id"] for m in pipe.sent[-2:]}
        bridge.handle({"id": sent["main"], "result": {"result": {"value": {"targets": [], "report": None}}}})
        self.assertEqual(host.update.call_args_list[-1], mock.call("main", []))
        self.assertEqual(len(host.update.call_args_list), 2)
        bridge.handle({"id": sent["hidden"], "result": {"result": {"value": {"targets": [], "report": None}}}})
        self.assertEqual(len(host.update.call_args_list), 2)

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
