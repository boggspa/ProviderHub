"""Peer references exercise production settlement and model boundaries."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import chat_team
import chat_team_mentions as mentions
import test_chat_team as harness_module
from test_chat_runtime import response
from test_chat_agents import call


class PeerMentionTests(unittest.TestCase):
    def fixture(self):
        members = [{'id': 'a', 'name': 'Alice', 'route': 'one', 'status': 'working'},
                   {'id': 'b', 'name': 'Bob', 'route': 'two', 'status': 'working', 'cursor': 1}]
        source = {'id': 'source', 'kind': 'assistant', 'text': '@Bob inspect the public result',
                  'memberID': 'a', 'memberName': 'Alice', 'contributionID': 'c', 'teamRunID': 'r',
                  'recorded': True, 'peerRoster': copy.deepcopy(members)}
        chat = {'entries': [source], 'team': {'members': members, 'runID': 'r', 'queue': []}}
        return chat, members, source

    def test_parser_code_email_escape_longest_and_utf16(self):
        _, members, _ = self.fixture()
        text = '😀 @bOB, email@Bob \\@Bob `@Bob`\n```\n@Bob\n```\n@Alice_x @Alice'
        found = mentions.resolve(text, members)
        self.assertEqual([x['id'] for x in found], ['b', 'a'])
        self.assertEqual(found[0]['start'], 3)
        self.assertEqual(mentions.resolve('@Bob', members + [dict(members[1], id='duplicate')]), [])

    def test_boundary_delivery_public_only_and_idempotent(self):
        chat, members, source = self.fixture()
        members[0].update(terminalStatus='ready', contributionID='c', contributions=0,
                          decision={'state': 'done', 'next_step': ''}, messages=[{'private': 'SECRET'}])
        chat_team.finish_contribution(chat, members[0])
        self.assertEqual(source['peerDeliveries'][0]['status'], 'pending')
        parent = SimpleNamespace(chat=chat, _mutex=__import__('threading').RLock())
        child = SimpleNamespace(team_parent=parent, team_member=members[1], chat={'messages': []}, save=Mock(), store=SimpleNamespace(dirty={}))
        chat_team.consume_update(child, {})
        payload = json.dumps(child.chat['messages'])
        self.assertIn('Public peer mention', payload)
        self.assertIn('source', payload)
        self.assertNotIn('SECRET', payload)
        self.assertEqual(source['peerDeliveries'][0]['status'], 'delivered')
        self.assertIn('source', child.store.dirty)
        first = copy.deepcopy(child.chat['messages'])
        chat_team.consume_update(child, {})
        chat_team.finish_contribution(chat, members[0])
        self.assertEqual(child.chat['messages'], first)

    def test_stopped_done_self_and_renamed_never_schedule(self):
        for status in ('done', 'stopped', 'interrupted', 'error', 'needs_input'):
            chat, members, source = self.fixture()
            members[1]['status'] = status
            source['text'] = '@Alice @Bob @Bob'
            mentions.record(chat, source)
            self.assertEqual(len(source['peerDeliveries']), 1)
            self.assertEqual(source['peerDeliveries'][0]['status'], 'unavailable')
            self.assertEqual(chat['team']['queue'], [])
        chat, members, source = self.fixture()
        mentions.record(chat, source)
        members[1]['name'] = 'Robert'
        self.assertEqual(mentions.pending(chat, members[1]), [])
        self.assertEqual(source['peerDeliveries'][0]['status'], 'stale')

    def test_budget_and_run_identity(self):
        chat, members, source = self.fixture()
        source['text'] = '@Bob ' + '😀' * 20000
        mentions.record(chat, source)
        self.assertLessEqual(len(mentions.body(source).encode('utf-8')), mentions.MAX_DELIVERY_BYTES)
        chat['team']['runID'] = 'new'
        self.assertEqual(mentions.pending(chat, members[1]), [])
        chat, members, source = self.fixture()
        for i in range(40):
            row = dict(source, id=str(i), peerRoster=copy.deepcopy(members))
            chat['entries'].append(row)
            mentions.record(chat, row)
        self.assertEqual(sum(d['status'] == 'pending' for r in chat['entries'] for d in r.get('peerDeliveries', [])), 32)

    def test_removed_recipient_and_unrecorded_sources(self):
        chat, members, source = self.fixture()
        mentions.record(chat, source)
        source['recorded'] = False
        self.assertEqual(mentions.pending(chat, members[1]), [])
        source['recorded'] = True
        chat['team']['members'] = [members[0]]
        self.assertEqual(mentions.pending(chat, members[0]), [])
        self.assertEqual(source['peerDeliveries'][0]['status'], 'stale')

    def test_multiple_references_defer_within_boundary_budget(self):
        chat, members, source = self.fixture()
        chat['entries'] = []
        for i in range(3):
            row = dict(source, id=str(i), text='@Bob ' + '😀' * 10000, peerRoster=copy.deepcopy(members))
            chat['entries'].append(row); mentions.record(chat, row)
        members[1]['cursor'] = 3
        first = chat_team.shared_context(chat, members[1], {})
        self.assertEqual(sum(d['status'] == 'pending' for r in chat['entries'] for d in r['peerDeliveries']), 1)
        second = chat_team.shared_context(chat, members[1], {})
        self.assertTrue(first and second)
        self.assertTrue(all(d['status'] == 'delivered' for r in chat['entries'] for d in r['peerDeliveries']))

    def test_real_service_does_not_restart_finished_peer(self):
        harness = harness_module.TeamTests(methodName='runTest')
        harness.setUp(); self.addCleanup(harness.doCleanups)
        parent = harness.service([[response('@Member 2 public finding')], [response('Finished')]])
        harness.configure(parent)
        harness.send(parent); harness.finish(parent)
        self.assertEqual([len(t.requests) for t in harness.transports], [1, 1])
        rows = [r for r in parent.chat['entries'] if r['kind'] == 'assistant']
        source = next(r for r in rows if r['text'].startswith('@'))
        self.assertEqual(source['mentions'][0]['name'], 'Member 2')
        self.assertEqual(len(source['peerDeliveries']), 1)
        self.assertEqual(parent.chat['team']['queue'], [])

    def test_real_running_peer_receives_at_next_round_and_receipt_persists(self):
        import threading
        import time
        harness = harness_module.TeamTests(methodName='runTest')
        harness.setUp(); self.addCleanup(harness.doCleanups)
        receiver_started = threading.Event()
        def sender(payload, cancel, delta):
            if not receiver_started.wait(2):
                raise AssertionError('Receiver did not start its first request')
            return response('@Member 2 ' + 'public finding ' * 500)
        def receiver(payload, cancel, delta):
            receiver_started.set()
            deadline = time.monotonic() + 2
            while parent.chat['team']['members'][0]['status'] != 'done':
                if time.monotonic() > deadline: raise AssertionError('Sender did not finish')
                time.sleep(.005)
            return call('team_status', {'state': 'done'})
        parent = harness.service([[sender],
                                  [receiver, response('Read the reference')]])
        members = harness.configure(parent)
        harness.send(parent); harness.finish(parent)
        self.assertEqual(len(harness.transports[1].requests), 2)
        first, second = (json.dumps(x) for x in harness.transports[1].requests)
        self.assertNotIn('Public peer mention', first)
        self.assertIn('Public peer mention', second)
        source = next(r for r in parent.chat['entries'] if r['kind'] == 'assistant' and r['memberID'] == members[0]['id'])
        self.assertEqual(source['peerDeliveries'][0]['status'], 'delivered')
        from chat_runtime import ChatStore
        reopened = ChatStore(harness.root, lock=False)
        saved = reopened.load(parent.chat['id'])
        saved_source = next(r for r in saved['entries'] if r['id'] == source['id'])
        self.assertEqual(saved_source['peerDeliveries'][0]['status'], 'delivered')


if __name__ == '__main__': unittest.main()
