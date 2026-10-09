"""Chat's execution boundary, private history, and Messages wire contract."""
import copy
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from chat_runtime import ChatService, ChatStore, GatewayClient, entry, needs_approval
from chat_tools import ChatToolRunner


def model(route="ollama/test", account=""):
    return {"id": route + "|" + account, "route": route, "account": account, "scope": "scope-" + account,
            "label": route, "provider": "Ollama", "accountLabel": account or "Default",
            "supportsTools": True, "efforts": ["low", "high"], "context": 100000,
            "max_output": 4096}


def response(text="Done", call=None):
    content = [{"type": "text", "text": text}] if text else []
    if call:
        content.append({"type": "tool_use", "id": "call-1", "name": "apply_patch", "input": {"patch": call}})
    return {"role": "assistant", "content": content, "stop_reason": "tool_use" if call else "end_turn", "usage": {"input_tokens": 12}}


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses); self.requests = []; self.rows = [model(), model("ollama/other", "work")]
    def catalogue(self): return copy.deepcopy(self.rows)
    def cancel(self): pass
    def stream(self, payload, cancel, delta):
        self.requests.append(copy.deepcopy(payload))
        item = self.responses.pop(0)
        if isinstance(item, Exception): raise item
        if callable(item): return item(payload, cancel, delta)
        for block in item["content"]:
            if block["type"] == "text": delta(block["text"])
        return copy.deepcopy(item)


class ChatRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = ChatStore(self.root, lock=False); self.events = []

    def service(self, responses):
        transport = FakeTransport(responses)
        service = ChatService(self.store, transport, self.events.append)
        service.refresh(); service.create(transport.rows[0]["id"], str(self.root))
        return service, transport

    def send(self, service, text="Please work"):
        service.handle({"command": "send", "id": service.chat["id"], "text": text})

    def finish(self, service):
        service.thread.join(3)
        self.assertFalse(service.busy, "worker did not complete")

    def approval(self, service):
        deadline = time.monotonic() + 3
        while service.approval is None and time.monotonic() < deadline: time.sleep(.01)
        self.assertIsNotNone(service.approval)
        return service.approval["id"]

    def test_streaming_reply_saved_with_opaque_reasoning(self):
        reply = response()
        reply["content"].insert(0, {"type": "thinking", "thinking": "private reasoning", "signature": "opaque-signature"})
        service, transport = self.service([reply]); self.send(service); self.finish(service)
        stored = self.store.load(service.chat["id"])
        self.assertEqual(stored["messages"][-1]["content"][0]["signature"], "opaque-signature")
        self.assertEqual(stored["entries"][-1]["text"], "Done")
        self.assertNotIn("private reasoning", json.dumps(stored["entries"]))
        self.assertEqual(transport.requests[0]["_provider_hub_surface"], "chat")
        self.assertEqual(transport.requests[0]["_provider_hub_connection"], "scope-")
        self.assertEqual(os.stat(self.store.path(stored["id"])).st_mode & 0o777, 0o600)

    def test_approval_denial_is_a_result_without_execution(self):
        patch = "*** Begin Patch\n*** Add File: denied.txt\n+no\n*** End Patch"
        service, transport = self.service([response("", patch), response("Denied")]); self.send(service)
        approval = self.approval(service)
        self.assertEqual(service.approval["detail"], patch)
        with self.assertRaises(ValueError): service.handle({"command": "approve", "id": "stale", "allow": True})
        self.assertFalse((self.root / "denied.txt").exists())
        service.handle({"command": "approve", "id": approval, "allow": False}); self.finish(service)
        self.assertFalse((self.root / "denied.txt").exists())
        result = transport.requests[1]["messages"][-1]["content"][0]
        self.assertTrue(result["is_error"]); self.assertIn("denied", result["content"][0]["text"])

    def test_web_search_default_toggle_and_unsupported_models(self):
        service, transport = self.service([response(), response(), response()])
        service.models[0]["supportsWebSearch"] = True
        self.send(service); self.finish(service)
        self.assertEqual(transport.requests[-1]["_web_search"], {"context_size": None, "allowed_domains": [], "live": True})
        self.assertNotIn("web_search", {tool["name"] for tool in transport.requests[-1]["tools"]})
        service.handle({"command": "preferences", "webSearch": False})
        self.send(service); self.finish(service)
        self.assertNotIn("_web_search", transport.requests[-1])
        self.assertIn("Web search is disabled", transport.requests[-1]["system"])
        service.handle({"command": "preferences", "webSearch": True})
        service.models[0]["supportsWebSearch"] = False
        self.send(service); self.finish(service)
        self.assertNotIn("_web_search", transport.requests[-1])
        self.assertIn("does not offer native web search", transport.requests[-1]["system"])
        for invalid in (None, "false", 0, 1):
            with self.assertRaises(ValueError):
                service.handle({"command": "preferences", "webSearch": invalid})

    def test_provider_search_results_are_visible_saved_and_never_executed_locally(self):
        reply = response("Verified result.")
        reply["content"][:0] = [
            {"type": "server_tool_use", "id": "native-1", "name": "web_search", "input": {"query": "release notes"}},
            {"type": "web_search_tool_result", "tool_use_id": "native-1", "content": [
                {"type": "web_search_result", "url": "https://example.com/releases", "title": "Releases", "encrypted_content": "opaque-search-data"},
                {"type": "web_search_result", "url": "file:///private/local", "title": "Local"}]}]
        reply["content"][-1]["citations"] = [{"type": "web_search_result_location", "url": "https://example.com/releases", "title": "Releases"}]
        service, transport = self.service([reply])
        service.models[0]["supportsWebSearch"] = True
        with mock.patch.object(ChatToolRunner, "execute", side_effect=AssertionError("Hosted searches are never local tools")):
            self.send(service); self.finish(service)
        saved = self.store.load(service.chat["id"])
        self.assertEqual(saved["status"], "ready")
        self.assertEqual(saved["messages"][-1]["content"], reply["content"])
        answer = next(item for item in saved["entries"] if item["kind"] == "assistant")
        self.assertEqual(answer["text"].count("https://example.com/releases"), 1)
        search = next(item for item in saved["entries"] if item.get("tool") == "web_search")
        self.assertEqual(search["summary"], "Web search: release notes")
        self.assertFalse(search["isError"])
        self.assertNotIn("opaque-search-data", json.dumps(saved["entries"]))
        self.assertNotIn("file:///private/local", json.dumps(saved["entries"]))

    def test_search_error_is_visible_without_failing_the_answer(self):
        reply = response("Search unavailable.")
        reply["content"][:0] = [
            {"type": "server_tool_use", "id": "native-1", "name": "web_search", "input": {}},
            {"type": "web_search_tool_result", "tool_use_id": "native-1", "content": {"type": "web_search_tool_result_error", "error_code": "unavailable"}}]
        service, _ = self.service([reply]); self.send(service); self.finish(service)
        search = next(item for item in service.chat["entries"] if item.get("tool") == "web_search")
        self.assertTrue(search["isError"])
        self.assertIn("unavailable", search["detail"])
        self.assertEqual(service.chat["status"], "ready")

    def test_approved_action_is_persisted_and_not_replayed_on_retry(self):
        patch = "*** Begin Patch\n*** Add File: once.txt\n+once\n*** End Patch"
        service, transport = self.service([response("", patch), ValueError("Connection lost"), response("Recovered")])
        self.send(service); approval = self.approval(service)
        service.handle({"command": "approve", "id": approval, "allow": True}); self.finish(service)
        self.assertEqual((self.root / "once.txt").read_text(), "once\n")
        self.assertEqual(service.chat["status"], "error")
        results = [block for msg in service.chat["messages"] for block in msg["content"] if block["type"] == "tool_result"]
        self.assertEqual(len(results), 1); self.assertFalse(results[0]["is_error"])
        service.handle({"command": "retry", "id": service.chat["id"]}); self.finish(service)
        self.assertEqual(service.chat["status"], "ready")
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(transport.requests[-1]["messages"][-1]["content"][0]["tool_use_id"], "call-1")
        self.assertEqual(len([item for item in service.chat["entries"] if item["kind"] == "user"]), 1)

    def test_stop_waiting_for_approval_never_executes_and_completes_cycle(self):
        patch = "*** Begin Patch\n*** Add File: stop.txt\n+no\n*** End Patch"
        service, _ = self.service([response("", patch)]); self.send(service); self.approval(service)
        service.handle({"command": "stop"}); self.finish(service)
        self.assertFalse((self.root / "stop.txt").exists())
        self.assertEqual(service.chat["status"], "stopped")
        self.assertTrue(all(msg["content"] for msg in service.chat["messages"]))
        self.assertEqual(service.chat["messages"][-1]["content"][0]["tool_use_id"], "call-1")

    def test_partial_stream_stop_is_visible_but_not_validated_history(self):
        def streaming(payload, cancel, delta):
            delta("Partial output")
            cancel.wait(2)
            raise InterruptedError()
        service, _ = self.service([streaming]); self.send(service)
        deadline = time.monotonic() + 2
        while not any(item["text"] == "Partial output" for item in service.chat["entries"]) and time.monotonic() < deadline: time.sleep(.01)
        service.handle({"command": "stop"}); self.finish(service)
        stored = self.store.load(service.chat["id"])
        self.assertTrue(any(item["text"] == "Partial output" for item in stored["entries"]))
        self.assertEqual(len(stored["messages"]), 1)

    def test_model_switch_keeps_thread_and_archives_context_workspace_starts_new_chat(self):
        service, transport = self.service([response()]); self.send(service); self.finish(service)
        previous = service.chat["id"]
        service.handle({"command": "configure", "choice": transport.rows[1]["id"]})
        self.assertEqual(service.chat["id"], previous)
        self.assertTrue(service.chat["messages"])
        self.assertEqual(service.chat["account"], "work")
        self.assertEqual(len(self.store.load(previous)["archives"][0]["messages"]), 2)
        service.handle({"command": "select", "id": previous})
        folder = self.root / "other"; folder.mkdir()
        service.handle({"command": "configure", "workspace": str(folder)})
        self.assertNotEqual(service.chat["id"], previous)
        self.assertEqual(service.chat["workspace"], str(folder))

    def test_crash_recovery_marks_unanswered_actions_without_executing(self):
        service, transport = self.service([])
        service.chat["status"] = "working"
        service.chat["entries"] = [entry("tool", tool="run_shell", summary="Run touch never", detail="Running…")]
        service.chat["messages"] = [{"role": "assistant", "content": [{"type": "tool_use", "id": "crash", "name": "run_shell", "input": {"command": "touch never"}}]}]
        service.save()
        recovered = ChatService(self.store, transport, self.events.append); recovered.initialize()
        self.assertEqual(recovered.chat["status"], "interrupted")
        self.assertFalse((self.root / "never").exists())
        self.assertTrue(recovered.chat["messages"][-1]["content"][0]["is_error"])
        self.assertIn("Inspect", recovered.chat["messages"][-1]["content"][0]["content"][0]["text"])
        self.assertTrue(recovered.chat["entries"][0]["isError"])
        self.assertNotEqual(recovered.chat["entries"][0]["detail"], "Running…")

    def test_saved_chat_recovers_even_when_gateway_is_offline(self):
        service, transport = self.service([])
        service.chat["status"] = "working"; service.save()
        def offline(): raise ConnectionRefusedError("offline")
        transport.catalogue = offline
        recovered = ChatService(self.store, transport, self.events.append)
        recovered.initialize()
        self.assertEqual(recovered.chat["id"], service.chat["id"])
        self.assertEqual(recovered.chat["status"], "interrupted")
        self.assertTrue(any(e["event"] == "ready" for e in self.events))
        self.assertTrue(any("gateway is offline" in e.get("message", "") for e in self.events))

    def test_unknown_tool_and_duplicate_ids_do_not_execute(self):
        reply = response("", "invalid")
        reply["content"][-1]["name"] = "invented"
        service, transport = self.service([reply, response()]); self.send(service); self.finish(service)
        self.assertTrue(transport.requests[1]["messages"][-1]["content"][0]["is_error"])
        reply["content"] *= 2
        service, transport = self.service([reply]); self.send(service); self.finish(service)
        self.assertEqual(service.chat["status"], "error")
        self.assertEqual(len(service.chat["messages"]), 1)

    def test_storage_rejects_path_escape_symlinks_and_corruption(self):
        with self.assertRaises(ValueError): self.store.path("../outside")
        target = self.root / "outside"; target.write_text("preserved")
        identifier = "a" * 32; self.store.path(identifier).symlink_to(target)
        with self.assertRaises(ValueError): self.store.load(identifier)
        self.assertEqual(target.read_text(), "preserved")
        (self.store.root / ("b" * 32 + ".jsonl")).write_text("not json\n")
        self.assertEqual(self.store.all(), [])

    def test_only_one_worker_may_own_chat_storage(self):
        first = ChatStore(self.root)
        try:
            with self.assertRaises(ValueError): ChatStore(self.root)
        finally: first._lock.close()

    def test_context_trim_keeps_full_transcript_and_complete_tool_cycles(self):
        service, transport = self.service([])
        choice = transport.rows[0]; choice["context"] = 12000
        for _ in range(20):
            service.chat["messages"] += [{"role": "user", "content": [{"type": "text", "text": "old" * 2000}]},
                                         {"role": "assistant", "content": [{"type": "text", "text": "answer" * 2000}]}]
        service.chat["entries"] = [entry("user", "original transcript", choice["route"])]
        before = len(service.chat["messages"])
        payload = service.payload(choice)
        self.assertLess(len(payload["messages"]), before)
        self.assertEqual(service.chat["entries"][0]["text"], "original transcript")
        self.assertTrue(any("context trimmed" in item["text"] for item in service.chat["entries"]))

    def test_accept_edits_applies_repository_patch_without_approval(self):
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        patch = "*** Begin Patch\n*** Add File: accepted.txt\n+allowed\n*** End Patch"
        service, transport = self.service([response("", patch), response("Done")])
        service.handle({"command": "configure", "approvalMode": "accept_edits"})
        self.send(service); self.finish(service)
        self.assertEqual((self.root / "accepted.txt").read_text(), "allowed\n")
        self.assertFalse(any(event["event"] == "approval" for event in self.events))
        self.assertEqual(self.store.load(service.chat["id"])["approvalMode"], "accept_edits")
        self.assertIn("Accept Edits", transport.requests[0]["system"])

    def test_accept_edits_still_asks_for_shell_nonrepo_and_git_metadata(self):
        patch = {"patch": "*** Begin Patch\n*** Add File: accepted.txt\n+allowed\n*** End Patch"}
        self.assertTrue(needs_approval("accept_edits", "apply_patch", patch, self.root))
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        self.assertFalse(needs_approval("accept_edits", "apply_patch", patch, self.root))
        self.assertTrue(needs_approval("accept_edits", "run_shell", {"command": "printf hi > a"}, self.root))
        for path in ("../outside", ".git/config"):
            change = {"patch": f"*** Begin Patch\n*** Update File: {path}\n@@\n-old\n+new\n*** End Patch"}
            self.assertTrue(needs_approval("accept_edits", "apply_patch", change, self.root))
        service, _ = self.service([{"content": [{"type": "tool_use", "id": "shell", "name": "run_shell", "input": {"command": "echo hi"}}], "stop_reason": "tool_use"}, response()])
        service.handle({"command": "configure", "approvalMode": "accept_edits"})
        self.send(service); approval = self.approval(service)
        service.handle({"command": "configure", "approvalMode": "yolo"})
        self.assertEqual(service.chat["approvalMode"], "accept_edits")
        service.handle({"command": "approve", "id": approval, "allow": False}); self.finish(service)

    def test_yolo_runs_tools_without_prompt_and_modes_cannot_change_during_turn(self):
        first = {"content": [{"type": "tool_use", "id": "shell", "name": "run_shell", "input": {"command": "printf yolo > mode.txt"}}], "stop_reason": "tool_use"}
        service, _ = self.service([first, response()])
        service.handle({"command": "configure", "approvalMode": "yolo"})
        self.send(service); self.finish(service)
        self.assertEqual((self.root / "mode.txt").read_text(), "yolo")
        self.assertFalse(any(event["event"] == "approval" for event in self.events))
        self.assertEqual(service.chat["approvalMode"], "yolo")
        before = service.chat["id"]
        service.handle({"command": "configure", "approvalMode": "manual"})
        self.assertEqual(service.chat["id"], before)
        with self.assertRaises(ValueError): service.handle({"command": "configure", "approvalMode": "invented"})
        self.assertTrue(needs_approval("unknown", "run_shell", {}, self.root))
        self.assertFalse(needs_approval("yolo", "run_shell", {}, self.root))

    def test_old_chats_default_to_manual_and_sent_text_is_saved_before_ack(self):
        service, _ = self.service([response()])
        service.chat.pop("approvalMode"); service.save()
        self.assertEqual(self.store.load(service.chat["id"])["approvalMode"], "manual")
        self.assertTrue(needs_approval("manual", "apply_patch", {}, self.root))
        service.chat["approvalMode"] = "manual"
        accepted = []
        def observe(event):
            if event["event"] == "entry" and event["entry"]["kind"] == "user":
                saved = self.store.load(service.chat["id"])
                accepted.append(saved["messages"][-1]["content"][0]["text"])
        service.emit = observe
        self.send(service, "durable before acknowledgement"); self.finish(service)
        self.assertEqual(accepted, ["durable before acknowledgement"])


class MessagesStreamTests(unittest.TestCase):
    def wire(self, events):
        return io.BytesIO(b"".join(("data: " + json.dumps(e) + "\n\n").encode() for e in events))

    def events(self):
        return [{"type": "message_start", "message": {"usage": {"input_tokens": 20}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "opaque"}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "c", "name": "read_file", "input": {}}},
                {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"path":"a"}'}},
                {"type": "content_block_stop", "index": 1},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}},
                {"type": "message_stop"}]

    def stream(self, events):
        response = self.wire(events); response.status = 200; response.getheader = lambda key, default="": "text/event-stream"
        class Connection:
            def request(self, *args): pass
            def getresponse(self): return response
            def close(self): pass
        client = GatewayClient(Path("/unused")); client.connect = Connection; client.headers = lambda: {}
        return client.stream({}, threading.Event(), lambda text: None)

    def test_native_thinking_signature_and_tool_arguments_preserved(self):
        result = self.stream(self.events())
        self.assertEqual(result["content"][0]["signature"], "sig")
        self.assertEqual(result["content"][1]["input"], {"path": "a"})
        self.assertEqual(result["usage"], {"input_tokens": 20, "output_tokens": 5})

    def test_native_search_and_streamed_citations_preserved(self):
        citation = {"type": "web_search_result_location", "url": "https://example.com", "title": "Source", "encrypted_index": "opaque"}
        events = [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"query":"news"}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "web_search_tool_result", "tool_use_id": "s1", "content": []}},
            {"type": "content_block_stop", "index": 1},
            {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": "Answer"}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "citations_delta", "citation": citation}},
            {"type": "content_block_stop", "index": 2},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, {"type": "message_stop"}]
        result = self.stream(events)
        self.assertEqual(result["content"][0]["input"], {"query": "news"})
        self.assertEqual(result["content"][2]["citations"], [citation])

    def test_truncated_or_invalid_tool_stream_fails_before_execution(self):
        with self.assertRaises(ValueError): self.stream(self.events()[:-1])
        events = self.events(); events[6]["delta"]["partial_json"] = "not json"
        with self.assertRaises(ValueError): self.stream(events)

    def test_message_stop_cannot_execute_initial_input_of_an_unfinished_block(self):
        events = [{"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "bad", "name": "run_shell", "input": {"command": "touch unintended"}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"command":"unfinished'}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}, {"type": "message_stop"}]
        with self.assertRaisesRegex(ValueError, "incomplete content"): self.stream(events)

    def test_unfinished_json_fragments_count_toward_total_response_limit(self):
        events = [{"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "bad", "name": "run_shell", "input": {}}}]
        events += [{"type": "content_block_delta", "index": 0, "delta": {
            "type": "input_json_delta", "partial_json": "x" * 900000}}] * 5
        with self.assertRaisesRegex(ValueError, "4 MB"): self.stream(events)

    def test_split_sse_lines_and_unknown_blocks(self):
        wire = io.BytesIO(b': ping\n\ndata: {"type":\ndata: "ping"}\n\n')
        self.assertEqual(list(GatewayClient.events(wire)), [{"type": "ping"}])
        with self.assertRaises(ValueError): self.stream([{"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "bad"}}])


class ChatGatewayIntegrationTests(unittest.TestCase):
    def test_worker_gateway_and_native_tool_continuation_over_real_local_http(self):
        import test_gateway_hub as fixtures
        from bridge_core import atomic_json, load_settings
        harness = fixtures.GatewayHubHTTPTests(); harness.setUp()
        child = None
        try:
            route = harness.start_gateway("deepseek", "deepseek-flash", {"reasoning_history": "native"})
            settings = load_settings(harness.root); settings["port"] = harness.gateway.server_port
            atomic_json(harness.root / "settings.json", settings)
            (harness.root / "stream.txt").write_text("actual local file result\n")
            env = dict(os.environ, MISTRAL_BRIDGE_STATE_DIR=str(harness.root), PYTHONUNBUFFERED="1")
            child = subprocess.Popen([sys.executable, str(Path(__file__).with_name("chat_runtime.py"))],
                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            inbox = queue.Queue(); collected = []
            def reader():
                for line in child.stdout:
                    inbox.put(json.loads(line))
            thread = threading.Thread(target=reader, daemon=True); thread.start()
            def until(predicate):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    event = inbox.get(timeout=max(.01, deadline - time.monotonic()))
                    collected.append(event)
                    self.assertNotEqual(event["event"], "error", event)
                    if predicate(event): return event
                self.fail("worker did not produce expected event")
            def command(value):
                child.stdin.write(json.dumps(value) + "\n"); child.stdin.flush()
            until(lambda event: event["event"] == "ready")
            command({"command": "create", "choice": route + "|", "workspace": str(harness.root)})
            selected = until(lambda event: event["event"] == "selected")
            command({"command": "send", "id": selected["id"], "text": "Read stream.txt and finish."})
            until(lambda event: event["event"] == "state" and event["busy"] is False)
            entries = [event["entry"] for event in collected if event["event"] == "entry"]
            self.assertTrue(any("Native stream complete." in item["text"] for item in entries))
            self.assertTrue(any("actual local file result" in (item.get("detail") or "") for item in entries))
            store = ChatStore(harness.root, lock=False)
            saved = store.load(selected["id"])
            self.assertEqual(saved["messages"][1]["content"][0]["signature"], "native-provider-signature")
            self.assertIn("actual local file result", json.dumps(saved["messages"][2]))
            self.assertNotIn(fixtures.PROVIDER_KEY, json.dumps(collected))
            child.stdin.close(); child.wait(timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr.read())
            thread.join(1)
        finally:
            if child:
                if child.poll() is None: child.kill(); child.wait()
                for pipe in (child.stdin, child.stdout, child.stderr):
                    if pipe and not pipe.closed: pipe.close()
            harness.tearDown()


if __name__ == "__main__": unittest.main()
