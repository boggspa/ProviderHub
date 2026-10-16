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


def _native_home(directory, *, maximum=872000, default=500000, percent=95, slug='gpt-6.1-sol'):
    home=Path(directory);home.mkdir(parents=True,exist_ok=True)
    (home/'models_cache.json').write_text(json.dumps({'models':[{'slug':slug,'context_window':default,
        'max_context_window':maximum,'effective_context_window_percent':percent}]}))
    return str(home)


class NativeDisplayTests(unittest.TestCase):
    """Discovery and display follow the account's current card, as requests do."""

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.addCleanup(cli_routes._cache.pop,'codex',None)
        cli_routes._cache['codex']=codex

    def test_stale_catalogue_lowers_to_native_maximum_and_keeps_reserve_separate(self):
        home=_native_home(self.tmp.name,maximum=872000,percent=95)
        saved={'id':'codex/gpt-6.1-sol','context':1000000,'runtime_context':950000,'max_input':960000,
               'context_kind':'runtime_catalogue','context_evidence':'/stale/provider-hub-direct-catalog.json'}
        original=copy.deepcopy(saved)
        shown=cli_routes.native_context_spec('codex','gpt-6.1-sol',saved,config_dir=home)
        # Derived from the card (872000 * 95%), not a constant.
        self.assertEqual((shown['context'],shown['runtime_context'],shown['max_input']),(872000,828400,828400))
        self.assertEqual(shown['context_evidence'],str(Path(home)/'models_cache.json'))
        self.assertEqual(saved,original)
        other=_native_home(Path(self.tmp.name)/'other',maximum=600000,percent=90)
        self.assertEqual(cli_routes.native_context_spec('codex','gpt-6.1-sol',saved,config_dir=other)['runtime_context'],540000)

    def test_native_metadata_never_enlarges_or_invents_a_window(self):
        home=_native_home(self.tmp.name)
        for saved in ({'context':400000,'runtime_context':380000},{'context':400000},{'tools':True},
                      {'context':900000,'runtime_context':100000,'max_input':90000}):
            with self.subTest(saved=saved):
                shown=cli_routes.native_context_spec('codex','gpt-6.1-sol',saved,config_dir=home)
                for key in ('context','runtime_context','max_input'):
                    if key in saved:self.assertLessEqual(shown[key],saved[key])
                self.assertNotIn('context',shown) if 'context' not in saved else None
        small={'context':400000,'runtime_context':380000}
        self.assertIs(cli_routes.native_context_spec('codex','gpt-6.1-sol',small,config_dir=home),small)
        self.assertEqual(cli_routes.native_context_spec('codex','gpt-6.1-sol',{'context':900000,'runtime_context':100000},
                                                        config_dir=home),{'context':872000,'runtime_context':100000,
                                                        'context_kind':'runtime_catalogue',
                                                        'context_evidence':str(Path(home)/'models_cache.json')})
        stale={'context':1000000,'runtime_context':950000}
        for limits in ({},{'runtime_context':None},{'runtime_context':True},{'context':872000},{'runtime_context':'828400'}):
            with self.subTest(limits=limits):
                self.assertIs(cli_routes.native_context_spec('codex','m',stale,limits=limits),stale)
        self.assertIs(cli_routes.native_context_spec('codex','unknown',stale,config_dir=home),stale)
        self.assertIs(cli_routes.native_context_spec('codex','gpt-6.1-sol',stale,config_dir=self.tmp.name+'/missing'),stale)
        with patch.object(cli_routes,'adapter_for',side_effect=AssertionError('Other providers must not read Codex cards')):
            self.assertIs(cli_routes.native_context_spec('claude','gpt-6.1-sol',stale),stale)

    def test_configured_window_lowers_but_cannot_lift_the_card_ceiling(self):
        home=_native_home(self.tmp.name)
        def listed(configured):
            return codex._catalogue_context([],configured,home)['gpt-6.1-sol']
        self.assertEqual((listed(1000000)['context'],listed(1000000)['runtime_context']),(872000,828400))
        self.assertEqual((listed(400000)['context'],listed(400000)['runtime_context']),(400000,380000))
        self.assertEqual((listed(None)['context'],listed(None)['runtime_context']),(872000,828400))
        self.assertEqual(codex.context_catalogue(config_dir=home),codex._catalogue_context([],home=home))

    def _cached(self,mode):
        from bridge_core import cached_catalogue,connection_signature,default_settings
        root=Path(self.tmp.name)/'state';(root/'catalogues').mkdir(parents=True)
        account=_native_home(Path(self.tmp.name)/'account')
        settings=default_settings()
        settings['providers']['codex'].update(credential_mode=mode,cli_account='work',
            cli_accounts=[{'id':'work','label':'Work','config_dir':account}])
        (root/'catalogues'/'codex.json').write_text(json.dumps({'provider_id':'codex','source':'cli',
            'connection_signature':connection_signature('codex',settings['providers']['codex']),
            'models':[{'id':'gpt-6.1-sol','canonical_id':'gpt-6.1-sol','display_name':'GPT-6.1-Sol',
                       'aliases':['gpt-6.1-sol'],'context':1000000,'runtime_context':950000,'tools':True,
                       'context_kind':'runtime_catalogue','context_evidence':'/stale/catalog.json'}]}))
        before=(root/'catalogues'/'codex.json').read_bytes()
        inventory=cached_catalogue(settings,root)
        self.assertEqual((root/'catalogues'/'codex.json').read_bytes(),before)
        return settings,next(m for m in inventory['models'] if m['id']=='codex/gpt-6.1-sol'),inventory

    def test_cached_cli_catalogue_reaches_every_surface_at_the_active_accounts_window(self):
        from codex_catalogue import project_codex
        settings,model,inventory=self._cached('cli')
        self.assertEqual((model['context'],model['runtime_context']),(872000,828400))
        projected=project_codex({**settings,'codex_model':'codex/gpt-6.1-sol','codex_catalogue':['codex/gpt-6.1-sol']},inventory)
        rows=projected['models'] if isinstance(projected,dict) else projected
        row=next(r for r in rows if r['slug']=='codex/gpt-6.1-sol')
        self.assertEqual(row['context_window'],828400)

    def test_api_key_catalogue_is_not_lowered_by_cli_cards(self):
        _,model,_=self._cached('keychain')
        self.assertEqual((model['context'],model['runtime_context']),(1000000,950000))

    def test_chat_rows_show_each_logins_own_window(self):
        from chat_catalogue import chat_choices
        from hub_config import SLOTS,defaults
        default=_native_home(Path(self.tmp.name)/'default',maximum=872000)
        small=_native_home(Path(self.tmp.name)/'small',maximum=400000,default=272000,percent=90)
        settings=defaults(SLOTS,'mistral-vibe-cli-latest',11438)
        settings['providers']['codex'].update(credential_mode='cli',cli_account=None,
            cli_accounts=[{'id':'small','label':'Small','config_dir':small}])
        spec={'id':'codex/gpt-6.1-sol','provider_id':'codex','model_id':'gpt-6.1-sol','tools':True,
              'context':1000000,'runtime_context':950000}
        settings['_model_specs']={spec['id']:spec}
        settings['codex_catalogue']=[spec['id']]
        with patch.object(codex,'_codex_home',side_effect=lambda home=None:Path(home or default)):
            rows={row['account']:row['context'] for row in chat_choices(settings) if row['route']==spec['id']}
        self.assertEqual(rows,{'':828400,'small':360000})
        self.assertEqual(spec['context'],1000000)


if __name__=='__main__':unittest.main()
