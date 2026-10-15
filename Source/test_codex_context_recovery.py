"""Context pressure must reach the desktop as compaction, not generic retry."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import cli_routes
import codex_cli_agent as codex
import gateway
from cli_lifecycle import is_context_overflow
from responses_bridge import MessagesResponsesAdapter
import test_codex_cli_agent as codex_fixture
import test_cli_routes as route_fixture
from test_codex_cli_agent import FakeCodexSession, _completed_turn, _ev
from test_cli_routes import _fake_adapter
from test_gateway_hub import parse_sse


ERROR = "Your input exceeds the context window of this model. Please adjust your input and try again."


class ContextErrorsTests(unittest.TestCase):
    def test_codex_terminal_failures_preserve_context_classification(self):
        for error in ({'message':ERROR}, {'message':'Input rejected','codexErrorInfo':'contextWindowExceeded'},
                      {'message':'Input rejected','codexErrorInfo':{'contextWindowExceeded':{}}},
                      {'codexErrorInfo':'contextWindowExceeded'}):
            for separate in (False,True):
                with self.subTest(error=error,separate=separate):
                    fixture=codex_fixture.RunTurnTests();fixture.setUp()
                    try:
                        fake=FakeCodexSession([])
                        fake.script=([_ev('error',{'error':error,'willRetry':False}),_completed_turn(status='failed')]
                                     if separate else [_completed_turn(status='failed',error=error)])
                        events,_,_=fixture._run(fixture._request(),fake)
                        self.assertEqual(events[-1]['code'],'context_length_exceeded')
                        self.assertEqual(events[-1]['http_status'],400)
                        self.assertEqual(events[-1]['type'],'error')
                    finally:fixture.doCleanups()

    def test_another_turns_context_error_does_not_change_this_turns_failure(self):
        fixture=codex_fixture.RunTurnTests();fixture.setUp()
        try:
            fake=FakeCodexSession([])
            fake.script=[_ev('error',{'turnId':'other-turn','error':{'message':ERROR},'willRetry':False}),
                         _completed_turn(status='failed')]
            events,_,_=fixture._run(fixture._request(),fake)
            self.assertNotIn('code',events[-1])
            self.assertNotIn(ERROR,events[-1]['message'])
        finally:fixture.doCleanups()

    def test_unrelated_failures_and_output_limits_are_not_context_errors(self):
        for error in ('quota exhausted','maximum output tokens exceeded','model unavailable',
                      {'message':'network unavailable','codexErrorInfo':'httpConnectionFailed'}):
            self.assertFalse(is_context_overflow(error))
            event=codex._turn_error(str(error),error)
            self.assertNotIn('code',event)

    def test_rpc_error_text_becomes_context_error(self):
        for details in ({'code':-32000,'message':ERROR},
                        {'code':-32000,'message':'Input rejected','codexErrorInfo':'contextWindowExceeded'}):
            with self.subTest(details=details):
                try: codex._result({'error':details},context='turn/start')
                except codex.CodexCliAgentError as error:
                    event=codex._turn_error(str(error),{'code':error.error_code},http_status=error.http_status)
                else:self.fail('Expected RPC failure')
                self.assertEqual(event['code'],'context_length_exceeded')
                self.assertEqual(event['http_status'],400)

    def test_stream_adapter_preserves_code_even_after_text_started(self):
        for error in ({'message':ERROR},{'code':'context_length_exceeded','message':'Input rejected'}):
            adapter=MessagesResponsesAdapter('codex/test',None,'test')
            adapter.feed({'type':'message_start','message':{'usage':{}}})
            event=adapter.feed({'type':'error','error':error})[-1]
            self.assertEqual(event['type'],'response.failed')
            self.assertEqual(event['response']['error']['code'],'context_length_exceeded')
            self.assertEqual(event['response']['error']['message'],error['message'])

    def test_cli_to_responses_overflow_never_retries_or_becomes_provider_error(self):
        for stream in (False,True):
            for start in ('none','text','ping'):
                for typed in (False,True):
                    with self.subTest(stream=stream,start=start,typed=typed):
                        fixture=route_fixture.ResponsesEndToEndTest();fixture.setUp()
                        try:
                            route='codex/gpt-6.1-sol';calls=[]
                            fixture.runtime.settings['providers']['codex']['credential_mode']='cli'
                            fixture.runtime.settings['_model_specs'][route]={'context':1000000,'tools':True}
                            def run(request,**kwargs):
                                calls.append(request)
                                if start=='text':yield {'type':'text_delta','text':'Starting'}
                                elif start=='ping':time.sleep(.06)
                                yield {'type':'error','message':'Input rejected' if typed else ERROR,
                                       **({'code':'context_length_exceeded','http_status':400} if typed else {})}
                            cli_routes._cache['codex']=_fake_adapter(PROVIDER_ID='codex',run_turn=run)
                            with patch.object(gateway,'CLI_KEEPALIVE_INTERVAL',.01):
                                client=http.client.HTTPConnection('127.0.0.1',fixture.gateway.server_port,timeout=5)
                                try:
                                    client.request('POST','/v1/responses',json.dumps({'model':route,'stream':stream,'input':'Synthetic task'}),
                                                   {'Authorization':'Bearer '+fixture.runtime.token,'Content-Type':'application/json'})
                                    response=client.getresponse();raw=response.read()
                                finally:client.close()
                            if stream:
                                self.assertEqual(response.status,200,raw)
                                events=parse_sse(raw)
                                terminal=[e for e in events if e['type'] in ('response.completed','response.failed','error')]
                                self.assertEqual([e['type'] for e in terminal],['response.failed'],raw)
                                error=terminal[0]['response']['error']
                            else:
                                self.assertEqual(response.status,400,raw)
                                error=json.loads(raw)['error']
                            self.assertEqual(error['code'],'context_length_exceeded',raw)
                            self.assertEqual(len(calls),1)
                        finally:fixture.tearDown()


class NativeBudgetTests(unittest.TestCase):
    def test_current_card_maximum_and_reserve_override_stale_larger_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory);file=home/'models_cache.json'
            file.write_text(json.dumps({'models':[{'slug':'gpt-6.1-sol','context_window':500000,
                'max_context_window':872000,'effective_context_window_percent':95}]}))
            saved={'context':1000000,'runtime_context':950000,'max_input':950000,'tools':True}
            original=copy.deepcopy(saved)
            with patch.object(cli_routes,'adapter_for',return_value=codex):
                effective=cli_routes.current_context_spec('codex','gpt-6.1-sol',saved,config_dir=directory)
            self.assertEqual(effective['context'],828400)
            self.assertEqual(effective['runtime_context'],828400)
            self.assertEqual(effective['max_input'],828400)
            self.assertEqual(saved,original)
            self.assertEqual(codex.context_limits('unknown',config_dir=directory),{})
            self.assertEqual(list(home.iterdir()),[file])

    def test_larger_missing_and_malformed_metadata_do_not_enlarge_limits(self):
        saved={'context':200000,'max_input':180000}
        for limit in (1000000,None,-1,True,'872000'):
            with self.subTest(limit=limit),patch.object(cli_routes,'adapter_for',return_value=
                    _fake_adapter(context_limits=lambda *args,**kw:{'runtime_context':limit})):
                result=cli_routes.current_context_spec('codex','model',saved)
                self.assertLessEqual(result['context'],saved['context'])
                self.assertLessEqual(result['max_input'],saved['max_input'])
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'models_cache.json').write_text('{broken')
            self.assertEqual(codex.context_limits('model',config_dir=directory),{})
        with patch.object(cli_routes,'adapter_for',side_effect=AssertionError('Other providers must not read Codex cache')):
            self.assertIs(cli_routes.current_context_spec('claude','model',saved),saved)

    def test_gateway_compacts_with_current_account_budget_without_mutating_catalogue(self):
        fixture=route_fixture.ResponsesEndToEndTest();fixture.setUp()
        try:
            route='codex/gpt-6.1-sol';seen=[]
            spec={'context':100000,'runtime_context':95000,'tools':True}
            fixture.runtime.settings['providers']['codex']['credential_mode']='cli'
            fixture.runtime.settings['_model_specs'][route]=spec
            def limits(model,*,config_dir):
                seen.append((model,config_dir));return {'runtime_context':20000}
            cli_routes._cache['codex']=_fake_adapter(PROVIDER_ID='codex',context_limits=limits)
            body={'model':route,'max_tokens':2000,'_provider_hub_surface':'responses','messages':[
                {'role':'user' if i%2==0 else 'assistant','content':f'Round {i} '+('word '*4000)} for i in range(10)]}
            body['messages'].append({'role':'user','content':'Latest instruction must survive'})
            original=copy.deepcopy(body)
            with patch.object(fixture.runtime,'cli_account_dir',return_value='/synthetic/account'):
                plan=fixture.runtime.plan(body)
            self.assertEqual(seen,[('gpt-6.1-sol','/synthetic/account')])
            self.assertEqual(plan['model_spec']['context'],20000)
            self.assertLess(plan['compatibility']['auto_compact']['estimated_after'],20000)
            self.assertIn('Latest instruction must survive',json.dumps(plan['body']))
            self.assertEqual(spec['context'],100000)
            self.assertEqual(body,original)
        finally:fixture.tearDown()


if __name__=='__main__':unittest.main()
