"""Observable streaming/cleanup order, rather than elapsed-time assumptions."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import cli_routes
import cli_structured_reply
from cli_lifecycle import CleanupQueue, ManagedTurn, TurnTiming, observe_events, cleanup_after_exit
from test_cli_host_tools import TOOLS
from test_cli_structured_reply import reply


class LifecycleTests(unittest.TestCase):
    def test_silent_session_observes_cancellation_without_waiting_for_output(self):
        from test_cli_session import FakeProcess, make_session
        fake = FakeProcess(script=None)
        session = make_session(fake)
        cancel = threading.Event()
        timing = TurnTiming('test', 'm')
        timing.cancel = cancel.is_set
        finished = threading.Event()
        errors = []
        def consume():
            try:
                list(observe_events(session.events(timeout=30), timing))
            except BrokenPipeError:
                errors.append('cancelled')
            finally:
                session.close()
                finished.set()
        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        try:
            cancel.set()
            self.assertTrue(finished.wait(2), 'silent CLI ignored cancellation')
            self.assertEqual(errors, ['cancelled'])
        finally:
            session.close()
            worker.join(2)

    def test_resources_wait_for_observed_child_exit(self):
        process = SimpleNamespace(returncode=None)
        process.poll = lambda: process.returncode
        done = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prompt.txt'
            path.write_text('held until exit')
            def release():
                path.unlink()
                done.set()
            cleanup_after_exit(SimpleNamespace(process=process), release)
            self.assertTrue(path.exists())
            self.assertFalse(done.is_set())
            process.returncode = 0
            self.assertTrue(done.wait(2))
            self.assertFalse(path.exists())

    def test_terminal_delivery_precedes_slow_cleanup_and_retains_files(self):
        entered, release, finished = (threading.Event() for _ in range(3))
        cleanup = CleanupQueue(workers=1, capacity=2)
        with tempfile.TemporaryDirectory() as directory:
            attachment = Path(directory) / 'image.png'
            attachment.write_bytes(b'image')
            order = []

            def events():
                try:
                    yield {'type': 'text_delta', 'text': 'ready'}
                    yield {'type': 'message_stop', 'stop_reason': 'end_turn'}
                    self.fail('adapter consumed past terminal')
                finally:
                    order.append('cleanup')
                    entered.set()
                    release.wait(3)
                    attachment.unlink()
                    finished.set()

            stream = ManagedTurn(events, TurnTiming('test', 'model'), 2, cleanup=cleanup)
            try:
                result = cli_routes.relay_cli_turn(stream,
                    lambda event: order.append(event['type']), model='model')
                self.assertIsNone(result['error'])
                self.assertTrue(entered.wait(2))
                self.assertLess(order.index('message_stop'), order.index('cleanup'))
                self.assertTrue(attachment.exists())
                self.assertFalse(finished.is_set())
            finally:
                release.set()
                self.assertTrue(finished.wait(2))
            self.assertFalse(attachment.exists())
            stream.close()
            self.assertEqual(order.count('cleanup'), 1)

    def test_cancelled_wire_still_cleans_generator(self):
        done = threading.Event()
        timing = TurnTiming('test', 'model')
        def events():
            try:
                yield {'type': 'text_delta', 'text': 'hello'}
            finally:
                done.set()
        def emit(event):
            raise BrokenPipeError()
        stream = ManagedTurn(events, timing, 2, cleanup=CleanupQueue(workers=1, capacity=1))
        with self.assertRaises(BrokenPipeError):
            cli_routes.relay_cli_turn(stream, emit, model='model')
        self.assertTrue(done.wait(2))
        self.assertFalse(timing.delivered)

    def test_capacity_is_reserved_before_another_child_starts(self):
        cleanup = CleanupQueue(workers=1, capacity=1)
        entered, release, done = (threading.Event() for _ in range(3))
        calls = []
        def events():
            calls.append(True)
            try:
                yield {'type': 'message_stop', 'stop_reason': 'end_turn'}
            finally:
                entered.set()
                release.wait(3)
                done.set()
        first = ManagedTurn(events, TurnTiming('test', 'm'), 2, cleanup=cleanup)
        next(first)
        first.close()
        try:
            self.assertTrue(entered.wait(2))
            second = ManagedTurn(events, TurnTiming('test', 'm'), .01, cleanup=cleanup)
            self.assertEqual(next(second)['type'], 'error')
            self.assertEqual(len(calls), 1)
        finally:
            release.set()
            self.assertTrue(done.wait(2))

    def test_native_text_is_immediate_but_calls_still_validate(self):
        consumed = []
        def events(request, **kwargs):
            consumed.append('text')
            yield {'type': 'text_delta', 'text': 'working', 'source_id': 'part'}
            consumed.append('tool')
            yield {'type': 'tool_call', 'id': 'call', 'name': 'unoffered', 'input': {}}
            yield {'type': 'message_stop', 'stop_reason': 'tool_use'}
        adapter = SimpleNamespace(HOST_TOOL_TRANSPORT='dynamic', run_turn=events)
        stream = cli_routes._tool_turn(adapter, {'tools': TOOLS}, timeout=2)
        first = next(stream)
        self.assertEqual(first, {'type': 'text_delta', 'text': 'working', 'source_id': 'part'})
        self.assertEqual(consumed, ['text'])
        tail = list(stream)
        self.assertEqual([event['type'] for event in tail], ['error'])
        self.assertEqual(tail[0]['code'], 'invalid_cli_tool_call')

    def test_muse_valid_object_does_not_wait_for_or_consume_terminal(self):
        closed = []
        wire = reply(arguments={'path': 'quote-"-brace-}-slash-\\.txt'})
        def events():
            try:
                for char in wire:
                    yield {'type': 'text_delta', 'text': char}
                self.fail('read past the first complete object')
            finally:
                closed.append(True)
        parsed = list(cli_structured_reply.parse_stream(events(), tools=TOOLS, stop_after_object=True))
        self.assertEqual(parsed[-1]['stop_reason'], 'tool_use')
        self.assertEqual(next(event for event in parsed if event['type'] == 'tool_call')['input'],
                         {'path': 'quote-"-brace-}-slash-\\.txt'})
        self.assertEqual(closed, [True])


if __name__ == '__main__':
    unittest.main()
