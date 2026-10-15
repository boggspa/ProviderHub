"""Codex Responses continuation remains completed history on Claude's API."""
import copy
import http.client
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import test_gateway_hub as fixtures
from providers import prepare_request, ProviderError
from responses_bridge import to_messages, ReasoningEnvelope, append_claude_continuation, MessagesResponsesAdapter, CLAUDE_CONTINUATION
from bridge_core import BridgeError


def payload(tail=None, surface="responses"):
    return {"model":"claude/claude-opus-5-5", "max_tokens":512,
            "_provider_hub_surface":surface,
            "messages":[{"role":"user","content":"Inspect the synthetic project"},
                        tail or {"role":"assistant","content":[{"type":"text","text":"I have checked the first file."}]}]}


class AnthropicResponseTailTests(unittest.TestCase):
    def plan(self, body, provider="claude", model="claude-opus-5-5"):
        body=copy.deepcopy(body)
        if provider=='claude' and body.get('_provider_hub_surface')=='responses':
            append_claude_continuation(body['messages'])
        return prepare_request(provider, {}, "synthetic-test-key", body, model, {})

    def test_current_models_get_a_new_user_turn_without_reclassifying_assistant_history(self):
        for model in ("claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5-5", "claude-haiku-5-5"):
            with self.subTest(model=model):
                body=payload(); original=copy.deepcopy(body)
                plan=self.plan(body,model=model)
                self.assertEqual(body,original)
                self.assertEqual(plan['body']['messages'][:-1],original['messages'])
                self.assertEqual(plan['body']['messages'][-1]['role'],'user')
                self.assertIn('completed history',plan['body']['messages'][-1]['content'][0]['text'])
                self.assertEqual(plan['body']['messages'][-1]['content'][0]['text'],CLAUDE_CONTINUATION)

    def test_only_anthropic_responses_is_adapted(self):
        for provider,surface in (("claude","messages"),("claude","chat"),("claude",None),
                                 ("kimi","responses"),("mimo","responses")):
            with self.subTest(provider=provider,surface=surface):
                body=payload(surface=surface)
                plan=self.plan(body,provider=provider)
                self.assertEqual(plan['body']['messages'],body['messages'])
                self.assertNotIn('assistant_tail_continuation',plan['compatibility'])

    def test_user_tail_and_complete_tool_cycle_are_unchanged(self):
        body=payload()
        body['messages'].extend([
            {'role':'assistant','content':[{'type':'tool_use','id':'call-1','name':'read_file','input':{'path':'file'}}]},
            {'role':'user','content':[{'type':'tool_result','tool_use_id':'call-1','content':'file content'}]}])
        original=copy.deepcopy(body)
        plan=self.plan(body)
        self.assertEqual(plan['body']['messages'],original['messages'])
        self.assertNotIn('assistant_tail_continuation',plan['compatibility'])

    def test_missing_tool_result_is_rejected_without_inventing_one(self):
        body=payload({'role':'assistant','content':[{'type':'text','text':'Reading'},
                      {'type':'tool_use','id':'call-1','name':'read_file','input':{'path':'file'}}]})
        original=copy.deepcopy(body)
        with self.assertRaisesRegex(BridgeError,'missing tool results'): self.plan(body)
        self.assertEqual(body,original)

    def test_native_reasoning_and_trailing_developer_instruction_are_preserved(self):
        thinking={'type':'thinking','thinking':'Synthetic reasoning','signature':'synthetic-signature'}
        body=payload({'role':'assistant','content':[thinking,{'type':'text','text':'Ready to continue'}]})
        body['messages'].append({'role':'developer','content':'Only inspect; do not edit.'})
        original=copy.deepcopy(body)
        plan=self.plan(body)
        self.assertEqual(plan['body']['messages'][1],original['messages'][1])
        self.assertIn('Only inspect; do not edit.',json.dumps(plan['body']['system']))
        self.assertEqual(plan['body']['messages'][-1]['role'],'user')
        self.assertEqual(body,original)

    def test_replanning_does_not_stack_continuation_notes(self):
        first=self.plan(payload())
        again={**first['body'],'_provider_hub_surface':'responses'}
        second=self.plan(again)
        self.assertEqual(second['body']['messages'],first['body']['messages'])

    def test_commentary_and_compacted_response_items_reach_anthropic_with_user_tail(self):
        for phase in ('commentary','final_answer',None):
            with self.subTest(phase=phase):
                body={'stream':False,'store':False,'input':[
                    {'type':'message','role':'user','content':'Continue the synthetic task'},
                    {'type':'message','role':'assistant','phase':phase,'content':[
                        {'type':'output_text','text':'Completed work summary.'}]}]}
                translated=to_messages(body,'claude/claude-opus-5-5',{},None,'synthetic')
                translated['_provider_hub_surface']='responses'
                plan=self.plan(translated)
                self.assertEqual([m['role'] for m in plan['body']['messages']],['user','assistant','user'])
                self.assertEqual(plan['body']['messages'][1]['content'][0]['text'],'Completed work summary.')

    def test_responses_encrypted_reasoning_survives_tail_correction(self):
        with tempfile.TemporaryDirectory() as directory:
            envelope=ReasoningEnvelope(Path(directory))
            thinking={'type':'thinking','thinking':'Synthetic saved reasoning','signature':'native-signature'}
            token=envelope.seal([thinking],'claude-synthetic')
            body={'stream':False,'store':False,'input':[
                {'type':'message','role':'user','content':'Inspect only'},
                {'type':'reasoning','encrypted_content':token},
                {'type':'message','role':'assistant','content':'Inspection in progress'}]}
            translated=to_messages(body,'claude/claude-opus-5-5',{},envelope,'claude-synthetic')
            translated['_provider_hub_surface']='responses'
            plan=self.plan(translated)
            self.assertEqual(plan['body']['messages'][1]['content'][0],thinking)
            self.assertEqual(plan['body']['messages'][-1]['role'],'user')

    def test_real_responses_gateway_accepts_streaming_and_buffered_assistant_tails(self):
        for stream in (False,True):
            with self.subTest(stream=stream):
                fixture=fixtures.GatewayHubHTTPTests();fixture.setUp()
                try:
                    route=fixture.start_gateway('claude','claude-opus-5-5',{'context':200000})
                    def upstream(handler,body):
                        if body['messages'][-1]['role']!='user':
                            handler.send_json(400,{'type':'error','error':{'type':'invalid_request_error',
                                'message':'This model does not support assistant message prefill. The conversation must end with a user message.'}})
                        else:
                            content=[{'type':'thinking','thinking':'Synthetic new reasoning','signature':'new-signature'},
                                     {'type':'text','text':'Continuation accepted'}]
                            if body.get('stream'):
                                events=[{'type':'message_start','message':fixtures.native_message([],'end_turn')}]
                                for index,block in enumerate(content):
                                    events.extend([{'type':'content_block_start','index':index,'content_block':block},
                                                   {'type':'content_block_stop','index':index}])
                                events.extend([{'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'input_tokens':10,'output_tokens':5}},
                                               {'type':'message_stop'}])
                                handler.send_sse(events)
                            else: handler.send_json(200,fixtures.native_message(content,'end_turn'))
                    with patch.object(fixtures.MockProvider,'handle_native',upstream):
                        body={'model':route,'stream':stream,'store':False,'input':[
                            {'role':'user','content':'Synthetic task'},
                            {'role':'assistant','content':'Synthetic progress','phase':'commentary'}]}
                        client=http.client.HTTPConnection('127.0.0.1',fixture.gateway.server_port,timeout=10)
                        try:
                            client.request('POST','/v1/responses',json.dumps(body),{
                                'Authorization':'Bearer '+fixture.runtime.token,'Content-Type':'application/json'})
                            response=client.getresponse();raw=response.read()
                            self.assertEqual(response.status,200,raw)
                        finally:client.close()
                        response=fixtures.parse_sse(raw)[-1]['response'] if stream else json.loads(raw)
                        self.assertEqual(response['status'],'completed')
                        sent=fixtures.MockProvider.requests[-1]['messages']
                        self.assertEqual([m['role'] for m in sent],['user','assistant','user'])
                        self.assertEqual(sent[1]['content'][0]['text'],'Synthetic progress')
                        first_prefix=copy.deepcopy(sent)
                        for turn in range(3):
                            body['input'].extend(response['output'])
                            # Replay with an actual user turn, then another
                            # assistant-tail continuation, then user input again.
                            if turn!=1:body['input'].append({'role':'user','content':f'Follow-up {turn}'})
                            client=http.client.HTTPConnection('127.0.0.1',fixture.gateway.server_port,timeout=10)
                            try:
                                client.request('POST','/v1/responses',json.dumps(body),{
                                    'Authorization':'Bearer '+fixture.runtime.token,'Content-Type':'application/json'})
                                next_response=client.getresponse();raw=next_response.read()
                                self.assertEqual(next_response.status,200,raw)
                            finally:client.close()
                            response=fixtures.parse_sse(raw)[-1]['response'] if stream else json.loads(raw)
                            self.assertEqual(response['status'],'completed')
                            replay=fixtures.MockProvider.requests[-1]['messages']
                            self.assertEqual(replay[:len(first_prefix)],first_prefix)
                            self.assertEqual(replay[3]['role'],'assistant')
                            self.assertEqual(replay[3]['content'][0]['signature'],'new-signature')
                            self.assertEqual(replay[-1]['role'],'user')
                            self.assertNotIn('_provider_hub_continuation',fixtures.MockProvider.requests[-1])
                finally:fixture.tearDown()

    def test_text_only_reply_still_carries_an_authenticated_replay_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            envelope=ReasoningEnvelope(Path(directory))
            original={'stream':False,'input':[{'role':'user','content':'Task'},{'role':'assistant','content':'Progress'}]}
            translated=to_messages(original,'claude/model',{},envelope,'scope',assistant_continuation=True)
            adapter=MessagesResponsesAdapter('claude/model',envelope,'scope',continuation=translated['_provider_hub_continuation'])
            response=adapter.from_message(fixtures.native_message([{'type':'text','text':'Done'}],'end_turn'))
            self.assertEqual(response['output'][0]['type'],'reasoning')
            self.assertEqual(envelope.open(response['output'][0]['encrypted_content'],'scope'),[])
            replay={'stream':False,'input':original['input']+response['output']+[{'role':'user','content':'Next'}]}
            messages=to_messages(replay,'claude/model',{},envelope,'scope',assistant_continuation=True)['messages']
            self.assertEqual(messages[:3],translated['messages'])
            self.assertEqual([m['role'] for m in messages],['user','assistant','user','assistant','user'])
            altered=copy.deepcopy(replay);altered['input'][1]['content']='Edited progress'
            altered_messages=to_messages(altered,'claude/model',{},envelope,'scope',assistant_continuation=True)['messages']
            self.assertEqual(altered_messages[1]['content'][0]['text'],'Edited progress')
            self.assertEqual(altered_messages[2]['content'][0]['text'],CLAUDE_CONTINUATION)
            compacted={'stream':False,'input':[{'role':'user','content':'Earlier conversation summary'}]+
                       response['output']+[{'role':'user','content':'Next'}]}
            compacted_messages=to_messages(compacted,'claude/model',{},envelope,'scope',assistant_continuation=True)['messages']
            self.assertEqual(compacted_messages[1]['content'][0]['text'],CLAUDE_CONTINUATION)
            self.assertEqual(compacted_messages[-1]['content'][0]['text'],'Next')
            # Another account cannot use the receipt to introduce a user turn.
            foreign=to_messages(replay,'claude/model',{},envelope,'other-scope',assistant_continuation=True)['messages']
            self.assertNotIn(CLAUDE_CONTINUATION,json.dumps(foreign))
            bad=envelope.seal([], 'scope', continuation='unrecognized_receipt')
            with self.assertRaisesRegex(BridgeError,'malformed'):
                to_messages({'stream':False,'input':original['input']+[{'type':'reasoning','encrypted_content':bad}]},
                            'claude/model',{},envelope,'scope',assistant_continuation=True)


if __name__=='__main__':unittest.main()
