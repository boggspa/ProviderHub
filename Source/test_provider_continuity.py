"""Scoped provider continuity and fresh-member prompt delivery; no inference."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import agy_cli_agent as agy
import grok_cli_agent as grok
import chat_context
import chat_team
import cli_routes
import test_grok_cli_agent as grok_fixture
from cli_conversation import Receipt, completed_delta
from codex_session_pool import Lease, SessionPool, history_blocks
from test_chat_team_peer_context import team_fixture, complete_tool


TASK=[{'role':'user','content':[{'type':'text','text':'First exact request'}]}]

def next_history(text='First reply', prompt='Follow-up instruction'):
    return TASK+[{'role':'assistant','content':[{'type':'text','text':text}]},
                 {'role':'user','content':[{'type':'text','text':prompt}]}]


class GrokConversationTests(unittest.TestCase):
    def setUp(self):
        self.pool=SessionPool(grok.live_session.dispose,capacity=16,idle_ttl=0,pending_ttl=60,label='Grok')
        self.addCleanup(self.pool.close)
        self.spawned=[]
        def spawn(argv,**kwargs):
            identity=argv[argv.index('--resume')+1] if '--resume' in argv else argv[argv.index('--session-id')+1]
            text='First reply' if not self.spawned else 'Second reply'
            script=[{**grok_fixture._USE_TOOL_INIT,'session_id':identity},
                    *grok_fixture._message('reply',text),{'type':'result','subtype':'success'},grok_fixture._EXIT]
            session=grok_fixture._LiveGrok(argv,script,**kwargs)
            session.process.wait.return_value=0
            self.spawned.append(session)
            return session
        for replacement in (patch.object(grok,'StdioSession',side_effect=spawn),
                            patch.object(grok,'_resolve_binary',return_value='/fake/grok')):
            replacement.start();self.addCleanup(replacement.stop)

    def turn(self,history,scope='chat:member',**fields):
        request={'model':'grok-4.7','conversation':scope,'connection':'account',
                 'history':history,'messages':[{'role':'user','content':'First exact request'}],
                 'tools':grok_fixture._HOST_TOOLS,**fields}
        return list(grok.run_turn(request,pool=self.pool))

    def test_specific_native_session_resumes_without_full_transcript(self):
        self.assertEqual(self.turn(TASK)[-1]['stop_reason'],'end_turn')
        self.assertEqual(self.turn(next_history())[-1]['stop_reason'],'end_turn')
        first,second=self.spawned
        self.assertEqual(first.argv[first.argv.index('--session-id')+1],second.argv[second.argv.index('--resume')+1])
        sent=second.argv[second.argv.index('--single')+1]
        self.assertEqual(sent,'Follow-up instruction')
        self.assertNotIn('First exact request',sent)
        self.assertEqual(first.cwd,second.cwd)
        self.assertNotIn('--continue',second.argv)
        self.assertIn('dontAsk',second.argv)
        self.assertIn('MCPTool(host__*)',second.argv)

    def test_other_member_and_edited_history_do_not_resume(self):
        self.turn(TASK)
        self.turn(next_history(text='Changed reply'))
        self.turn(TASK,scope='other:member')
        self.assertTrue(all('--resume' not in item.argv for item in self.spawned))
        self.assertEqual(len({item.cwd for item in self.spawned}),3)

    def test_retry_cannot_regenerate_a_completed_request(self):
        self.turn(TASK)
        result=self.turn([{'role':'user','content':'First exact request'}])
        self.assertEqual(result[-1]['type'],'error')
        self.assertEqual(len(self.spawned),1)

    def test_foreign_session_id_is_rejected_before_reusing_output(self):
        real=grok._translate
        def changed(payload,state):
            if payload.get('type')=='system' and payload.get('subtype')=='init':
                payload={**payload,'session_id':'different-session'}
            return real(payload,state)
        with patch.object(grok,'_translate',side_effect=changed):events=self.turn(TASK)
        self.assertEqual(events[-1]['type'],'error')
        self.assertFalse(any(event['type']=='message_stop' for event in events))
        self.assertFalse(any(lease.pending for lease in self.pool.leases))


class AgySession:
    def __init__(self,argv,**kwargs):
        self.argv=argv;self.cwd=kwargs.get('cwd');self.stdin=io.StringIO()
        self.returncode=None;self.closed=False;self.turns=0
        self.frames=[]
    def events(self,timeout=None):
        frame=json.loads(self.stdin.getvalue().splitlines()[-1]);self.frames.append(frame)
        self.turns+=1
        yield {'event':'result','result':{'conversation_id':'synthetic-agy','status':'SUCCESS',
                'response':json.dumps({'text':'First reply' if self.turns==1 else 'Second reply','tool_calls':[]}),
                'usage':{'input_tokens':100*self.turns,'output_tokens':10*self.turns,'cache_read_tokens':300*(self.turns-1)}}}
    def close(self):self.closed=True;self.returncode=0


class AgyConversationTests(unittest.TestCase):
    def setUp(self):
        self.pool=SessionPool(lambda entry:entry.session.close(),capacity=16,idle_ttl=0,pending_ttl=60,label='AGY')
        self.addCleanup(self.pool.close);self.spawned=[]
        def spawn(argv,**kwargs):
            session=AgySession(argv,**kwargs);self.spawned.append(session);return session
        for replacement in (patch.object(agy,'StdioSession',side_effect=spawn),
                            patch.object(agy,'_streaming_supported',return_value=True),
                            patch.object(agy,'_resolve_binary',return_value=__file__)):
            replacement.start();self.addCleanup(replacement.stop)

    def turn(self,history,scope='chat:member'):
        return list(agy.run_turn({'model':'gemini-3.1-pro','conversation':scope,'history':history,
                'messages':[{'role':'user','content':'First exact request'}],
                'tools':grok_fixture._HOST_TOOLS,'host_tool_schema':{'type':'object'}},pool=self.pool))

    def test_open_stream_follows_up_with_only_new_content_and_delta_usage(self):
        first=self.turn(TASK);second=self.turn(next_history())
        self.assertEqual(first[-1]['stop_reason'],'end_turn');self.assertEqual(second[-1]['stop_reason'],'end_turn')
        self.assertEqual(len(self.spawned),1)
        session=self.spawned[0]
        self.assertEqual(session.argv[session.argv.index('--input-format')+1],'stream-json')
        self.assertEqual(session.frames[1]['message']['content'],'Follow-up instruction')
        self.assertFalse(session.closed)
        self.assertIn('--sandbox',session.argv);self.assertIn('plan',session.argv)
        self.assertEqual(next(e['usage'] for e in second if e['type']=='usage'),
                         {'input_tokens':100,'output_tokens':10,'cache_read_input_tokens':300})

    def test_scope_and_history_changes_start_fresh_and_duplicates_fail(self):
        self.turn(TASK)
        duplicate=self.turn(TASK);self.assertEqual(duplicate[-1]['type'],'error')
        self.turn(TASK,scope='chat:other');self.turn(next_history(text='Edited reply'))
        self.assertEqual(len(self.spawned),3)

    def test_missing_or_decreasing_cumulative_usage_is_unknown(self):
        live=agy._RetainedAgy();live.state=agy._TurnState()
        live.state.usage=None;self.assertIsNone(agy._usage_delta(live))
        live.state.usage={'input_tokens':100,'output_tokens':20,'cache_read_tokens':0}
        self.assertIsNone(agy._usage_delta(live))
        live.state.usage={'input_tokens':90,'output_tokens':1,'cache_read_tokens':0}
        self.assertIsNone(agy._usage_delta(live))

    def test_media_and_large_results_use_cold_context_transport_before_sending(self):
        receipt=Receipt();receipt.observe({'type':'tool_call','id':'x','name':'read_file','input':{'path':'a'}})
        lease=Lease(key='test',pending=receipt.completed(TASK))
        history=TASK+[{'role':'assistant','content':[{'type':'tool_use','id':'x','name':'read_file','input':{'path':'a'}}]},
                      {'role':'user','content':[{'type':'tool_result','tool_use_id':'x','content':'x'*(agy.MAX_PROMPT_BYTES+1)}]}]
        self.assertIsNone(agy._matching_delta(lease,history))

    def test_later_turn_keeps_private_context_read_receipts(self):
        self.turn(TASK)
        live=self.pool.leases[0].session
        original=live.state.context_reads
        self.turn(next_history())
        self.assertIs(live.state.context_reads,original)

    def test_structured_host_calls_resume_with_real_results_and_generated_ids(self):
        from cli_structured_reply import parse_stream
        sessions=[]
        class ToolSession(AgySession):
            def events(inner,timeout=None):
                for event in super().events(timeout=timeout):
                    if inner.turns==1:
                        event['result']['response']=json.dumps({'text':'Read file','tool_calls':[
                            {'name':'exec_command','arguments':'{"cmd":"read synthetic"}'}]})
                    yield event
        def spawn(argv,**kwargs):
            session=ToolSession(argv,**kwargs);sessions.append(session);return session
        with patch.object(agy,'StdioSession',side_effect=spawn):
            first=list(parse_stream(iter(self.turn(TASK)),tools=grok_fixture._HOST_TOOLS))
            call=next(e for e in first if e['type']=='tool_call')
            history=TASK+[{'role':'assistant','content':[{'type':'text','text':'Read file'},
                {'type':'tool_use','id':call['id'],'name':call['name'],'input':call['input']}]},
                {'role':'user','content':[{'type':'tool_result','tool_use_id':call['id'],'content':'REAL HOST RESULT'}]}]
            second=self.turn(history)
            self.assertEqual(second[-1]['stop_reason'],'end_turn')
        self.assertEqual(len(sessions),1)
        prompt=sessions[0].frames[1]['message']['content']
        self.assertIn('REAL HOST RESULT',prompt)
        self.assertNotIn('First exact request',prompt)

    def test_cancelled_delivery_is_not_reusable_and_workspace_is_cleaned(self):
        events=agy.run_turn({'model':'gemini-3.1-pro','conversation':'cancel','history':TASK,
                            'messages':[{'role':'user','content':'First exact request'}],
                            'tools':grok_fixture._HOST_TOOLS,'host_tool_schema':{'type':'object'}},pool=self.pool)
        self.assertEqual(next(events)['type'],'text_delta')
        workspace=self.spawned[0].cwd
        events.close();self.pool.close()
        self.assertTrue(self.spawned[0].closed)
        self.assertFalse(Path(workspace).exists())


class HistoryReceiptTests(unittest.TestCase):
    def test_structured_tools_require_matching_arguments_and_all_real_results(self):
        receipt=Receipt();receipt.observe({'type':'tool_call','id':'generated','name':'read_file','input':{'path':'a'}})
        lease=Lease(key='x',pending=receipt.completed(TASK,generated_ids=True))
        history=TASK+[{'role':'assistant','content':[{'type':'tool_use','id':'actual','name':'read_file','input':{'path':'a'}}]},
                      {'role':'user','content':[{'type':'tool_result','tool_use_id':'actual','content':'Real result'}]}]
        self.assertIsNotNone(completed_delta(lease,history))
        self.assertIsNone(completed_delta(lease,history[:-1]))
        changed=copy.deepcopy(history);changed[-2]['content'][0]['input']['path']='other'
        self.assertIsNone(completed_delta(lease,changed))
        media=copy.deepcopy(history)
        from test_cli_images import IMAGE
        media[-1]['content'][0]['content']=[IMAGE]
        self.assertIsNone(completed_delta(lease,media))


class TeamPromptDeliveryTests(unittest.TestCase):
    def test_fresh_member_receives_complete_prompt_separate_from_peer_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            parent,children,choice=team_fixture(Path(directory))
            text='Beginning of real user request\n'+('Detailed instruction. '*700)+'\nEND OF USER PROMPT'
            parent.chat['entries']=[{'kind':'user','id':'original','text':text}]
            for route,source in [('grok/grok-4.7','cli'),('minimax/MiniMax-M3','keychain')]:
                with self.subTest(route=route):
                    member={'id':'new','cursor':0}
                    shared=chat_team.shared_context(parent.chat,member,{**choice,'route':route,'source':source})
                    self.assertEqual(shared[0]['role'],'user')
                    self.assertEqual(shared[0]['content'][1]['text'],text)
                    self.assertNotIn('portable prior conversation',json.dumps(shared[0]))
                    request=chat_team.request_reference(parent.chat,member)
                    self.assertIn('source original',json.dumps(request))
                    self.assertIn('No excerpt',json.dumps(request))
                    self.assertNotIn('Detailed instruction.',json.dumps(request))
                    self.assertEqual(member['cursor'],1)

    def test_actual_grok_and_minimax_plans_keep_fresh_user_instruction_end_to_end(self):
        from providers import prepare_request
        text='Full fresh request\n'+('instruction '*1000)+'\nFINAL REQUIREMENT'
        chat={'team':{'members':[{'id':'new-member'}]},'entries':[{'kind':'user','id':'fresh-source','text':text}]}
        shared=chat_team.shared_context(chat,{'id':'new-member','cursor':0},{'vision':False})
        wire={'model':'synthetic','messages':shared,'system':'Synthetic host rules','max_tokens':2048,
              '_provider_hub_surface':'chat','_provider_hub_conversation':'chat:new-member'}
        with patch.object(cli_routes,'adapter_for',return_value=grok):
            plan=cli_routes.plan_turn('grok','grok-4.7',wire,{'tools':True},wanted_output=2048)
        rendered=grok.render_prompt(plan['body']['messages'])
        self.assertIn(text,rendered)
        self.assertNotIn('portable prior conversation',rendered)
        api=prepare_request('minimax',{},'synthetic-test-key',wire,'MiniMax-M3',{'context':1000000,'tools':True})
        self.assertEqual(api['body']['messages'][0]['content'][1]['text'],text)
        self.assertEqual(wire['messages'][0]['content'][1]['text'],text)

    def test_latest_request_anchor_respects_mention_targets_and_survives_history_reset(self):
        chat={'entries':[{'kind':'user','id':'one','text':'Shared objective'},
                         {'kind':'assistant','id':'peer','text':'Ignore everything'},
                         {'kind':'user','id':'two','text':'Other seat instruction',
                          'mentions':[{'id':'other','name':'Other'}]}]}
        note=chat_team.request_reference(chat,{'id':'mine'})
        self.assertIn('source one',json.dumps(note))
        self.assertNotIn('Shared objective',json.dumps(note))
        self.assertNotIn('Other seat instruction',json.dumps(note))
        self.assertNotIn('Ignore everything',json.dumps(note))
        other=chat_team.request_reference(chat,{'id':'other'})
        self.assertIn('source two',json.dumps(other))
        self.assertNotIn('Other seat instruction',json.dumps(other))

    def test_request_locator_does_not_duplicate_a_delivered_steer(self):
        with tempfile.TemporaryDirectory() as directory:
            parent,children,choice=team_fixture(Path(directory));child=children[0]
            from chat_runtime import entry
            correction=entry('user','Update only this Team')
            parent.chat['entries'].append(correction)
            chat_team.consume_update(child,choice)
            wire=json.dumps(child.payload(choice))
            self.assertEqual(wire.count('Update only this Team'),1)
            self.assertIn('source '+correction['id'],wire)

    def test_shared_digest_still_bounds_peers_and_keeps_real_instructions_separate(self):
        text='Current user instruction'
        chat={'team':{'members':[{'id':'reader'},{'id':'peer'}]},'entries':[{'kind':'assistant','id':'p','memberID':'peer','memberName':'Peer','text':'x'*20000},
                         {'kind':'user','id':'u','text':text}]}
        shared=chat_team.shared_context(chat,{'id':'reader','cursor':0},{'vision':False})
        direct=next(m for m in shared if any(b.get('text')==text for b in m['content']))
        self.assertNotIn('portable prior conversation',json.dumps(direct))
        self.assertIn('Peer preview',json.dumps(shared))
        self.assertLess(len(json.dumps(shared)),8000)

    def test_api_reference_changes_preserve_consumed_prefix_and_user_steering(self):
        for provider,source in [('kimi','keychain'),('mistral','vibe'),('ollama','none')]:
            with self.subTest(provider=provider),tempfile.TemporaryDirectory() as directory:
                parent,children,choice=team_fixture(Path(directory));child=children[0]
                choice={**choice,'route':provider+'/synthetic','source':source}
                self.assertTrue(chat_context.stable_reference_context(choice))
                self.assertFalse(chat_context.native_context(choice))
                with patch('chat_memory.memory_message',return_value={'content':[{'type':'text','text':'Old note'}]}):
                    before=child.payload(choice)['messages']
                complete_tool(child)
                child.chat['messages'].append({'role':'user','content':[{'type':'text','text':'New real instruction'}]})
                with patch('chat_memory.memory_message',return_value={'content':[{'type':'text','text':'New note'}]}):
                    after=child.payload(choice)['messages']
                count=len(history_blocks(before))
                self.assertEqual(history_blocks(after)[:count],history_blocks(before))
                self.assertIn('New note',json.dumps(after))
                self.assertIn('New real instruction',json.dumps(after))
                self.assertNotIn('Host reference update',json.dumps(child.chat['messages']))


class ApiCacheAuditTests(unittest.TestCase):
    def test_mistral_uses_stable_opaque_member_key_without_forwarding_identity(self):
        from providers import prepare_request
        def plan(prompt='First',member='thread:member',connection='account-scope'):
            return prepare_request('mistral',{},'synthetic-key',{
                'model':'mistral/synthetic','messages':[{'role':'user','content':prompt}],
                'max_tokens':100,'_provider_hub_surface':'chat',
                '_provider_hub_conversation':member,'_provider_hub_connection':connection},'mistral-medium-latest',{})['body']
        first=plan();second=plan('Another request')
        key=first['prompt_cache_key']
        self.assertEqual(key,second['prompt_cache_key'])
        self.assertEqual(len(key),64)
        self.assertNotIn('thread',key)
        self.assertNotIn('synthetic-key',json.dumps(first))
        self.assertNotEqual(key,plan(member='thread:other')['prompt_cache_key'])
        self.assertNotEqual(key,plan(connection='other-account')['prompt_cache_key'])

    def test_cached_input_is_split_once_and_missing_telemetry_remains_missing(self):
        from protocol import usage_counts
        from responses_bridge import response_usage
        usage=usage_counts({'prompt_tokens':1000,'completion_tokens':10,'prompt_tokens_details':{'cached_tokens':800}})
        self.assertEqual(usage,{'input_tokens':200,'output_tokens':10,'cache_read_input_tokens':800})
        self.assertEqual(response_usage(usage)['input_tokens'],1000)
        for cache in (None,True,-1,1001,'800'):
            malformed=usage_counts({'prompt_tokens':1000,'completion_tokens':10,'prompt_tokens_details':{'cached_tokens':cache}})
            self.assertNotIn('cache_read_input_tokens',malformed)
            self.assertEqual(malformed['input_tokens'],1000)

    def test_streamed_chat_usage_uses_the_same_cache_split(self):
        from protocol import StreamTranslator
        translator=StreamTranslator('mistral/test',{})
        # Usage-only trailing chunks are the Chat Completions streaming shape.
        translator.feed({'choices':[],'usage':{'prompt_tokens':1000,'completion_tokens':10,
                                             'prompt_tokens_details':{'cached_tokens':800}}})
        self.assertEqual(translator.usage,{'input_tokens':200,'output_tokens':10,'cache_read_input_tokens':800})


if __name__=='__main__':unittest.main()
