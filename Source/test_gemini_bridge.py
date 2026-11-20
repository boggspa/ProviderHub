"""The Gemini surface: Antigravity's request in, a routed turn out."""
from __future__ import annotations

import json
import http.client
import unittest

from antigravity_catalogue import slug_for
from gemini_bridge import (GENERATE, STREAM, GeminiStream, gemini_to_messages, is_gemini_path,
                           messages_to_gemini, parse_path)


class PathTests(unittest.TestCase):
    def test_the_client_path_is_recognised(self):
        self.assertTrue(is_gemini_path("/v1beta/models/mistral~large-3:generateContent"))
        self.assertFalse(is_gemini_path("/v1/messages"))

    def test_the_model_and_method_are_read(self):
        self.assertEqual(parse_path("/v1beta/models/mistral~large-3:generateContent"),
                         ("mistral~large-3", GENERATE))
        self.assertEqual(parse_path("/v1beta/models/mistral~large-3:streamGenerateContent"),
                         ("mistral~large-3", STREAM))

    def test_an_unknown_method_is_not_addressed(self):
        self.assertIsNone(parse_path("/v1beta/models/mistral~large-3:countTokens"))


class RequestTests(unittest.TestCase):
    def test_contents_become_messages(self):
        request = gemini_to_messages({
            "contents": [{"role": "user", "parts": [{"text": "hello"}]},
                         {"role": "model", "parts": [{"text": "hi"}]}]},
            "mistral/large-3")
        self.assertEqual(request["model"], "mistral/large-3")
        self.assertEqual(request["messages"], [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}])

    def test_two_user_turns_are_one_message(self):
        request = gemini_to_messages({
            "contents": [{"role": "user", "parts": [{"text": "one"}]},
                         {"role": "user", "parts": [{"text": "two"}]}]},
            "mistral/large-3")
        self.assertEqual(len(request["messages"]), 1)
        self.assertEqual(len(request["messages"][0]["content"]), 2)

    def test_the_system_instruction_becomes_the_system_field(self):
        request = gemini_to_messages({
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "systemInstruction": {"parts": [{"text": "be brief"}]}},
            "mistral/large-3")
        self.assertEqual(request["system"], "be brief")

    def test_an_inline_image_becomes_an_image_block(self):
        request = gemini_to_messages({
            "contents": [{"role": "user", "parts": [
                {"inlineData": {"mimeType": "image/png", "data": "AAAA"}}]}]},
            "mistral/large-3")
        self.assertEqual(request["messages"][0]["content"][0], {
            "type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}})

    def test_a_function_declaration_becomes_a_tool(self):
        schema = {"type": "object", "properties": {"path": {"type": "string"}}}
        declaration = {"name": "read_file", "description": "read a file", "parameters": schema}
        request = gemini_to_messages({
            "contents": [{"role": "user", "parts": [{"text": "read it"}]}],
            "tools": [{"functionDeclarations": [declaration]}]},
            "mistral/large-3")
        self.assertEqual(request["tools"], [{
            "name": "read_file", "description": "read a file", "input_schema": schema}])
        self.assertEqual(request["tool_choice"], {"type": "auto"})

    def test_a_required_function_narrows_the_choice(self):
        request = gemini_to_messages({
            "contents": [{"role": "user", "parts": [{"text": "go"}]}],
            "tools": [{"functionDeclarations": [{"name": "read_file"}]}],
            "toolConfig": {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["read_file"]}}},
            "mistral/large-3")
        self.assertEqual(request["tool_choice"], {"type": "tool", "name": "read_file"})

    def test_a_function_call_and_its_result_are_paired(self):
        request = gemini_to_messages({
            "contents": [
                {"role": "user", "parts": [{"text": "read it"}]},
                {"role": "model", "parts": [{"functionCall": {"name": "read_file", "args": {"path": "a.py"}}}]},
                {"role": "user", "parts": [{"functionResponse": {"name": "read_file", "response": {"text": "hi"}}}]}]},
            "mistral/large-3")
        call = request["messages"][1]["content"][0]
        result = request["messages"][2]["content"][0]
        self.assertEqual(call["type"], "tool_use")
        self.assertEqual(call["input"], {"path": "a.py"})
        self.assertEqual(result["type"], "tool_result")
        self.assertEqual(result["tool_use_id"], call["id"])

    def test_the_generation_config_becomes_the_control_fields(self):
        request = gemini_to_messages({
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "generationConfig": {"maxOutputTokens": 512, "temperature": 0.2, "topP": 0.9,
                                  "stopSequences": ["STOP"]}},
            "mistral/large-3")
        self.assertEqual(request["max_tokens"], 512)
        self.assertEqual(request["temperature"], 0.2)
        self.assertEqual(request["top_p"], 0.9)
        self.assertEqual(request["stop_sequences"], ["STOP"])

    def test_a_thinking_budget_becomes_the_thinking_control(self):
        request = gemini_to_messages({
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "generationConfig": {"thinkingConfig": {"thinkingBudget": 2048}}},
            "mistral/large-3")
        self.assertEqual(request["thinking"], {"type": "enabled", "budget_tokens": 2048})

    def test_a_zero_thinking_budget_turns_thinking_off(self):
        request = gemini_to_messages({
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}}},
            "mistral/large-3")
        self.assertEqual(request["thinking"], {"type": "disabled"})


class ResponseTests(unittest.TestCase):
    def test_text_becomes_a_candidate_part(self):
        response = messages_to_gemini({
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 7, "output_tokens": 2}}, "mistral~large-3")
        candidate = response["candidates"][0]
        self.assertEqual(candidate["content"]["parts"], [{"text": "hello"}])
        self.assertEqual(candidate["finishReason"], "STOP")
        self.assertEqual(response["usageMetadata"]["promptTokenCount"], 7)
        self.assertEqual(response["usageMetadata"]["candidatesTokenCount"], 2)
        self.assertEqual(response["usageMetadata"]["totalTokenCount"], 9)
        self.assertEqual(response["modelVersion"], "mistral~large-3")

    def test_a_truncated_turn_says_so(self):
        response = messages_to_gemini({"content": [], "stop_reason": "max_tokens"})
        self.assertEqual(response["candidates"][0]["finishReason"], "MAX_TOKENS")

    def test_a_tool_call_becomes_a_function_call(self):
        response = messages_to_gemini({"content": [
            {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "a.py"}}]})
        self.assertEqual(response["candidates"][0]["content"]["parts"],
                         [{"functionCall": {"name": "read_file", "id": "call_1", "args": {"path": "a.py"}}}])

    def test_thinking_becomes_a_thought_part(self):
        response = messages_to_gemini({"content": [{"type": "thinking", "thinking": "hmm", "signature": "proof"}]})
        self.assertEqual(response["candidates"][0]["content"]["parts"],
                         [{"text": "hmm", "thought": True, "thoughtSignature": "cHJvb2Y="}])

    def test_signed_thinking_round_trips(self):
        request = gemini_to_messages({"contents": [{"role": "model", "parts": [
            {"text": "hmm", "thought": True, "thoughtSignature": "cHJvb2Y="}]}]}, "mistral/large-3")
        self.assertEqual(request["messages"][0]["content"],
                         [{"type": "thinking", "thinking": "hmm", "signature": "proof"}])


class StreamTests(unittest.TestCase):
    def test_a_text_delta_becomes_a_text_part(self):
        stream = GeminiStream("mistral~large-3")
        chunks = stream.feed({"type": "content_block_delta", "index": 0,
                              "delta": {"type": "text_delta", "text": "hi"}})
        self.assertEqual(chunks[0]["candidates"][0]["content"]["parts"], [{"text": "hi"}])

    def test_a_tool_call_appears_once_its_arguments_are_complete(self):
        stream = GeminiStream()
        stream.feed({"type": "content_block_start", "index": 0,
                     "content_block": {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {}}})
        self.assertEqual(stream.feed({"type": "content_block_delta", "index": 0,
                                      "delta": {"type": "input_json_delta", "partial_json": "{\"path\":"}}), [])
        chunks = stream.feed({"type": "content_block_delta", "index": 0,
                              "delta": {"type": "input_json_delta", "partial_json": "\"a.py\"}"}})
        self.assertEqual(chunks, [])
        chunks = stream.feed({"type": "content_block_stop", "index": 0})
        self.assertEqual(chunks[0]["candidates"][0]["content"]["parts"],
                         [{"functionCall": {"name": "read_file", "id": "call_1", "args": {"path": "a.py"}}}])

    def test_the_last_chunk_carries_the_finish_reason(self):
        stream = GeminiStream()
        stream.feed({"type": "message_start", "message": {"usage": {"input_tokens": 5, "output_tokens": 0}}})
        stream.feed({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                     "usage": {"input_tokens": 5, "output_tokens": 3}})
        chunks = stream.feed({"type": "message_stop"})
        self.assertEqual(chunks[0]["candidates"][0]["finishReason"], "STOP")
        self.assertEqual(chunks[0]["usageMetadata"]["totalTokenCount"], 8)

    def test_a_stream_that_stops_without_a_reason_still_closes(self):
        stream = GeminiStream()
        self.assertEqual(stream.end()[0]["error"]["code"], 502)


class GatewayIntegrationTests(unittest.TestCase):
    def setUp(self):
        from test_gateway_hub import GatewayHubHTTPTests
        self.fixture = GatewayHubHTTPTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.route = self.fixture.start_gateway("minimax", "MiniMax-M3", {"reasoning_history": "native"})

    def request(self, method, path, body=None, token=True, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.fixture.gateway.server_port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["x-goog-api-key"] = self.fixture.runtime.token
        if host:
            headers["Host"] = host
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        status, data = response.status, response.read()
        connection.close()
        return status, data

    def test_model_discovery_and_streaming_tool_call(self):
        status, raw = self.request("GET", "/v1beta/models")
        self.assertEqual(status, 200)
        models = json.loads(raw)["models"]
        slug = slug_for(self.route)
        self.assertTrue(any(row["name"] == "models/" + slug for row in models))
        status, raw = self.request("POST", f"/v1beta/models/{slug}:streamGenerateContent?alt=sse", {
            "contents": [{"role": "user", "parts": [{"text": "Read a file"}]}],
            "tools": [{"functionDeclarations": [{"name": "read_file", "description": "Read",
                         "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}]}],
        })
        self.assertEqual(status, 200, raw)
        chunks = [json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith("data: ")]
        parts = [part for chunk in chunks for candidate in chunk.get("candidates", [])
                 for part in candidate.get("content", {}).get("parts", [])]
        self.assertTrue(any(part.get("functionCall", {}).get("name") == "read_file" for part in parts), parts)
        self.assertEqual(chunks[-1]["candidates"][0]["finishReason"], "STOP")
        call = next(part["functionCall"] for part in parts if "functionCall" in part)
        status, raw = self.request("POST", f"/v1beta/models/{slug}:generateContent", {
            "contents": [{"role": "user", "parts": [{"text": "Read a file"}]},
                         {"role": "model", "parts": parts},
                         {"role": "user", "parts": [{"functionResponse": {
                             "name": call["name"], "id": call["id"], "response": {"content": "contents"}}}]}],
            "tools": [{"functionDeclarations": [{"name": "read_file", "description": "Read",
                         "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}]}],
        })
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)["candidates"][0]["content"]["parts"][0]["text"], "Native JSON complete.")

    def test_rejects_wrong_key_and_nonlocal_host(self):
        self.assertEqual(self.request("GET", "/v1beta/models", token=False)[0], 401)
        self.assertEqual(self.request("GET", "/v1beta/models", host="evil.example")[0], 403)
        slug = slug_for(self.route)
        self.assertEqual(self.request("POST", f"/v1beta/models/{slug}:generateContent", {}, host="evil.example")[0], 403)


if __name__ == "__main__":
    unittest.main()
