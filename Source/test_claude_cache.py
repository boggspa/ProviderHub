"""Prompt-cache defaults and native Messages wire usage; no provider accounts."""
import copy
import http.client
import json
import time
import unittest
from unittest.mock import patch

from providers import prepare_request
import test_gateway_hub as fixtures


def prompt():
    return {"model":"claude/claude-opus-5-5", "max_tokens":512,
            "system":"Stable instructions", "messages":[{"role":"user", "content":"Large repeated context"}]}


class CachePlanTests(unittest.TestCase):
    def plan(self, body, provider="claude"):
        return prepare_request(provider, {}, "fixture-key", body, "claude-opus-5-5", {})["body"]

    def test_default_moves_with_conversation_without_rewriting_content(self):
        body = prompt()
        body["messages"] += [
            {"role":"assistant", "content":[{"type":"thinking","thinking":"private","signature":"signed"},
                {"type":"tool_use","id":"same-id","name":"read_file","input":{"path":"a"}}]},
            {"role":"user","content":[{"type":"tool_result","tool_use_id":"same-id","content":"contents"}]}]
        original = copy.deepcopy(body)
        plan = self.plan(body)
        self.assertEqual(plan["cache_control"], {"type":"ephemeral"})
        self.assertEqual(plan["messages"], body["messages"])
        self.assertEqual(body, original)
        body["messages"] += [{"role":"assistant","content":"Read it"},{"role":"user","content":"Next question"}]
        later = self.plan(body)
        self.assertEqual(later["messages"][:len(original["messages"])], original["messages"])
        self.assertEqual(later["cache_control"], plan["cache_control"])

    def test_explicit_top_level_policy_is_preserved_including_ttl_and_invalid_values(self):
        for policy in [{"type":"ephemeral"}, {"type":"ephemeral","ttl":"1h"}, None, {"type":"invalid"}]:
            with self.subTest(policy=policy):
                body = prompt(); body["cache_control"] = policy
                self.assertEqual(self.plan(body)["cache_control"], policy)

    def test_block_breakpoints_do_not_get_an_extra_slot_or_changed_ttl(self):
        policy = {"type":"ephemeral","ttl":"1h"}
        for location in ["system", "tools", "messages", "four"]:
            with self.subTest(location=location):
                body = prompt()
                if location in {"system", "four"}: body["system"] = [{"type":"text","text":"Stable", "cache_control":policy}]
                if location in {"tools", "four"}: body["tools"] = [{"name":"read_file","input_schema":{"type":"object"}, "cache_control":policy}]
                if location in {"messages", "four"}: body["messages"][0]["content"] = [{"type":"text","text":"Context","cache_control":policy}]
                if location == "four": body["messages"][0]["content"].append({"type":"text","text":"More context","cache_control":policy})
                original = copy.deepcopy(body)
                plan = self.plan(body)
                self.assertNotIn("cache_control", plan)
                self.assertEqual(body, original)
                self.assertEqual(plan["messages"], body["messages"])
                self.assertEqual(plan.get("tools"), body.get("tools"))

    def test_tool_arguments_and_schema_names_are_not_cache_policy(self):
        body = prompt()
        body["tools"] = [{"name":"configure", "input_schema":{"type":"object","properties":{"cache_control":{"type":"string"}}}}]
        body["messages"] += [{"role":"assistant","content":[{"type":"tool_use","id":"id","name":"configure","input":{"cache_control":"data"}}]},
                            {"role":"user","content":[{"type":"tool_result","tool_use_id":"id","content":"Done"}]}]
        self.assertEqual(self.plan(body)["cache_control"], {"type":"ephemeral"})

    def test_no_anthropic_policy_in_other_messages_compatibility_providers(self):
        for provider in ["kimi", "mimo", "deepseek", "ollama", "muse"]:
            with self.subTest(provider=provider):
                self.assertNotIn("cache_control", self.plan(prompt(), provider))


class CacheWireTests(unittest.TestCase):
    setUp = fixtures.GatewayHubHTTPTests.setUp
    tearDown = fixtures.GatewayHubHTTPTests.tearDown
    start_gateway = fixtures.GatewayHubHTTPTests.start_gateway

    def test_json_and_sse_on_both_surfaces_preserve_cache_counters(self):
        usage = {"input_tokens":12, "cache_creation_input_tokens":30, "cache_read_input_tokens":400000, "output_tokens":8}
        def serve(server):
            body = server.read_body()
            if not body.get("stream"):
                server.send_json(200, fixtures.native_message([{"type":"text","text":"Answer"}], "end_turn", usage=usage))
                return
            message = fixtures.native_message([], None, usage={**usage,"output_tokens":0})
            server.send_sse([{"type":"message_start","message":message},
                {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}},
                {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Answer"}},
                {"type":"content_block_stop","index":0},
                {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":8}},
                {"type":"message_stop"}])
        route = self.start_gateway("claude", "claude-opus-5-5", {"context":1000000})
        with patch.object(fixtures.MockProvider, "do_POST", serve):
            for surface in ["messages", "responses"]:
                for stream in [False, True]:
                    with self.subTest(surface=surface, stream=stream):
                        body = prompt() if surface == "messages" else {"model":route,"input":"Large repeated context","max_output_tokens":512}
                        body.update(model=route, stream=stream)
                        client = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=5)
                        client.request("POST", "/v1/" + surface, json.dumps(body), {"Content-Type":"application/json","Authorization":"Bearer " + self.runtime.token})
                        reply = client.getresponse(); raw = reply.read(); client.close()
                        self.assertEqual(reply.status, 200, raw)
                        if stream:
                            events = fixtures.parse_sse(raw)
                            value = events[-1]["response"] if surface == "responses" else {"usage":{**events[0]["message"]["usage"], **events[-2]["usage"]}}
                        else: value = json.loads(raw)
                        if surface == "messages": self.assertEqual(value["usage"], usage)
                        else:
                            self.assertEqual(value["usage"]["input_tokens"], 400042)
                            self.assertEqual(value["usage"]["input_tokens_details"]["cached_tokens"], 400000)
                        self.assertEqual(fixtures.MockProvider.requests[-1]["cache_control"], {"type":"ephemeral"})
        deadline = time.monotonic() + 2
        while self.runtime.status()["completed"] < 4 and time.monotonic() < deadline: time.sleep(.01)
        status = self.runtime.status()
        self.assertEqual(status["completed"], 4)
        for counters in [status, status["providers"]["claude"]]:
            self.assertEqual(counters["input_tokens"], 48)
            self.assertEqual(counters["cache_creation_input_tokens"], 120)
            self.assertEqual(counters["cache_read_input_tokens"], 1600000)
            self.assertEqual(counters["total_input_tokens"], 1600168)
        events = [json.loads(line) for line in (self.root / "activity.jsonl").read_text().splitlines()]
        done = [event for event in events if event["event"] == "completed"]
        self.assertEqual(len(done), 4)
        self.assertTrue(all(event["usage"] == usage for event in done))
        self.assertNotIn("Large repeated context", json.dumps(events))


if __name__ == "__main__": unittest.main()
