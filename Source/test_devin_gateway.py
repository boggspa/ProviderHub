"""HTTP integration tests for the Devin agent-session gateway endpoints.

All Devin upstreams are local deterministic mocks. No credential lookup or
paid inference is performed.
"""
from __future__ import annotations

import copy
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


DEVIN_KEY = "pat_test-fixture-key-never-sent"
ORG_ID = "org-test-123"
SESSION_ID = "session-abc-456"


class MockDevin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests = []
    request_headers = []
    lock = threading.Lock()

    def log_message(self, *_):
        pass

    @classmethod
    def reset(cls):
        cls.requests = []
        cls.request_headers = []

    def read_body(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.loads(raw) if raw else {}
        with type(self).lock:
            type(self).requests.append(body)
            type(self).request_headers.append({k.lower(): v for k, v in self.headers.items()})
        return body

    def send_json(self, status, value):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def _session(self):
        return {
            "id": SESSION_ID,
            "task": "Write a sort function",
            "mode": "normal",
            "state": "running",
            "createdAt": "2026-09-13T00:00:00Z",
            "updatedAt": "2026-09-13T00:01:00Z",
        }

    def do_GET(self):
        if self.path == f"/v3/organizations/{ORG_ID}/sessions":
            self.send_json(200, {"data": [self._session()], "cursor": None})
            return
        if self.path == f"/v3/organizations/{ORG_ID}/sessions/{SESSION_ID}":
            self.send_json(200, self._session())
            return
        self.send_json(404, {"error": {"message": "unexpected mock path"}})

    def do_POST(self):
        self.read_body()
        if self.path == f"/v3/organizations/{ORG_ID}/sessions":
            self.send_json(200, self._session())
            return
        if self.path == f"/v3/organizations/{ORG_ID}/sessions/{SESSION_ID}/archive":
            self.send_json(200, {**self._session(), "state": "archived"})
            return
        if self.path == f"/v3/organizations/{ORG_ID}/sessions/{SESSION_ID}/messages":
            self.send_json(200, {"content": "task submitted"})
            return
        self.send_json(404, {"error": {"message": "unexpected mock path"}})


class DevinGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        MockDevin.reset()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), MockDevin)
        self.upstream.daemon_threads = True
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.runtime = None
        self.gateway = None

    def tearDown(self):
        if self.gateway is not None:
            self.gateway.shutdown()
            self.gateway.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.temp.cleanup()

    def start_gateway(self):
        vibe = {
            "active_model": "unused",
            "active_display_name": "Unused",
            "key_name": "MISTRAL_API_KEY",
            "vibe_home": str(self.root / "vibe"),
            "configured_models": [],
        }
        with patch("bridge_core.vibe_settings", return_value=vibe):
            settings = default_settings()
        route = "devin/normal"
        settings["mappings"] = {slot[0]: route for slot in SLOTS}
        # Devin's org_id lives on the provider connection (a non-secret routing
        # field), alongside the standard region/base_url defaults.
        settings["providers"]["devin"]["org_id"] = ORG_ID
        atomic_json(self.root / "settings.json", settings)
        entry = {
            "id": "normal",
            "canonical_id": "normal",
            "display_name": "Devin Normal",
            "context": None,
            "aliases": ["normal"],
            "tools": True,
            "vision": False,
            "reasoning": True,
            "effort_modes": [],
            "fast_mode": False,
            "inference_status": "advertised",
            "source": "provider_documentation",
            "evidence": "local-test",
        }
        atomic_json(self.root / "catalogues" / "devin.json", {
            "provider_id": "devin",
            "source": "provider_documentation",
            "connection_signature": connection_signature(
                "devin", settings["providers"]["devin"]),
            "models": [entry],
        })
        upstream_url = f"http://127.0.0.1:{self.upstream.server_port}"
        with patch("bridge_core.vibe_settings", return_value=vibe):
            self.runtime = Runtime(self.root, upstream_url=upstream_url, key=DEVIN_KEY)
        self.gateway = Server(self.runtime, 0)
        threading.Thread(target=self.gateway.serve_forever, daemon=True).start()

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=6)
        headers = {"Authorization": "Bearer " + self.runtime.token}
        raw = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            raw = json.dumps(body)
        connection.request(method, path, raw, headers)
        response = connection.getresponse()
        data = response.read()
        status = response.status
        connection.close()
        parsed = json.loads(data) if data else None
        return status, parsed

    def test_agent_catalogue(self):
        self.start_gateway()
        status, body = self.request("GET", "/v1/agents/catalogue")
        self.assertEqual(status, 200)
        self.assertEqual(body["provider_id"], "devin")
        self.assertEqual({m["id"] for m in body["modes"]}, {"normal", "fast", "lite", "ultra", "fusion"})

    def test_list_sessions(self):
        self.start_gateway()
        status, body = self.request("GET", "/v1/agents/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"][0]["id"], SESSION_ID)
        self.assertEqual(body["data"][0]["state"], "running")

    def test_get_session(self):
        self.start_gateway()
        status, body = self.request("GET", f"/v1/agents/sessions/{SESSION_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], SESSION_ID)

    def test_create_session(self):
        self.start_gateway()
        status, body = self.request("POST", "/v1/agents/sessions", {"task": "Do a thing", "mode": "normal"})
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], SESSION_ID)

    def test_create_session_rejects_missing_task(self):
        self.start_gateway()
        status, body = self.request("POST", "/v1/agents/sessions", {"mode": "normal"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_cancel_session(self):
        self.start_gateway()
        status, body = self.request("POST", f"/v1/agents/sessions/{SESSION_ID}/cancel")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "archived")

    def test_archive_session(self):
        self.start_gateway()
        status, body = self.request("POST", f"/v1/agents/sessions/{SESSION_ID}/archive")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "archived")

    def test_submit_task(self):
        self.start_gateway()
        status, body = self.request("POST", f"/v1/agents/sessions/{SESSION_ID}/messages", {"content": "keep going"})
        self.assertEqual(status, 200)
        self.assertEqual(body["content"], "task submitted")

    def test_unknown_agent_endpoint(self):
        self.start_gateway()
        status, body = self.request("GET", "/v1/agents/sessions/")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
