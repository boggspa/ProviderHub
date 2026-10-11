"""Translated Responses across all six existing Messages/Chat adapters."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import time
import unittest

import test_gateway_hub as fixtures
from responses_bridge import MessagesResponsesAdapter, ReasoningEnvelope, response_usage, to_messages
from bridge_core import BridgeError
from providers import ProviderError, prepare_request


MODELS = {
    "mistral": ("mistral-medium-latest", ["none", "low", "medium", "high", "max"]),
    "kimi": ("kimi-for-coding", ["low", "high", "max"]),
    "mimo": ("mimo-v2.5-pro", ["none", "high"]),
    "deepseek": ("deepseek-flash", ["none", "low", "high", "max"]),
    "muse": ("muse-spark-1.3", ["minimal", "low", "medium", "high", "xhigh", "max"]),
    "cerebras": ("gpt-oss-120b", ["low", "medium", "high"]),
}


class ResponsesBridgeTests(unittest.TestCase):
    def test_native_image_progress_interleaves_without_losing_identity_or_failing_the_run(self):
        from cli_routes import relay_cli_turn
        adapter = MessagesResponsesAdapter("codex/gpt-6-luna", None, "scope")
        wire = []

        def emit(event):
            wire.extend(adapter.feed(event))

        result = relay_cli_turn(iter([
            {"type": "image_generation", "id": "a", "phase": "started", "status": "in_progress"},
            {"type": "text_delta", "text": "Preparing another image"},
            {"type": "image_generation", "id": "b", "phase": "started", "status": "in_progress"},
            {"type": "image_generation", "id": "a", "phase": "completed", "status": "failed", "result": ""},
            {"type": "image_generation", "id": "b", "phase": "completed", "status": "completed", "result": "PNG", "revised_prompt": "Bird"},
            {"type": "image_generation", "id": "b", "phase": "completed", "status": "completed", "result": "PNG"},
            {"type": "text_delta", "text": "The second image succeeded."},
            {"type": "message_stop", "stop_reason": "end_turn"}]), emit, model="codex/gpt-6-luna")
        self.assertIsNone(result["error"])
        self.assertEqual(wire[-1]["type"], "response.completed")
        self.assertFalse(any(event["type"] in {"error", "response.failed"} for event in wire))
        done = [event for event in wire if event["type"] == "response.output_item.done"
                and event["item"]["type"] == "image_generation_call"]
        self.assertEqual([event["output_index"] for event in done], [0, 2])
        self.assertEqual([event["item"]["status"] for event in done], ["failed", "completed"])
        self.assertEqual(done[1]["item"]["result"], "PNG")
        added = [event for event in wire if event["type"] == "response.output_item.added"
                 and event["item"]["type"] == "image_generation_call"]
        self.assertEqual([event["item"]["id"] for event in added], [event["item"]["id"] for event in done])
        self.assertEqual(sum(event["type"] == "response.image_generation_call.completed" for event in wire), 1)

    def test_generated_images_survive_replay_and_failed_images_are_not_fabricated(self):
        body = {"input": [{"type": "image_generation_call", "status": "completed", "result": "iVBORw0KGgo=",
                           "revised_prompt": "A bird"},
                          {"type": "image_generation_call", "status": "failed", "result": "", "revised_prompt": "A fish"}],
                "stream": False}
        before = copy.deepcopy(body)
        translated = to_messages(body, "codex/gpt-6-luna", {}, None, "scope")
        blocks = translated["messages"][0]["content"]
        self.assertEqual([block["type"] for block in blocks], ["text", "image", "text"])
        self.assertEqual(blocks[1]["source"]["data"], "iVBORw0KGgo=")
        self.assertIn("failed", blocks[2]["text"])
        self.assertEqual(body, before)

    def test_no_tools_is_preserved_even_without_function_definitions(self):
        translated = to_messages({"input": "hi", "stream": True, "tool_choice": "none"},
                                 "codex/gpt-6-luna", {}, None, "scope")
        self.assertEqual(translated["tool_choice"], {"type": "none"})

    def test_generated_image_history_recovers_from_image_limits_and_text_only_switches(self):
        from unittest.mock import patch
        from responses_bridge import image_generation_message
        item = {"type": "image_generation_call", "status": "completed", "result": "iVBORw0KGgo=",
                "revised_prompt": "A bird"}
        with patch("cli_images.MAX_IMAGE_BYTES", 1):
            replay = image_generation_message(item)
        self.assertTrue(all(part["type"] == "input_text" for part in replay["content"]))
        self.assertIn("could not relay", replay["content"][1]["text"])
        replay = image_generation_message(item, include_image=False)
        self.assertIn("does not support image input", replay["content"][1]["text"])
        self.assertEqual(item["result"], "iVBORw0KGgo=")

    def exercise(self, provider, stream):
        fixture = fixtures.GatewayHubHTTPTests()
        fixture.setUp()
        try:
            model, efforts = MODELS[provider]
            route = fixture.start_gateway(provider, model, {"context": 1048576, "effort_modes": efforts,
                          "reasoning_history": "gateway_signed_replay" if provider == "cerebras" else "native"})

            def request(body):
                client = http.client.HTTPConnection("127.0.0.1", fixture.gateway.server_port, timeout=8)
                client.request("POST", "/v1/responses", json.dumps(body), {
                    "Authorization": "Bearer " + fixture.runtime.token, "Content-Type": "application/json"})
                response = client.getresponse()
                status, raw = response.status, response.read()
                client.close()
                # The gateway flushes the terminal chunk before it records
                # the turn as completed, so the client can see the body end
                # while the server thread is still bookkeeping. Wait for the
                # active count to settle, as the hub fixture's own helper does,
                # before any assertion reads runtime.status().
                deadline = time.monotonic() + 2
                while fixture.runtime.status()["active"] and time.monotonic() < deadline:
                    time.sleep(.01)
                return status, raw

            body = {"model": route, "input": [{"role": "user", "content": fixtures.LOCAL_REQUEST_TEXT}],
                    "max_output_tokens": 512, "reasoning": {"effort": "high"}, "stream": stream, "store": False,
                    "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}]}
            status, raw = request(body)
            self.assertEqual(status, 200, raw)
            if stream:
                events = fixtures.parse_sse(raw)
                self.assertEqual(events[-1]["type"], "response.completed", raw)
                first = events[-1]["response"]
            else:
                first = json.loads(raw)
            calls = [item for item in first["output"] if item["type"] == "function_call"]
            self.assertTrue(calls)
            if provider != "mistral":
                thinking = next(item for item in first["output"] if item["type"] == "reasoning")
                self.assertTrue(thinking["encrypted_content"].startswith("ph_reasoning_v1."))
                self.assertNotIn("native-provider-signature", thinking["encrypted_content"])
            body["input"] += first["output"] + [{"type": "function_call_output", "call_id": call["call_id"], "output": "green"} for call in calls]
            status, raw = request(body)
            self.assertEqual(status, 200, raw)
            final = fixtures.parse_sse(raw)[-1]["response"] if stream else json.loads(raw)
            self.assertEqual(final["status"], "completed", raw)
            self.assertTrue(any(item["type"] == "message" for item in final["output"]))
            self.assertEqual(len(fixtures.MockProvider.requests), 2)
            # Only inner provider calls are counted; the local translation hop
            # must not double-count requests or consume another concurrency slot.
            self.assertEqual(fixture.runtime.status()["completed"], 2)
            self.assertNotIn(fixtures.LOCAL_REQUEST_TEXT, (fixture.root / "activity.jsonl").read_text())
            if provider == "cerebras":
                assistant = next(message for message in fixtures.MockProvider.requests[1]["messages"] if message["role"] == "assistant")
                self.assertTrue(assistant.get("reasoning"))
            elif provider != "mistral":
                assistant = fixtures.MockProvider.requests[1]["messages"][1]
                self.assertTrue(any(block["type"] == "thinking" for block in assistant["content"]))
            for headers in fixtures.MockProvider.request_headers:
                self.assertNotIn(fixture.runtime.token, json.dumps(headers))
        finally:
            fixture.tearDown()

    def test_all_six_json_tool_cycles(self):
        for provider in MODELS:
            with self.subTest(provider=provider):
                self.exercise(provider, False)

    def test_all_six_streaming_tool_cycles(self):
        for provider in MODELS:
            with self.subTest(provider=provider):
                self.exercise(provider, True)

    def test_encrypted_reasoning_survives_restart_and_rejects_tampering_or_other_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = ReasoningEnvelope(root)
            blocks = [{"type": "thinking", "thinking": "PRIVATE THINKING", "signature": "provider-signature"}]
            token = first.seal(blocks, "account-one")
            self.assertNotIn("PRIVATE THINKING", token)
            self.assertEqual(ReasoningEnvelope(root).open(token, "account-one"), blocks)
            with self.assertRaises(BridgeError): first.open(token, "account-two")
            with self.assertRaises(BridgeError): first.open(token[:-3] + "abc", "account-one")
            with self.assertRaises(BridgeError): first.open("foreign-token", "account-one")
            self.assertNotIn("PRIVATE THINKING", ''.join(path.read_text() for path in root.iterdir() if path.is_file()))

    def test_a_model_switch_drops_other_routes_reasoning_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as directory:
            envelope = ReasoningEnvelope(Path(directory))
            opus = envelope.seal([{"type": "thinking", "thinking": "Opus planned this.", "signature": "s"}], "claude-opus")
            mine = envelope.seal([{"type": "thinking", "thinking": "Kimi planned this.", "signature": "k"}], "kimi-k3")
            body = {"input": [
                {"type": "message", "role": "user", "content": "Keep playing"},
                {"type": "reasoning", "encrypted_content": opus},
                {"type": "reasoning", "encrypted_content": "gAAAA-native-openai-reasoning"},
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "bare summary"}]},
                {"type": "reasoning", "encrypted_content": mine},
                {"type": "message", "role": "assistant", "content": "On it"},
                {"type": "message", "role": "user", "content": "Continue"}], "stream": False, "store": False}
            translated = to_messages(body, "kimi/k3", {}, envelope, "kimi-k3")
            text = json.dumps(translated["messages"])
            self.assertNotIn("Opus planned", text)
            self.assertNotIn("bare summary", text)
            self.assertIn("Kimi planned this.", text)
            self.assertIn("Keep playing", text)
            self.assertIn("Continue", text)
            # Reasoning that authenticates for this very route but is corrupt still fails.
            broken = envelope.cipher.encrypt(json.dumps({"scope": "kimi-k3", "blocks": [{"type": "text"}]}).encode()).decode()
            with self.assertRaises(BridgeError):
                to_messages({"input": [{"type": "reasoning", "encrypted_content": "ph_reasoning_v1." + broken}],
                             "stream": False, "store": False}, "kimi/k3", {}, envelope, "kimi-k3")

    def test_codex_legacy_reasoning_and_phases_replay_without_native_references(self):
        from codex_cli_agent import _history_items

        with tempfile.TemporaryDirectory() as directory:
            envelope = ReasoningEnvelope(Path(directory))
            adapter = MessagesResponsesAdapter("codex/gpt-6-astra", envelope, "scope", {})
            saved = adapter.from_message({
                "type": "message", "content": [
                    {"type": "thinking", "thinking": "Already checked the workspace."},
                    {"type": "tool_use", "id": "c1", "name": "shell", "input": {}}],
                "stop_reason": "tool_use"})
            body = {"input": [
                {"type": "message", "role": "user", "content": "Fix it"},
                {"type": "message", "role": "assistant", "phase": "commentary",
                 "content": "Checking"},
                *saved["output"],
                {"type": "function_call_output", "call_id": "c1", "output": "clean"},
                {"type": "message", "role": "assistant", "phase": "final_answer",
                 "content": "Done"}], "stream": False, "store": False}
            translated = to_messages(body, "codex/gpt-6-astra", {}, envelope, "scope")
            items = _history_items(translated["messages"])
            self.assertFalse(any(item["type"] in {"reasoning", "item_reference"} for item in items))
            messages = [item for item in items if item.get("role") == "assistant"]
            self.assertEqual([item["phase"] for item in messages],
                             ["commentary", "commentary", "final_answer"])
            self.assertIn("Already checked", messages[1]["content"][0]["text"])
            call = next(item for item in items if item["type"] == "function_call")
            output = next(item for item in items if item["type"] == "function_call_output")
            self.assertEqual(call["call_id"], output["call_id"])
            self.assertEqual(output["output"], [{"type": "input_text", "text": "clean"}])

    def test_phase_metadata_is_not_forwarded_to_other_messages_providers(self):
        body = {"input": [{"role": "assistant", "phase": "commentary",
                          "content": "Checking"}], "stream": False}
        translated = to_messages(body, "kimi/k3", {}, None, "scope")
        self.assertEqual(translated["messages"][0]["content"],
                         [{"type": "text", "text": "Checking"}])

    def test_unknown_context_does_not_prevent_request_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            envelope = ReasoningEnvelope(Path(directory))
            body = {"input": "hello", "stream": False, "store": False, "tools": []}
            translated = to_messages(body, "deepseek/deepseek-flash", {"context": None}, envelope, "scope")
            self.assertEqual(translated["messages"], [{"role": "user", "content": [{"type": "text", "text": "hello"}]}])
            self.assertEqual(translated["max_tokens"], 16384)

    def test_searches_render_as_calls_and_are_not_replayed_as_history(self):
        from responses_bridge import search_action
        # A provider's native server tool carries only its query.
        self.assertEqual(search_action({"query": "tide times"}), {"type": "search", "query": "tide times"})
        self.assertEqual(search_action({}), {"type": "other"})
        self.assertEqual(search_action({"query": "x", "action": {"type": "open_page", "url": "https://a.dev",
                                                                 "stray": 1}}),
                         {"type": "open_page", "url": "https://a.dev"})
        adapter = MessagesResponsesAdapter("codex/gpt-6-sol", None, "scope")
        response = adapter.from_message({"type": "message", "stop_reason": "end_turn", "usage": {}, "content": [
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "tide times"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": []},
            {"type": "text", "text": "High tide is at 14:02."}]})
        self.assertEqual([item["type"] for item in response["output"]], ["web_search_call", "message"])
        self.assertEqual(response["output"][0]["action"], {"type": "search", "query": "tide times"})
        with self.assertRaises(BridgeError):
            adapter.item({"type": "server_tool_use", "id": "srvtoolu_2", "name": "code_execution", "input": {}})
        with tempfile.TemporaryDirectory() as directory:
            body = {"stream": False, "store": False, "tools": [], "input": [
                {"type": "message", "role": "user", "content": "tide times?"},
                {"type": "web_search_call", "id": "ws_1", "status": "completed",
                 "action": {"type": "search", "query": "tide times"}},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "14:02."}]},
                {"type": "message", "role": "user", "content": "and tomorrow?"}]}
            translated = to_messages(body, "codex/gpt-6-sol", {}, ReasoningEnvelope(Path(directory)), "scope")
        self.assertEqual([message["role"] for message in translated["messages"]], ["user", "assistant", "user"])
        self.assertNotIn("tide times\"", json.dumps(translated["messages"][1]))

    def test_empty_history_items_do_not_emit_empty_text_blocks(self):
        # A restarted Codex thread replays tool calls whose output was empty as
        # function_call_output items with output:"". Anthropic-compatible
        # providers (Kimi) reject {"type":"text","text":""} with HTTP 400
        # "text content is empty", which permanently wedges the parent thread.
        with tempfile.TemporaryDirectory() as directory:
            envelope = ReasoningEnvelope(Path(directory))
            body = {"input": [
                {"type": "message", "role": "user", "content": "start"},
                {"type": "function_call", "call_id": "tool_1", "name": "exec", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "tool_1", "output": ""},
                {"type": "function_call", "call_id": "tool_2", "name": "exec", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "tool_2", "output": [{"type": "output_text", "text": ""}]},
                {"type": "message", "role": "assistant", "content": ""},
                {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": ""},
                    {"type": "input_text", "text": "kept"},
                ]},
            ], "stream": False, "store": False, "tools": []}
            translated = to_messages(body, "kimi/k3", {"context": 131072}, envelope, "scope")
            messages = translated["messages"]
            results = [block for message in messages for block in message["content"]
                       if block["type"] == "tool_result"]
            self.assertEqual(len(results), 2)
            self.assertTrue(all(result["content"] == "" for result in results))
            self.assertEqual(
                [block["text"] for message in messages for block in message["content"]
                 if block["type"] == "text"],
                ["start", "kept"])
            self.assertEqual([message["role"] for message in messages],
                             ["user", "assistant", "user", "assistant", "user"])
            self.assertTrue(all(message["content"] for message in messages))
            with self.assertRaisesRegex(BridgeError, "no content"):
                to_messages({"input": "", "stream": False, "store": False, "tools": []},
                            "kimi/k3", {"context": 131072}, envelope, "scope")

    def test_chatgpt_effort_slider_and_fast_reach_translated_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            envelope = ReasoningEnvelope(Path(directory))
            body = {"input": "hello", "stream": False, "store": False, "tools": [],
                    "reasoning": {"effort": "xhigh"}, "service_tier": "fast"}
            translated = to_messages(
                body, "kimi/k3", {"context": 131072, "max_output": 4096}, envelope, "scope")
            self.assertEqual(translated["output_config"], {"effort": "xhigh"})
            self.assertEqual(translated["service_tier"], "fast")
            self.assertNotIn("thinking", translated)
            body["reasoning"] = {"effort": "none"}
            disabled = to_messages(
                body, "kimi/k3", {"context": 131072, "max_output": 4096}, envelope, "scope")
            self.assertEqual(disabled["thinking"], {"type": "disabled"})

    def test_kimi_highspeed_accepts_every_codex_slider_position_with_thinking_fixed_on(self):
        # HighSpeed publishes a placeholder High position plus the synthesized
        # Ultra alias, and Codex may still send a persisted "none", which the
        # bridge turns into thinking disabled. HighSpeed cannot change its
        # thinking, so the gateway drops each of these on the wire instead of
        # failing the turn.
        fixture = fixtures.GatewayHubHTTPTests()
        fixture.setUp()
        try:
            route = fixture.start_gateway("kimi", "kimi-for-coding-highspeed", {
                "context": 262144, "effort_modes": ["high"], "speed_tier": "highspeed",
                "reasoning_history": "native"})
            for effort in ("none", "high", "ultra"):
                with self.subTest(effort=effort):
                    client = http.client.HTTPConnection("127.0.0.1", fixture.gateway.server_port, timeout=8)
                    client.request("POST", "/v1/responses", json.dumps({
                        "model": route, "input": [{"role": "user", "content": fixtures.LOCAL_REQUEST_TEXT}],
                        "max_output_tokens": 512, "reasoning": {"effort": effort}, "stream": False, "store": False,
                        "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}],
                    }), {"Authorization": "Bearer " + fixture.runtime.token, "Content-Type": "application/json"})
                    response = client.getresponse()
                    status, raw = response.status, response.read()
                    client.close()
                    self.assertEqual(status, 200, raw)
                    self.assertEqual(json.loads(raw)["status"], "completed", raw)
                    upstream = fixtures.MockProvider.requests[-1]
                    self.assertEqual(upstream["model"], "kimi-for-coding-highspeed")
                    self.assertNotIn("thinking", upstream)
                    self.assertNotIn("output_config", upstream)
        finally:
            fixture.tearDown()

    def test_muse_slider_xhigh_and_max_reach_native_messages(self):
        spec = {
            "id": "muse-spark-1.3", "context": 1048576, "max_output": 131072,
            "reasoning": True, "effort_modes": MODELS["muse"][1], "fast_mode": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            envelope = ReasoningEnvelope(Path(directory))
            for requested in ("xhigh", "max"):
                with self.subTest(requested=requested):
                    translated = to_messages(
                        {"input": "hello", "stream": False, "store": False, "tools": [],
                         "reasoning": {"effort": requested}},
                        "muse/muse-spark-1.3", spec, envelope, "scope",
                    )
                    self.assertEqual(translated["output_config"]["effort"], requested)
                    plan = prepare_request(
                        "muse", {}, "key", translated, "muse-spark-1.3", spec,
                    )
                    self.assertEqual(plan["body"]["output_config"]["effort"], requested)
            omitted = {**spec, "effort_modes": ["minimal", "low", "medium", "high"]}
            translated = to_messages(
                {"input": "hello", "stream": False, "store": False, "tools": [],
                 "reasoning": {"effort": "max"}},
                "muse/muse-spark-1.3", omitted, envelope, "scope",
            )
            squeezed = prepare_request("muse", {}, "key", translated, "muse-spark-1.3", omitted)
            self.assertEqual(squeezed["body"]["output_config"]["effort"], "high")
            self.assertEqual(squeezed["compatibility"]["reasoning_effort"], "max_normalized_to_high")

    def test_anthropic_cache_usage_is_included_in_codex_context_accounting(self):
        self.assertEqual(response_usage({"input_tokens": 10, "cache_creation_input_tokens": 20,
                                         "cache_read_input_tokens": 30, "output_tokens": 4}),
                         {"input_tokens": 60, "output_tokens": 4, "total_tokens": 64,
                          "input_tokens_details": {"cached_tokens": 30}})


class CustomApplyPatchBridgeTests(unittest.TestCase):
    PATCH = "*** Begin Patch\n*** Update File: a.txt\n@@\n-old\n+new\n*** End Patch"
    TOOL_MAP = {"apply_patch": {"namespace": None, "name": "apply_patch", "custom": "apply_patch"}}

    def test_custom_history_translates_to_patch_tool_blocks(self):
        from responses_bridge import to_messages
        body = {"input": [
            {"type": "message", "role": "user", "content": "edit it"},
            {"type": "custom_tool_call", "call_id": "call-1", "name": "apply_patch", "input": self.PATCH},
            {"type": "custom_tool_call_output", "call_id": "call-1", "output": "applied"},
        ], "stream": False, "store": False, "tools": []}
        translated = to_messages(body, "mistral/mistral-medium-latest", {"context": 131072}, None, "scope")
        messages = translated["messages"]
        use = next(block for message in messages if message["role"] == "assistant"
                   for block in message["content"] if block["type"] == "tool_use")
        self.assertEqual(use["input"], {"patch": self.PATCH})
        result = next(block for message in messages if message["role"] == "user"
                      for block in message["content"] if block["type"] == "tool_result")
        self.assertEqual(result["tool_use_id"], "call-1")
        with self.assertRaises(BridgeError):
            to_messages({"input": [{"type": "custom_tool_call", "name": "other_tool"}],
                         "stream": False, "store": False, "tools": []},
                        "mistral/mistral-medium-latest", {"context": 131072}, None, "scope")

    def test_mapped_tool_use_returns_custom_tool_call(self):
        from responses_bridge import MessagesResponsesAdapter
        adapter = MessagesResponsesAdapter("mistral/mistral-medium-latest", None, "scope", dict(self.TOOL_MAP))
        item = adapter.item({"type": "tool_use", "id": "call-1", "name": "apply_patch",
                             "input": {"patch": self.PATCH}})
        self.assertEqual(item["type"], "custom_tool_call")
        self.assertEqual(item["input"], self.PATCH)
        self.assertEqual(item["name"], "apply_patch")
        plain = MessagesResponsesAdapter("mistral/mistral-medium-latest", None, "scope",
                                         dict(self.TOOL_MAP)).item(
            {"type": "tool_use", "id": "call-2", "name": "read_file", "input": {"path": "a"}})
        self.assertEqual(plain["type"], "function_call")

    def test_streaming_drops_json_deltas_for_mapped_calls(self):
        from responses_bridge import MessagesResponsesAdapter
        adapter = MessagesResponsesAdapter("mistral/mistral-medium-latest", None, "scope", dict(self.TOOL_MAP))
        started = adapter.feed({"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "call-1", "name": "apply_patch", "input": {}}})
        self.assertEqual(started[0]["item"]["type"], "custom_tool_call")
        self.assertEqual(adapter.feed({"type": "content_block_delta", "index": 0, "delta": {
            "type": "input_json_delta", "partial_json": '{"patch": "x"}'}}), [])
        stopped = adapter.feed({"type": "content_block_stop", "index": 0})
        kinds = [event["type"] for event in stopped]
        self.assertNotIn("response.function_call_arguments.done", kinds)
        done = next(event["item"] for event in stopped if event["type"] == "response.output_item.done")
        self.assertEqual(done["type"], "custom_tool_call")
        self.assertEqual(done["input"], "x")


class VisualizeReferenceBridgeTests(unittest.TestCase):
    """The desktop renders a visualize reference only in its sentinel form."""

    BODY = '{"path":"/tmp/viz/busiest-days.html"}'

    def setUp(self):
        from responses_visualize import CLOSE, OPEN, SEPARATOR
        self.wrapped = OPEN + "visualize" + SEPARATOR + self.BODY + CLOSE

    def test_streamed_deltas_and_the_completed_item_share_the_wrapped_text(self):
        adapter = MessagesResponsesAdapter("claude/fable", None, "scope")
        adapter.feed({"type": "message_start", "message": {"usage": {"input_tokens": 1}}})
        adapter.feed({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        chunks = ["Here it is.\n\nvisu", "alize" + self.BODY[:9], self.BODY[9:] + "\n\n**Patterns**",
                  " worth noticing.\nvisualize" + self.BODY]
        deltas = []
        for chunk in chunks:
            for event in adapter.feed({"type": "content_block_delta", "index": 0,
                                       "delta": {"type": "text_delta", "text": chunk}}):
                self.assertEqual(event["type"], "response.output_text.delta")
                deltas.append(event["delta"])
        # Prose streams as it arrives; only the reference line waits for its newline.
        self.assertEqual(deltas, ["Here it is.\n\n", self.wrapped + "\n\n**Patterns**", " worth noticing.\n"])
        stopped = adapter.feed({"type": "content_block_stop", "index": 0})
        self.assertEqual([event["type"] for event in stopped],
                         ["response.output_text.delta", "response.output_text.done",
                          "response.content_part.done", "response.output_item.done"])
        expected = "Here it is.\n\n" + self.wrapped + "\n\n**Patterns** worth noticing.\n" + self.wrapped
        self.assertEqual("".join(deltas) + stopped[0]["delta"], expected)
        self.assertEqual(stopped[1]["text"], expected)
        self.assertEqual(stopped[2]["part"]["text"], expected)
        self.assertEqual(stopped[3]["item"]["content"][0]["text"], expected)
        adapter.feed({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 9}})
        completed = adapter.feed({"type": "message_stop"})[0]
        self.assertEqual(completed["type"], "response.completed")
        self.assertEqual(completed["response"]["output"][0]["content"][0]["text"], expected)

    def test_buffered_replies_are_wrapped_and_sentinel_text_is_left_alone(self):
        adapter = MessagesResponsesAdapter("kimi/kimi-for-coding", None, "scope")
        response = adapter.from_message({"type": "message", "stop_reason": "end_turn", "usage": {},
                                         "content": [{"type": "text", "text": "Chart:\nvisualize" + self.BODY + "\n"}]})
        self.assertEqual(response["output"][0]["content"][0]["text"], "Chart:\n" + self.wrapped + "\n")
        # Codex routes already carry the sentinels; nothing is double-wrapped.
        adapter = MessagesResponsesAdapter("codex/gpt-6.1-sol", None, "scope")
        response = adapter.from_message({"type": "message", "stop_reason": "end_turn", "usage": {},
                                         "content": [{"type": "text", "text": self.wrapped + "\n"}]})
        self.assertEqual(response["output"][0]["content"][0]["text"], self.wrapped + "\n")

    def test_nonempty_opening_text_and_fences_survive_every_split(self):
        from responses_visualize import wrap_text
        for raw in ("Intro\nvisualize" + self.BODY,
                    "```text\nvisualize" + self.BODY + "\n```\nvisualize" + self.BODY):
            expected = wrap_text(raw)
            for cut in range(len(raw) + 1):
                with self.subTest(cut=cut, raw=raw):
                    adapter = MessagesResponsesAdapter("claude/fable", None, "scope")
                    opened = adapter.feed({"type": "content_block_start", "index": 0,
                                           "content_block": {"type": "text", "text": raw[:cut]}})
                    stream = opened[0]["item"]["content"][0]["text"]
                    events = adapter.feed({"type": "content_block_delta", "index": 0,
                                           "delta": {"type": "text_delta", "text": raw[cut:]}})
                    events += adapter.feed({"type": "content_block_stop", "index": 0})
                    stream += "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")
                    self.assertEqual(stream, expected)
                    self.assertEqual(events[-1]["item"]["content"][0]["text"], expected)
                    all_events = opened + events
                    self.assertEqual([e["sequence_number"] for e in all_events], list(range(len(all_events))))


class PublishedReasoningSummaryTests(unittest.TestCase):
    """Only an opted-in, source-filtered summary lane becomes visible."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.envelope = ReasoningEnvelope(Path(directory.name))

    def adapter(self, *, enabled=True):
        return MessagesResponsesAdapter(
            "codex/gpt-6-astra", self.envelope, "scope",
            expose_reasoning_summaries=enabled)

    def test_streaming_summary_events_and_final_item_share_identity(self):
        adapter = self.adapter()
        events = []
        for value in (
            {"type": "message_start", "message": {}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": "Checking "}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "the data."}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "signature_delta", "signature": "private-signature"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        ):
            events.extend(adapter.feed(value))
        self.assertEqual([event["type"] for event in events], [
            "response.created", "response.output_item.added",
            "response.reasoning_summary_part.added",
            "response.reasoning_summary_text.delta",
            "response.reasoning_summary_text.delta",
            "response.reasoning_summary_text.done",
            "response.reasoning_summary_part.done",
            "response.output_item.done", "response.completed",
        ])
        self.assertEqual([event["sequence_number"] for event in events],
                         list(range(len(events))))
        final = events[-1]["response"]["output"][0]
        part = {"type": "summary_text", "text": "Checking the data."}
        self.assertEqual(final["summary"], [part])
        self.assertEqual(events[1]["item"]["summary"], [])
        self.assertEqual(events[2]["part"], {"type": "summary_text", "text": ""})
        for event in events[2:7]:
            self.assertEqual((event["item_id"], event["output_index"], event["summary_index"]),
                             (final["id"], 0, 0))
        self.assertEqual(events[5]["text"], part["text"])
        self.assertEqual(events[6]["part"], part)
        self.assertEqual(events[7]["item"], final)
        self.assertNotIn("private-signature", json.dumps(events))
        self.assertEqual(self.envelope.open(final["encrypted_content"], "scope"), [{
            "type": "thinking", "thinking": part["text"], "signature": "private-signature"}])

    def test_buffered_summary_and_redacted_blocks_keep_encrypted_replay(self):
        blocks = [
            {"type": "thinking", "thinking": "Published summary", "signature": "signed"},
            {"type": "redacted_thinking", "data": "REDACTED SECRET", "thinking": "HIDDEN EXTRA"},
            {"type": "text", "text": "Answer"},
        ]
        response = self.adapter().from_message({
            "type": "message", "content": blocks, "stop_reason": "end_turn"})
        self.assertEqual(response["output"][0]["summary"],
                         [{"type": "summary_text", "text": "Published summary"}])
        self.assertEqual(response["output"][1]["summary"], [])
        self.assertNotIn("REDACTED SECRET", json.dumps(response))
        self.assertNotIn("HIDDEN EXTRA", json.dumps(response))
        translated = to_messages({
            "input": [{"role": "user", "content": "start"}] + response["output"]
                     + [{"role": "user", "content": "continue"}],
            "stream": False, "store": False,
        }, "codex/gpt-6-astra", {}, self.envelope, "scope")
        self.assertEqual(translated["messages"][1]["content"], blocks)

    def test_default_and_disabled_adapters_keep_thinking_hidden(self):
        for adapter in (
            MessagesResponsesAdapter("codex/gpt-6-astra", self.envelope, "scope"),
            self.adapter(enabled=False),
        ):
            events = []
            for value in (
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "thinking", "thinking": "PRIVATE "}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "thinking_delta", "thinking": "THINKING"}},
                {"type": "content_block_stop", "index": 0},
            ):
                events.extend(adapter.feed(value))
            self.assertFalse(any("reasoning_summary" in event["type"] for event in events))
            self.assertNotIn("PRIVATE", json.dumps(events))
            self.assertEqual(events[-1]["item"]["summary"], [])
            self.assertEqual(self.envelope.open(events[-1]["item"]["encrypted_content"], "scope"),
                             [{"type": "thinking", "thinking": "PRIVATE THINKING"}])
            buffered = adapter.from_message({
                "type": "message", "content": [{"type": "thinking", "thinking": "PRIVATE"}]})
            self.assertEqual(buffered["output"][0]["summary"], [])
            self.assertNotIn("PRIVATE", json.dumps(buffered))

    def test_redacted_stream_never_becomes_a_summary(self):
        adapter = self.adapter()
        events = []
        for value in (
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "redacted_thinking", "data": "REDACTED SECRET"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "HIDDEN EXTRA"}},
            {"type": "content_block_stop", "index": 0},
        ):
            events.extend(adapter.feed(value))
        self.assertFalse(any("reasoning_summary" in event["type"] for event in events))
        self.assertEqual(events[-1]["item"]["summary"], [])
        self.assertNotIn("REDACTED SECRET", json.dumps(events))
        self.assertNotIn("HIDDEN EXTRA", json.dumps(events))

    def test_error_never_completes_an_open_summary(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                adapter = self.adapter(enabled=enabled)
                adapter.feed({"type": "content_block_start", "index": 0,
                              "content_block": {"type": "thinking", "thinking": "Partial summary"}})
                events = adapter.feed({"type": "error", "error": {"message": "backend failed"}})
                self.assertEqual([event["type"] for event in events], ["response.failed"])
                response = events[0]["response"]
                self.assertEqual(response["status"], "failed")
                self.assertEqual(response["error"]["message"], "backend failed")
                self.assertEqual("Partial summary" in json.dumps(response), enabled)


class ReasoningStoreCapTests(unittest.TestCase):
    """Bounded storage of sealed provider thinking traces."""

    TRACE = "".join(f"chunk-{index:06d}\n" for index in range(20000))  # 220,000 characters

    def round_trip(self, cap, blocks):
        with tempfile.TemporaryDirectory() as directory:
            envelope = ReasoningEnvelope(Path(directory), cap)
            token = envelope.seal(copy.deepcopy(blocks), "account-one")
            return envelope.open(token, "account-one")

    def test_default_envelope_preserves_full_trace(self):
        blocks = [{"type": "thinking", "thinking": self.TRACE}]
        self.assertEqual(self.round_trip(0, blocks), blocks)

    def test_bounded_cap_keeps_head_prefix_with_marker(self):
        cap = 16384
        opened = self.round_trip(cap, [{"type": "thinking", "thinking": self.TRACE}])
        thinking = opened[0]["thinking"]
        self.assertTrue(thinking.startswith(self.TRACE[:cap].rstrip()))
        self.assertIn(f"{len(self.TRACE) - cap:,} of {len(self.TRACE):,} characters omitted", thinking)
        self.assertTrue(thinking.endswith("characters omitted]"))
        self.assertLess(len(thinking), cap + 120)
        self.assertNotIn("chunk-019999", thinking)

    def test_short_trace_passes_through(self):
        blocks = [{"type": "thinking", "thinking": "brief"}]
        self.assertEqual(self.round_trip(16384, blocks), blocks)

    def test_redacted_thinking_passes_through(self):
        blocks = [{"type": "redacted_thinking", "data": "x" * 500000}]
        self.assertEqual(self.round_trip(16384, blocks), blocks)

    def test_nonpositive_or_noninteger_cap_is_unbounded(self):
        blocks = [{"type": "thinking", "thinking": "y" * 50000}]
        for cap in (0, -5, None, True, "16384"):
            with self.subTest(cap=cap):
                self.assertEqual(self.round_trip(cap, blocks), blocks)

    def test_streaming_path_seals_bounded_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            envelope = ReasoningEnvelope(Path(directory), 32)
            adapter = MessagesResponsesAdapter("kimi/kimi-for-coding", envelope, "scope")
            adapter.feed({"type": "content_block_start", "index": 0,
                          "content_block": {"type": "thinking", "thinking": ""}})
            adapter.feed({"type": "content_block_delta", "index": 0,
                          "delta": {"type": "thinking_delta", "thinking": "y" * 500}})
            events = adapter.feed({"type": "content_block_stop", "index": 0})
            done = next(event["item"] for event in events if event["type"] == "response.output_item.done")
            self.assertEqual(done["type"], "reasoning")
            opened = envelope.open(done["encrypted_content"], "scope")
            self.assertEqual(opened[0]["thinking"],
                             "y" * 32 + "\n\n[Provider Hub: earlier reasoning truncated - 468 of 500 characters omitted]")

    def test_store_cap_advertised_only_on_rambling_providers(self):
        from providers import PROVIDERS
        for provider_id in ("kimi", "mimo", "qwen-token-plan"):
            self.assertEqual(PROVIDERS[provider_id].get("reasoning_store_cap"), 16384)
        for provider_id in ("mistral", "deepseek", "cerebras", "ollama", "grok", "openrouter", "muse"):
            self.assertIsNone(PROVIDERS[provider_id].get("reasoning_store_cap"))

if __name__ == "__main__":
    unittest.main()
