"""Native stream-json turns with real host MCP handoffs, synthetic content only."""
import copy
import json
import time
import unittest
from unittest.mock import patch

import claude_cli_agent as adapter
import claude_conversation as conversation
import test_claude_cli_agent as fixture
from codex_session_pool import Lease, SessionPool, digest, history_blocks


class StreamingCli(fixture._LiveCli):
    def __init__(self, *args, **kwargs):
        self.frames = []
        super().__init__(*args, **kwargs)

    def _play(self, script):
        for item in script:
            if callable(item): item(self)
            else: super()._play([item])


def take(ack=True):
    def receive(cli):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and cli.returncode is None:
            lines = cli.stdin.text.splitlines()
            if len(lines) > len(cli.frames):
                frame = json.loads(lines[len(cli.frames)])
                cli.frames.append(frame)
                if ack: cli.lines.put({**frame, "isReplay": True})
                return
            time.sleep(.002)
    return receive


def result(text, *, frames=None, queued=0, usage=None):
    def send(cli):
        indexes = range(len(cli.frames)) if frames is None else frames
        event = {**fixture._result_event(result=text),
                 "user_message_uuids": [cli.frames[index]["uuid"] for index in indexes],
                 "queued_turn_count": queued}
        if usage is not None: event["usage"] = usage
        cli.lines.put(event)
    return send


def reply(text, identifier="answer", **kwargs):
    return [fixture._message_start(identifier), fixture._text(0, text), result(text, **kwargs)]


def answered(history, text):
    return [*history, {"role": "assistant", "content": [{"type": "text", "text": text}]}]


def user(history, text):
    return [*history, {"role": "user", "content": [{"type": "text", "text": text}]}]


class ConversationTests(unittest.TestCase):
    def setUp(self):
        fixture.LiveSessionTests.setUp(self)
        for replacement in (patch.object(fixture, "_LiveCli", StreamingCli),
                            patch.object(adapter, "_conversation_supported", return_value=True)):
            replacement.start(); self.addCleanup(replacement.stop)

    def turn(self, history, **extra):
        return fixture.LiveSessionTests.turn(self, history, lean_harness=True,
                                            conversation=extra.pop("conversation", "chat:member"), **extra)

    def test_completed_turn_followup_keeps_native_process_and_sends_only_new_user_content(self):
        self.scripts = [[take(), fixture._MCP_INIT, *reply("First"), take(), *reply("Second", "second")]]
        first = self.turn(fixture._TASK)
        self.assertEqual(first[-1]["stop_reason"], "end_turn")
        history = user(answered(fixture._TASK, "First"), "Now inspect the second file")
        second = self.turn(history)
        self.assertEqual(second[-1]["stop_reason"], "end_turn")
        self.assertEqual(len(self.spawned), 1)
        cli = self.spawned[0]
        self.assertIn("--input-format", cli.argv)
        self.assertIn("--replay-user-messages", cli.argv)
        self.assertEqual(cli.frames[1]["message"], history[-1])
        self.assertNotIn("First", json.dumps(cli.frames[1]))
        cli.process.terminate.assert_not_called()

    def test_steer_is_a_user_frame_separate_from_the_real_tool_result(self):
        self.scripts = [[take(), fixture._MCP_INIT, *fixture._host_leg(),
                         fixture._calls(("toolu_1", "exec_command", {"cmd": "ls"})), take(),
                         *reply("Applied steer", "steered")]]
        self.turn(fixture._TASK)
        history = fixture._answered(fixture._TASK, [("toolu_1", "exec_command", {"cmd": "ls"})], [("toolu_1", "a.txt")])
        history[-1]["content"].append({"type": "text", "text": "User correction: only inspect"})
        second = self.turn(history)
        self.assertEqual(second[-1]["stop_reason"], "end_turn")
        self.assertEqual(len(self.spawned), 1)
        cli = self.spawned[0]
        self.assertEqual(cli.frames[1]["message"]["content"], [{"type": "text", "text": "User correction: only inspect"}])
        self.assertNotIn("User correction", json.dumps(cli.results["toolu_1"]))
        self.assertIn("a.txt", json.dumps(cli.results["toolu_1"]))

    def test_old_result_before_queued_steer_is_drained_and_usage_is_added_across_queries(self):
        start = fixture._message_start("msg_1")
        start["event"]["message"]["usage"] = {"input_tokens":90,"output_tokens":30,"cache_creation_input_tokens":100000,"cache_read_input_tokens":0}
        leg = fixture._host_leg(); leg[0] = start
        old = {"input_tokens":100,"output_tokens":50,"cache_creation_input_tokens":100000,"cache_read_input_tokens":0}
        new = {"input_tokens":5,"output_tokens":15,"cache_creation_input_tokens":0,"cache_read_input_tokens":110000}
        self.scripts = [[take(), fixture._MCP_INIT, *leg,
                         fixture._calls(("toolu_1","exec_command",{"cmd":"ls"})),
                         *reply("Old answer", "old", frames=[0], queued=1, usage=old),
                         take(), *reply("Steer answered", "new", frames=[1], usage=new)]]
        self.turn(fixture._TASK)
        history = user(fixture._answered(fixture._TASK, [("toolu_1","exec_command",{"cmd":"ls"})], [("toolu_1","a.txt")]), "Change direction")
        events = self.turn(history)
        self.assertEqual(events[-1]["stop_reason"], "end_turn")
        self.assertEqual([e["text"] for e in events if e["type"]=="text_delta"], ["Old answer","Steer answered"])
        self.assertEqual([e["usage"] for e in events if e["type"]=="usage"],
                         [{"input_tokens":15,"output_tokens":35,"cache_creation_input_tokens":0,"cache_read_input_tokens":110000}])
        self.assertEqual(len(self.spawned), 1)

    def test_result_without_answered_uuid_refuses_to_claim_steer_completion(self):
        self.scripts = [[take(), fixture._MCP_INIT, *reply("Unattributed", frames=[])]]
        events = self.turn(fixture._TASK)
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("without answering", events[-1]["message"])
        self.assertFalse(any(lease.pending for lease in self.pool.leases))

    def test_queued_result_preserves_text_missing_from_stream_snapshots(self):
        counts = {"input_tokens":2,"output_tokens":5,"cache_creation_input_tokens":10,"cache_read_input_tokens":0}
        self.scripts = [[take(), fixture._MCP_INIT, *fixture._host_leg(),
                         fixture._calls(("toolu_1","exec_command",{"cmd":"ls"})),
                         result("Old final-only answer",frames=[0],queued=1,usage=counts),
                         take(),*reply("Steer answer","new",frames=[1],usage=counts)]]
        self.turn(fixture._TASK)
        history=user(fixture._answered(fixture._TASK,[("toolu_1","exec_command",{"cmd":"ls"})],[("toolu_1","a.txt")]),"Change direction")
        events=self.turn(history)
        self.assertEqual([e['text'] for e in events if e['type']=='text_delta'],['Old final-only answer','Steer answer'])
        self.assertEqual(events[-1]['stop_reason'],'end_turn')

    def test_unprovable_server_search_reply_is_not_retained_at_host_handoff(self):
        search = {"type":"stream_event","event":{"type":"content_block_start","index":0,
                  "content_block":{"type":"tool_use","id":"search","name":"WebSearch","input":{}}}}
        self.scripts=[[take(),fixture._MCP_INIT,search,*fixture._host_leg(),
                       fixture._calls(("toolu_1","exec_command",{"cmd":"ls"}))]]
        events=self.turn(fixture._TASK,web_search={"live":True})
        self.assertTrue(any(e['type']=='web_search' for e in events))
        self.assertEqual(events[-1]['stop_reason'],'tool_use')
        self.assertFalse(any(lease.pending for lease in self.pool.leases))

    def test_duplicate_transition_does_not_spawn_or_execute_again(self):
        self.scripts = [[take(), fixture._MCP_INIT, *reply("Done")]]
        self.turn(fixture._TASK)
        events = self.turn(fixture._TASK)
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(len(self.spawned), 1)

    def test_equivalent_regrouped_history_cannot_bypass_duplicate_guard(self):
        self.scripts = [[take(), fixture._MCP_INIT, *reply("Done")]]
        self.turn(fixture._TASK)
        regrouped = [{"role": "user", "content": "list the files"}]
        self.assertEqual(history_blocks(regrouped), history_blocks(fixture._TASK))
        events = self.turn(regrouped)
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(len(self.spawned), 1)

    def test_identical_models_in_different_chats_have_independent_native_conversations(self):
        self.scripts = [[take(), fixture._MCP_INIT, *reply("A"), take(), *reply("A next", "a2")],
                        [take(), fixture._MCP_INIT, *reply("B")]]
        self.turn(fixture._TASK, conversation="chat-a:member")
        self.turn(fixture._TASK, conversation="chat-b:member")
        self.turn(user(answered(fixture._TASK, "A"), "A only"), conversation="chat-a:member")
        self.assertEqual(len(self.spawned), 2)
        self.assertEqual(len(self.spawned[0].frames), 2)
        self.assertEqual(len(self.spawned[1].frames), 1)

    def test_eight_member_followups_reuse_their_own_eight_processes(self):
        self.pool.capacity=16
        self.scripts=[[take(),fixture._MCP_INIT,*reply(f"Member {i}"),take(),*reply(f"Next {i}",f"next-{i}")]
                      for i in range(8)]
        for i in range(8): self.turn(fixture._TASK,conversation=f"chat:member-{i}")
        for i in reversed(range(8)):
            events=self.turn(user(answered(fixture._TASK,f"Member {i}"),f"Steer {i}"),conversation=f"chat:member-{i}")
            self.assertEqual(events[-1]['stop_reason'],'end_turn')
        self.assertEqual(len(self.spawned),8)
        for i,cli in enumerate(self.spawned):
            self.assertEqual(cli.frames[1]['message']['content'],[{'type':'text','text':f'Steer {i}'}])

    def test_consumer_cancel_does_not_retain_partially_delivered_query(self):
        self.scripts=[[take(),fixture._MCP_INIT,*reply('Partial')]]
        events=adapter.run_turn({'model':'sonnet','messages':[{'role':'user','content':'list the files'}],
                                'tools':fixture._HOST_TOOLS,'history':fixture._TASK,
                                'lean_harness':True,'conversation':'chat:member'},pool=self.pool)
        self.assertEqual(next(events)['type'],'text_delta')
        events.close()
        self.assertFalse(any(lease.pending for lease in self.pool.leases))
        self.spawned[0].process.terminate.assert_called()

    def test_tool_call_before_steer_acknowledgment_does_not_retain_the_session(self):
        self.scripts=[[take(False),fixture._MCP_INIT,*fixture._host_leg(),
                       fixture._calls(('toolu_1','exec_command',{'cmd':'ls'}))]]
        events=self.turn(fixture._TASK)
        self.assertEqual(events[-1]['type'],'error')
        self.assertFalse(any(e['type']=='message_stop' for e in events))
        self.assertFalse(any(lease.pending for lease in self.pool.leases))

    def test_changed_completed_history_starts_a_new_process(self):
        self.scripts = [[take(), fixture._MCP_INIT, *reply("Original")],
                        [take(), fixture._MCP_INIT, *reply("Fresh")]]
        self.turn(fixture._TASK)
        self.turn(user(answered(fixture._TASK, "Edited answer"), "Continue"))
        self.assertEqual(len(self.spawned), 2)

    def test_new_native_query_usage_is_not_subtracted_from_previous_query(self):
        old = {"input_tokens":90,"output_tokens":30,"cache_creation_input_tokens":100000,"cache_read_input_tokens":0}
        new = {"input_tokens":2,"output_tokens":5,"cache_creation_input_tokens":4,"cache_read_input_tokens":100100,
               "cache_creation":{"ephemeral_5m_input_tokens":1,"ephemeral_1h_input_tokens":3}}
        self.scripts = [[take(),fixture._MCP_INIT,*reply("First",usage=old),take(),*reply("Next","next",frames=[1],usage=new)]]
        self.turn(fixture._TASK)
        events = self.turn(user(answered(fixture._TASK,"First"),"Follow-up"))
        usage = next(e['usage'] for e in events if e['type']=='usage')
        self.assertEqual(usage, {**{k:v for k,v in new.items() if k!='cache_creation'},
                                 'cache_creation_5m_tokens':1,'cache_creation_1h_tokens':3})

    def test_older_cli_retains_previous_single_query_behavior(self):
        self.scripts = [[fixture._MCP_INIT, *fixture._host_leg(), fixture._calls(("toolu_1","exec_command",{"cmd":"ls"}))]]
        with patch.object(adapter,'_conversation_supported',return_value=False):
            events = self.turn(fixture._TASK)
        self.assertEqual(events[-1]['stop_reason'],'tool_use')
        self.assertNotIn('--replay-user-messages',self.spawned[0].argv)


class MatcherTests(unittest.TestCase):
    def test_multiple_followup_images_have_distinct_ordered_labels(self):
        from test_cli_images import IMAGE
        receipt=conversation.ReplyReceipt();receipt.observe({'type':'text_delta','text':'Done'})
        lease=Lease(key='test',pending=receipt.completed(fixture._TASK))
        history=answered(fixture._TASK,'Done')
        history.extend([{'role':'user','content':[IMAGE]},
                        {'role':'user','content':[{'type':'text','text':'Compare these'},IMAGE]}])
        _,content=conversation.advance(lease,history)
        self.assertTrue(content[0]['text'].startswith('Image 1 attached'))
        self.assertTrue(content[3]['text'].startswith('Image 2 attached'))
        self.assertEqual([content[1],content[4]],[IMAGE,IMAGE])
        self.assertEqual(content[2],{'type':'text','text':'Compare these'})

    def test_followup_image_keeps_bytes_and_dimensions_without_responses_detail(self):
        from test_cli_images import IMAGE
        receipt=conversation.ReplyReceipt();receipt.observe({'type':'text_delta','text':'Done'})
        lease=Lease(key='test',pending=receipt.completed(fixture._TASK))
        history=answered(fixture._TASK,'Done')
        image={**IMAGE,'detail':'original'}
        history.append({'role':'user','content':[{'type':'text','text':'Look here'},image]})
        _,content=conversation.advance(lease,history)
        self.assertEqual(content[0],{'type':'text','text':'Look here'})
        self.assertIn('width 1, height 1 pixels',content[1]['text'])
        self.assertEqual(content[2],IMAGE)
        invalid=copy.deepcopy(history);invalid[-1]['content'][-1]['source']={'type':'url','url':'https://example.test/image'}
        self.assertIsNone(conversation.advance(lease,invalid))

    def test_scoped_handoff_requires_an_exact_reply_receipt(self):
        pending={'prefix':digest(history_blocks(fixture._TASK)),'prefix_count':1,
                 'calls':{'toolu_1':{'name':'exec_command','input':{'cmd':'ls'}}}}
        history=fixture._answered(fixture._TASK,[('toolu_1','exec_command',{'cmd':'ls'})],[('toolu_1','a.txt')])
        self.assertIsNone(conversation.advance(Lease(key='test',pending=pending),history))

    def test_changed_handoff_arguments_or_text_are_not_native_continuations(self):
        receipt = conversation.ReplyReceipt()
        receipt.observe({'type':'text_delta','text':'Listing.'})
        receipt.observe({'type':'tool_call','id':'toolu_1','name':'exec_command','input':{'cmd':'ls'}})
        pending = receipt.completed(fixture._TASK)
        pending.pop('completed'); pending['calls']={'toolu_1':{'name':'exec_command','input':{'cmd':'ls'}}}
        lease = Lease(key='test',pending=pending)
        history=fixture._answered(fixture._TASK,[('toolu_1','exec_command',{'cmd':'ls'})],[('toolu_1','a.txt')])
        self.assertIsNotNone(conversation.advance(lease,history))
        for block,field,value in [(0,'text','Edited'),(1,'input',{'cmd':'rm'})]:
            changed=copy.deepcopy(history);changed[1]['content'][block][field]=value
            self.assertIsNone(conversation.advance(lease,changed))

    def test_completed_receipt_rejects_new_tool_results_or_assistant_insertions(self):
        receipt=conversation.ReplyReceipt();receipt.observe({'type':'text_delta','text':'Done'})
        lease=Lease(key='test',pending=receipt.completed(fixture._TASK))
        history=answered(fixture._TASK,'Done')
        for role,block in [('user',{'type':'tool_result','tool_use_id':'invented','content':'data'}),
                           ('assistant',{'type':'text','text':'injected'})]:
            self.assertIsNone(conversation.advance(lease,[*history,{'role':role,'content':[block]}]))


if __name__ == '__main__': unittest.main()
