"""Exercise real Claude parsing and live MCP legs with reported cache usage."""
import copy
import json
import http.client
import time
import unittest

import claude_cli_agent as claude
import test_claude_cli_agent as fixtures
from cli_routes import relay_cli_turn
from responses_bridge import MessagesResponsesAdapter
import test_cli_routes as cli_fixtures
import test_gateway_hub as gateway_fixtures


def usage(fresh=12, output=8, read=400000, write=30):
    return {"input_tokens":fresh,"output_tokens":output,"cache_read_input_tokens":read,"cache_creation_input_tokens":write}


def start(identifier, counters):
    event = fixtures._message_start(identifier)
    event["event"]["message"]["usage"] = counters
    return event


def snapshot(identifier, counters, text="Answer"):
    return {"type":"assistant", "message":{"id":identifier,"content":[{"type":"text","text":text}],"usage":counters}}


def final(counters, text="Answer"):
    return {**fixtures._result_event(result=text), "usage":counters}


class ClaudeUsageTests(unittest.TestCase):
    _stream = fixtures.RunTurnTests._stream
    _run = fixtures.RunTurnTests._run

    def events(self, script):
        return self._run({"model":"sonnet", "messages":[{"role":"user","content":"Question"}]}, self._stream(script))[0]

    def report(self, events):
        reported = [event["usage"] for event in events if event["type"] == "usage"]
        self.assertEqual(len(reported), 1, events)
        self.assertEqual(events[-2]["type"], "usage")
        self.assertEqual(events[-1]["type"], "message_stop")
        return reported[0]

    def test_final_usage_overrides_estimate_and_repeated_stream_snapshots(self):
        counters = usage()
        script = [start("message-1", usage(output=0)), fixtures._text(0,"Answer"),
                  snapshot("message-1", counters), snapshot("message-1", counters), final(counters)]
        events = self.events(script)
        self.assertEqual(self.report(events), counters)
        wires = []
        result = relay_cli_turn(iter(events), wires.append, model="claude/test", input_tokens=450000)
        self.assertEqual(result["usage"], counters)
        adapter = MessagesResponsesAdapter("claude/test", None, "fixture")
        responses = [item for wire in wires for item in adapter.feed(wire)]
        self.assertEqual(responses[-1]["response"]["usage"], {
            "input_tokens":400042,"output_tokens":8,"total_tokens":400050,"input_tokens_details":{"cached_tokens":400000}})

    def test_message_usage_updates_merge_and_deduplicate_without_result_usage(self):
        script = [start("message-1", usage(output=0)), fixtures._text(0,"Answer"),
                  {"type":"stream_event", "event":{"type":"message_delta","usage":{"output_tokens":8},"delta":{"stop_reason":"end_turn"}}},
                  snapshot("message-1", usage()), fixtures._result_event(result="Answer")]
        self.assertEqual(self.report(self.events(script)), usage())

    def test_assistant_placeholder_cannot_overwrite_final_stream_output(self):
        script = [start("message-1", usage(output=0)), fixtures._text(0,"Answer"),
                  {"type":"stream_event", "event":{"type":"message_delta","usage":{"output_tokens":8},"delta":{"stop_reason":"end_turn"}}},
                  snapshot("message-1", usage(output=1)), fixtures._result_event(result="Answer")]
        self.assertEqual(self.report(self.events(script)), usage())

    def test_multiple_native_requests_sum_and_terminal_total_is_not_added_twice(self):
        first, second = usage(fresh=11,output=5,read=90,write=7), usage(fresh=4,output=9,read=110,write=3)
        total = {key:first[key]+second[key] for key in first}
        script = [start("first",first), snapshot("first",first,"First answer"),
                  start("second",second), snapshot("second",second,"Answer")]
        for terminal in [fixtures._result_event(result="Answer"), final(total)]:
            with self.subTest(terminal_usage="usage" in terminal):
                self.assertEqual(self.report(self.events([*copy.deepcopy(script),terminal])), total)

    def test_result_only_and_real_zeros_are_measurements(self):
        for counters in [usage(), usage(0,0,0,0)]:
            with self.subTest(counters=counters): self.assertEqual(self.report(self.events([final(counters)])), counters)

    def test_unknown_or_malformed_usage_does_not_invent_cache_metrics(self):
        for invalid in [None, {}, {"input_tokens":9}, {"input_tokens":True,"output_tokens":3},
                        usage(read=-1), usage(write="30"), usage(output=3.5)]:
            with self.subTest(invalid=invalid):
                events = self.events([final(invalid)])
                self.assertFalse(any(event["type"] == "usage" for event in events))
                result = relay_cli_turn(iter(events), lambda event: None, model="claude/test", input_tokens=1234)
                self.assertEqual(result["usage"]["input_tokens"], 1234)

    def test_input_output_without_cache_telemetry_does_not_invent_zero_cache_counts(self):
        measured = {"input_tokens":31,"output_tokens":7}
        events = self.events([final(measured)])
        self.assertEqual(self.report(events), measured)
        result = relay_cli_turn(iter(events), lambda event: None, model="claude/test", input_tokens=1234)
        self.assertEqual(result["usage"], measured)

    def test_bad_terminal_usage_cannot_discard_valid_message_usage(self):
        self.assertEqual(self.report(self.events([snapshot("one",usage()), final(usage(read=-5))])), usage())

    def test_usage_fields_do_not_serialize_reasoning_prompt_or_cost_metadata(self):
        counters = {**usage(), "private":"prompt contents", "thinking":"secret", "cost_usd":2}
        report = self.report(self.events([final(counters)]))
        self.assertEqual(report, usage())
        self.assertNotIn("secret", json.dumps(report))

    def test_successful_retry_without_report_keeps_usage_unknown(self):
        state = claude._TurnState()
        claude._translate(start("old",usage()),state)
        self.assertEqual(list(state.usage_events())[0]["usage"],usage())
        state.next_leg()
        claude._translate(snapshot("old",usage()),state)
        self.assertEqual(list(state.usage_events()), [])
        claude._translate(start("new",None),state)
        self.assertEqual(list(state.usage_events()), [])

    def test_idless_messages_keep_distinct_usage_entries(self):
        script = [start(None,usage()), fixtures._text(0,"First"), start(None,usage(fresh=8)), fixtures._text(0,"Answer"),fixtures._result_event(result="Answer")]
        self.assertEqual(self.report(self.events(script)), usage(fresh=20,output=16,read=800000,write=60))

    def test_unknown_earlier_leg_is_not_counted_again_by_cumulative_result(self):
        state = claude._TurnState()
        claude._translate(start("unknown-first",None),state)
        self.assertEqual(list(state.usage_events()),[])
        state.next_leg()
        claude._translate(snapshot("unknown-first",usage()),state)
        fresh = usage(fresh=5,output=3,read=5000,write=7)
        claude._translate(start("new-leg",fresh),state)
        total = {key:usage()[key]+fresh[key] for key in fresh}
        claude._translate(final(total),state)
        self.assertEqual(list(state.usage_events()),[{"type":"usage","usage":fresh}])


class LiveClaudeUsageTests(unittest.TestCase):
    setUp = fixtures.LiveSessionTests.setUp
    turn = fixtures.LiveSessionTests.turn

    def test_handoff_and_resume_report_only_their_own_usage(self):
        first = usage(fresh=10,output=6,read=200000,write=50)
        second = usage(fresh=20,output=7,read=200100,write=5)
        total = {key:first[key]+second[key] for key in first}
        leg = fixtures._host_leg()
        leg[0] = start("msg_1", {**first,"output_tokens":0})
        leg[-1]["event"]["usage"] = {"output_tokens":first["output_tokens"]}
        self.scripts = [[fixtures._MCP_INIT, *leg, fixtures._calls(("toolu_1","exec_command",{"cmd":"ls"})),
            snapshot("msg_1", first, "Listing."), start("msg_2",{**second,"output_tokens":0}), fixtures._text(0,"Answer"),
            snapshot("msg_2",second), final(total),fixtures._EXIT]]
        before = self.turn(fixtures._TASK)
        after = self.turn(fixtures._answered(fixtures._TASK,[("toolu_1","exec_command",{"cmd":"ls"})],[("toolu_1","a.txt")]))
        self.assertEqual([event["usage"] for event in before if event["type"] == "usage"], [first])
        self.assertEqual([event["usage"] for event in after if event["type"] == "usage"], [second])
        self.assertEqual(len(self.spawned),1)
        self.assertTrue(self.spawned[0].closed.wait(5))


    def test_resumed_message_usage_works_when_result_omits_counters(self):
        first, second = usage(fresh=3), usage(fresh=5)
        leg = fixtures._host_leg(); leg[0] = start("msg_1",first)
        self.scripts = [[fixtures._MCP_INIT,*leg,fixtures._calls(("toolu_1","exec_command",{"cmd":"ls"})),
            start("msg_2",second),fixtures._text(0,"Answer"),fixtures._result_event(result="Answer"),fixtures._EXIT]]
        self.turn(fixtures._TASK)
        after = self.turn(fixtures._answered(fixtures._TASK,[("toolu_1","exec_command",{"cmd":"ls"})],[("toolu_1","a.txt")]))
        self.assertEqual([event["usage"] for event in after if event["type"] == "usage"], [second])
        self.assertTrue(self.spawned[0].closed.wait(5))


class ClaudeUsageWireTests(unittest.TestCase):
    setUp = cli_fixtures.GatewayCliTurnTest.setUp
    tearDown = cli_fixtures.GatewayCliTurnTest.tearDown
    _stream = fixtures.RunTurnTests._stream
    _run = fixtures.RunTurnTests._run

    def test_cli_report_survives_http_json_sse_responses_and_activity_log(self):
        counters = usage()
        self.events = self._run({"model":"sonnet","messages":[{"role":"user","content":"Question"}]},self._stream([final(counters)]))[0]
        for surface in ["messages", "responses"]:
            for streaming in [False, True]:
                with self.subTest(surface=surface, streaming=streaming):
                    body = {"model":"claude/claude-sonnet-5","stream":streaming}
                    body.update({"messages":[{"role":"user","content":"Question"}],"max_tokens":512} if surface == "messages" else {"input":"Question","max_output_tokens":512})
                    client = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=5)
                    client.request("POST", "/v1/" + surface, json.dumps(body), {"Content-Type":"application/json","Authorization":"Bearer " + self.runtime.token})
                    reply = client.getresponse(); raw = reply.read(); client.close()
                    self.assertEqual(reply.status, 200, raw)
                    if streaming:
                        events = gateway_fixtures.parse_sse(raw)
                        value = events[-1]["response"] if surface == "responses" else {"usage":events[-2]["usage"]}
                    else: value = json.loads(raw)
                    if surface == "messages": self.assertEqual(value["usage"], counters)
                    else:
                        self.assertEqual(value["usage"]["input_tokens"], 400042)
                        self.assertEqual(value["usage"]["input_tokens_details"]["cached_tokens"], 400000)
        deadline = time.monotonic() + 2
        while self.runtime.status()["completed"] < 4 and time.monotonic() < deadline: time.sleep(.01)
        state = self.runtime.status()
        self.assertEqual(state["completed"],4)
        self.assertEqual(state["providers"]["claude"]["cache_read_input_tokens"],1600000)
        self.assertEqual(state["total_input_tokens"],1600168)
        recorded = [json.loads(line) for line in (self.root / "activity.jsonl").read_text().splitlines()]
        self.assertTrue(all(event["usage"] == counters for event in recorded if event["event"] == "completed"))

if __name__ == "__main__": unittest.main()
