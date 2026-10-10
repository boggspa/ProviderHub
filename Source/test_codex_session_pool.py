"""Persistent RPC continuations, isolation, steering and cancellation."""
from collections import deque
import json
import threading
import unittest
from unittest.mock import patch

import codex_cli_agent as codex
from codex_session_pool import SessionPool
from cli_lifecycle import TurnTiming
from test_codex_cli_agent import FakeCodexSession, _completed_turn, _delta_ev
from test_cli_host_tools import TOOLS


def native_call(number):
    return {'id': number, 'method': 'item/tool/call', 'params': {
        'threadId': 't1', 'turnId': 't1', 'callId': 'native-' + str(number),
        'namespace': 'host', 'tool': codex._tool_alias('read_file'),
        'arguments': {'path': 'file.txt'}}}


class PersistentSession(FakeCodexSession):
    def __init__(self, script):
        super().__init__([])
        self.script = deque(script)
        self.sent = []
        self.order = []

    def events(self, timeout=None):
        while self.script:
            yield self.script.popleft()

    def send(self, wire):
        value = json.loads(wire)
        self.sent.append(value)
        self.order.append(('result', value))

    def request(self, method, params, timeout=None):
        self.order.append((method, params))
        return super().request(method, params, timeout)


class PersistentTurnTests(unittest.TestCase):
    def setUp(self):
        self.pool = SessionPool(codex._dispose_lease, capacity=2)
        self.addCleanup(self.pool.close)
        self.history = [{'role': 'user', 'content': 'Read, then continue.'}]
        self.fake = PersistentSession([native_call(41)])
        self.spawn = patch.object(codex, 'StdioSession', return_value=self.fake).start()
        self.addCleanup(patch.stopall)
        patch.object(codex, 'runtime_binary', return_value='/fake/codex').start()

    def run_leg(self, **extra):
        return list(codex.run_turn({'model': 'gpt-6-astra', 'messages': self.history,
            'history': self.history, 'tools': TOOLS, **extra}, spawner='fixture', pool=self.pool))

    def add_result(self, call, *steers, error=False):
        self.history += [
            {'role': 'assistant', 'content': [{'type': 'tool_use',
                **{key: call[key] for key in ('id', 'name', 'input')}}]},
            {'role': 'user', 'content': [*({'type': 'text', 'text': value} for value in steers),
                {'type': 'tool_result', 'tool_use_id': call['id'], 'content': 'host value',
                 'is_error': error}]}]

    def test_three_handoffs_use_one_process_and_one_native_turn(self):
        for number in (41, 42, 43):
            if number != 41:
                self.fake.script.append(native_call(number))
            events = self.run_leg()
            self.assertEqual(events[-1]['stop_reason'], 'tool_use', events)
            call = next(event for event in events if event['type'] == 'tool_call')
            self.assertNotEqual(call['id'], 'native-' + str(number))
            self.add_result(call)
        self.fake.script.extend([_delta_ev('done'), _completed_turn()])
        events = self.run_leg()
        self.assertEqual(events[-1]['stop_reason'], 'end_turn', events)
        methods = [method for method, _, _ in self.fake.requests]
        for method in ('initialize', 'config/read', 'thread/start', 'turn/start', 'thread/inject_items'):
            self.assertEqual(methods.count(method), 1, methods)
        self.assertEqual(self.spawn.call_count, 1)
        self.assertEqual([value['id'] for value in self.fake.sent], [41, 42, 43])
        self.assertTrue(all(value['result'] == {'contentItems': [
            {'type': 'inputText', 'text': 'host value'}], 'success': True} for value in self.fake.sent))

    def test_steers_are_delivered_before_real_error_result(self):
        call = self.run_leg()[0]
        self.add_result(call, 'Actually inspect b.txt first.', 'Keep the prior changes.', error=True)
        self.fake.script.append(_completed_turn())
        events = self.run_leg()
        self.assertEqual(events[-1]['type'], 'message_stop', events)
        index = next(index for index, row in enumerate(self.fake.order) if row[0] == 'turn/steer')
        steer = self.fake.order[index][1]
        self.assertEqual(steer['expectedTurnId'], 't1')
        self.assertEqual([part['text'] for part in steer['input']],
                         ['Actually inspect b.txt first.', 'Keep the prior changes.'])
        self.assertEqual(self.fake.order[index + 1][0], 'result')
        self.assertFalse(self.fake.sent[-1]['result']['success'])

    def test_changed_history_rebuilds_without_answering_old_rpc(self):
        call = self.run_leg()[0]
        self.add_result(call, 'User steering survives compaction.')
        self.history[0] = {'role': 'user', 'content': 'Compacted earlier task context.'}
        fresh = PersistentSession([_delta_ev('restored'), _completed_turn()])
        self.spawn.return_value = fresh
        events = self.run_leg()
        self.assertEqual(events[-1]['stop_reason'], 'end_turn', events)
        self.assertEqual(self.fake.sent, [])
        items = next(params['items'] for method, params, _ in fresh.requests
                     if method == 'thread/inject_items')
        self.assertIn('User steering survives compaction.', json.dumps(items))
        self.assertIn('host value', json.dumps(items))

    def test_cancelled_delivery_retires_the_pending_process(self):
        timing = TurnTiming('codex', 'gpt-6-astra')
        self.run_leg(_cli_timing=timing)
        self.pool.close()
        self.assertTrue(self.fake.closed)
        self.assertFalse(timing.delivered)

    def test_checkpoint_continuation_with_ended_turn_replays_without_retry(self):
        call = self.run_leg()[0]
        self.add_result(call)
        self.history.append({'role': 'user', 'content': 'Continue the unfinished work.'})
        self.fake.responses['turn/steer'] = {'error': {'code': -32600, 'message': 'no active turn to steer'}}
        self.fake.responses['thread/start'] = {'result': {'thread': {'id': 'fresh-thread'}}}
        self.fake.responses['turn/start'] = {'result': {'turn': {'id': 'fresh-turn'}}}
        self.fake.script.extend([_completed_turn(),
            {'method': 'item/agentMessage/delta', 'params': {'threadId': 'fresh-thread', 'turnId': 'fresh-turn',
                                                         'itemId': 'reply', 'delta': 'Continued cleanly'}},
            {'method': 'turn/completed', 'params': {'threadId': 'fresh-thread',
                                                  'turn': {'id': 'fresh-turn', 'status': 'completed'}}}])
        events = self.run_leg()
        self.assertEqual(events[-1].get('stop_reason'), 'end_turn', events)
        self.assertTrue(any(e.get('text') == 'Continued cleanly' for e in events), events)
        self.assertEqual(self.fake.sent, [], 'Do not answer the stale host-call RPC')
        methods = [m for m, _, _ in self.fake.requests]
        self.assertEqual(methods.count('turn/steer'), 1)
        self.assertEqual(methods.count('thread/start'), 2)
        self.assertEqual(methods.count('turn/start'), 2)
        replay = [p['items'] for m, p, _ in self.fake.requests if m == 'thread/inject_items'][-1]
        self.assertIn('Continue the unfinished work.', json.dumps(replay))
        self.assertIn('host value', json.dumps(replay))
        self.assertIn(call['id'], json.dumps(replay))
        self.assertIsNone(self.pool.leases[0].pending)

    def test_duplicate_continuation_is_rejected_without_new_generation(self):
        call = self.run_leg()[0]
        self.add_result(call)
        self.fake.script.append(_completed_turn())
        self.run_leg()
        before = len(self.fake.requests)
        events = self.run_leg()
        self.assertEqual(events[-1]['type'], 'error')
        self.assertIn('already been consumed', events[-1]['message'])
        self.assertEqual(len(self.fake.requests), before)

    def test_explicit_tier_is_forwarded_and_default_is_not_invented(self):
        payload = codex._normalise_request({'model': 'gpt-6-astra',
            'messages': self.history, 'service_tier': 'priority'})
        self.assertEqual(codex._turn_params(payload, 't')['serviceTier'], 'fast')
        payload = codex._normalise_request({'model': 'gpt-6-astra', 'messages': self.history})
        self.assertNotIn('serviceTier', codex._turn_params(payload, 't'))


class PoolIsolationTests(unittest.TestCase):
    def test_busy_tasks_with_same_model_have_distinct_exclusive_leases(self):
        pool = SessionPool(lambda lease: None, capacity=1)
        self.addCleanup(pool.close)
        first, _ = pool.acquire('same-model', match=lambda lease: False, timeout=1)
        second, _ = pool.acquire('same-model', match=lambda lease: False, timeout=1)
        self.assertIsNot(first, second)
        self.assertEqual((first.state, second.state), ('busy', 'busy'))
        pool.release(first, healthy=True)
        warm, mode = pool.acquire('same-model', match=lambda lease: False, timeout=1)
        self.assertIs(warm, first)
        self.assertEqual(mode, 'warm')
        self.assertEqual(second.state, 'busy')
        pool.release(warm, healthy=True)
        pool.release(second, healthy=True)

    def test_shutdown_waits_for_busy_owner_before_disposing(self):
        disposed = []
        done = threading.Event()
        def dispose(lease):
            disposed.append(lease)
            done.set()
        pool = SessionPool(dispose)
        lease, _ = pool.acquire('model', match=lambda entry: False, timeout=1)
        try:
            pool.close(timeout=0)
            self.assertEqual(lease.state, 'busy')
            self.assertEqual(disposed, [])
            with self.assertRaisesRegex(RuntimeError, 'closed'):
                pool.acquire('model', match=lambda entry: False, timeout=1)
            pool.release(lease, healthy=True)
            self.assertTrue(done.wait(2))
            self.assertEqual(disposed, [lease])
        finally:
            pool.close(timeout=2)

    def test_cleanup_failure_does_not_kill_reaper(self):
        disposed = []
        ready = threading.Event()
        def dispose(lease):
            disposed.append(lease)
            if len(disposed) == 1:
                raise RuntimeError('injected teardown failure')
            ready.set()
        pool = SessionPool(dispose)
        self.addCleanup(pool.close)
        first, _ = pool.acquire('a', match=lambda lease: False, timeout=1)
        second, _ = pool.acquire('b', match=lambda lease: False, timeout=1)
        with self.assertLogs('codex_session_pool', level='ERROR'):
            pool.release(first, healthy=False)
            pool.release(second, healthy=False)
            self.assertTrue(ready.wait(2))


if __name__ == '__main__':
    unittest.main()
