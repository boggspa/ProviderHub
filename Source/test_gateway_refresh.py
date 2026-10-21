"""Live routing refresh preserves real in-flight requests and shared accounting."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from bridge_core import SLOTS, atomic_json, default_settings
from gateway import Runtime, Server
from hub_config import connection_signature


class GatewayRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entered, self.release = threading.Event(), threading.Event()
        entered, release = self.entered, self.release
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                model = body["model"]
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    def emit(kind, **value):
                        self.wfile.write(b"event: " + kind.encode() + b"\ndata: " + json.dumps({"type": kind, **value}).encode() + b"\n\n")
                        self.wfile.flush()
                    emit("message_start", message={"id": "m", "type": "message", "role": "assistant", "model": model,
                         "content": [], "usage": {"input_tokens": 2, "output_tokens": 0}})
                    emit("content_block_start", index=0, content_block={"type": "text", "text": ""})
                    if model == "old":
                        entered.set()
                        release.wait(10)
                    emit("content_block_delta", index=0, delta={"type": "text_delta", "text": model})
                    emit("content_block_stop", index=0)
                    emit("message_delta", delta={"stop_reason": "end_turn"}, usage={"output_tokens": 1})
                    emit("message_stop")
                    self.close_connection = True
                    return
                if model == "old":
                    entered.set()
                    if not release.wait(10):
                        self.send_error(504)
                        return
                result = json.dumps({"id": "m", "type": "message", "role": "assistant", "model": model,
                    "content": [{"type": "text", "text": model}], "stop_reason": "end_turn",
                    "usage": {"input_tokens": 2, "output_tokens": 1}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(result)))
                self.end_headers()
                self.wfile.write(result)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        self.upstream.daemon_threads = True
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.addCleanup(self.release.set)
        self.vibe = {"active_model": "unused", "active_display_name": "Unused", "key_name": "MISTRAL_API_KEY",
                     "vibe_home": str(self.root / "vibe"), "configured_models": []}
        self.mock_vibe = patch("bridge_core.vibe_settings", return_value=self.vibe)
        self.mock_vibe.start()
        self.addCleanup(self.mock_vibe.stop)
        self.settings = default_settings()
        self.settings["mappings"] = {slot[0]: "minimax/old" for slot in SLOTS}
        atomic_json(self.root / "settings.json", self.settings)
        atomic_json(self.root / "catalogues/minimax.json", {"provider_id": "minimax", "source": "mock_metadata",
            "connection_signature": connection_signature("minimax", self.settings["providers"]["minimax"]),
            "models": [{"id": model, "canonical_id": model, "display_name": model, "context": 131072,
                        "aliases": [model], "tools": True, "vision": False, "reasoning": False,
                        "effort_modes": [], "inference_status": "advertised", "source": "mock_metadata"} for model in ["old", "new"]]})
        self.runtime = Runtime(self.root, upstream_url=f"http://127.0.0.1:{self.upstream.server_port}", key="test-key")
        self.server = Server(self.runtime, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, body, token=None, connection=None):
        connection = connection or http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        connection.request("POST", path, json.dumps(body), {"Authorization": "Bearer " + (token or self.runtime.token),
                                                            "Content-Type": "application/json"})
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def prepare_new(self):
        self.settings["mappings"] = {slot[0]: "minimax/new" for slot in SLOTS}
        atomic_json(self.root / "settings.json", self.settings)
        prepared = Runtime(self.root)
        return {"catalogue_fingerprint": prepared.catalogue_fingerprint,
                "codex_catalogue_digest": prepared.codex_catalogue_digest}

    def test_refresh_during_request_preserves_old_request_and_routes_new_requests(self):
        result = []
        payload = {"model": "claude-opus-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hello"}]}
        worker = threading.Thread(target=lambda: result.append(self.request("/v1/messages", payload)))
        worker.start()
        try:
            self.assertTrue(self.entered.wait(5), "Old request never reached the provider")
            before = self.runtime.status()
            self.assertEqual(before["active"], 1)
            expected = self.prepare_new()
            status, refreshed = self.request("/_bridge/refresh", expected)
            self.assertEqual(status, 200, refreshed)
            self.assertEqual(refreshed["pid"], before["pid"])
            self.assertEqual(refreshed["active"], 1)
            self.assertFalse(self.runtime.stopping.is_set())
            self.assertTrue(worker.is_alive(), "Refresh severed the old request")
            status, new = self.request("/v1/messages", payload)
            self.assertEqual(status, 200, new)
            self.assertEqual(new["content"][0]["text"], "new")
        finally:
            self.release.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0][0], 200, result)
        self.assertEqual(result[0][1]["content"][0]["text"], "old")
        self.assertEqual(self.runtime.status()["completed"], 2)
        self.assertEqual(self.runtime.status()["active"], 0)

    def test_snapshot_pins_metadata_and_shares_lifetime_and_counters(self):
        old = self.runtime.request_snapshot()
        self.assertEqual(old.resolve_route("claude-opus-5"), "minimax/old")
        expected = self.prepare_new()
        self.runtime.refresh_routes(expected)
        new = self.runtime.request_snapshot()
        self.assertEqual(old.resolve_route("claude-opus-5"), "minimax/old")
        self.assertEqual(new.resolve_route("claude-opus-5"), "minimax/new")
        self.assertIs(old.lock, new.lock)
        self.assertIs(old.admission, new.admission)
        self.assertIs(old.response_ownership, new.response_ownership)
        with old.lock:
            old.active += 1
        self.assertEqual(new.active, 1)
        old.record("completed", "minimax/old", 200, {"input_tokens": 3, "output_tokens": 2})
        self.assertEqual(self.runtime.completed, 1)
        self.assertEqual(new.completed, 1)

    def test_refresh_preserves_stream_through_message_stop(self):
        result = []
        payload = {"model": "claude-opus-5", "stream": True, "max_tokens": 64,
                   "messages": [{"role": "user", "content": "hello"}]}
        def streaming_request():
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
            try:
                connection.request("POST", "/v1/messages", json.dumps(payload), {"Authorization": "Bearer " + self.runtime.token})
                response = connection.getresponse()
                result.append((response.status, response.read().decode()))
            finally:
                connection.close()
        worker = threading.Thread(target=streaming_request)
        worker.start()
        try:
            self.assertTrue(self.entered.wait(5))
            self.assertEqual(self.request("/_bridge/refresh", self.prepare_new())[0], 200)
            self.assertTrue(worker.is_alive())
            self.assertEqual(self.runtime.active, 1)
        finally:
            self.release.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0][0], 200)
        self.assertIn('"text": "old"', result[0][1])
        self.assertIn("message_stop", result[0][1])
        self.assertEqual(self.runtime.completed, 1)

    def test_refresh_requires_auth_and_matching_prepared_snapshot(self):
        before = self.runtime.settings
        expected = self.prepare_new()
        self.assertEqual(self.request("/_bridge/refresh", expected, token="wrong")[0], 401)
        status, value = self.request("/_bridge/refresh", {"catalogue_fingerprint": "outdated"})
        self.assertEqual(status, 400, value)
        self.assertIs(self.runtime.settings, before)
        for invalid in [{}, {"unknown": "value"}, {"catalogue_fingerprint": 5}]:
            self.assertEqual(self.request("/_bridge/refresh", invalid)[0], 400)
        self.assertEqual(self.request("/_bridge/refresh", expected)[0], 200)

    def test_refresh_does_not_move_listener_or_refresh_a_stopping_service(self):
        before = self.runtime.settings
        expected = self.prepare_new()
        self.settings["port"] += 1
        atomic_json(self.root / "settings.json", self.settings)
        self.assertEqual(self.request("/_bridge/refresh", expected)[0], 400)
        self.assertIs(self.runtime.settings, before)
        self.settings["port"] -= 1
        atomic_json(self.root / "settings.json", self.settings)
        self.runtime.stopping.set()
        self.assertEqual(self.request("/_bridge/refresh", expected)[0], 400)
        self.assertIs(self.runtime.settings, before)

    def test_keepalive_captures_current_snapshot_for_each_request(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        headers = {"Authorization": "Bearer " + self.runtime.token}
        connection.request("GET", "/_bridge/status", headers=headers)
        response = connection.getresponse()
        old = json.loads(response.read())
        expected = self.prepare_new()
        self.assertEqual(self.request("/_bridge/refresh", expected)[0], 200)
        connection.request("GET", "/_bridge/status", headers=headers)
        response = connection.getresponse()
        new = json.loads(response.read())
        self.assertNotEqual(old["catalogue_fingerprint"], new["catalogue_fingerprint"])
        self.assertEqual(new["catalogue_fingerprint"], expected["catalogue_fingerprint"])


if __name__ == "__main__":
    unittest.main()
