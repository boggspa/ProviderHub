"""The gateway as a service: shared by every client, stopped only on request."""
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from bridge_core import atomic_json, default_settings, gateway_token
from gateway import Runtime, Server
import gateway_service


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def alive(pid):
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


class GatewayServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "state"
        self.root.mkdir(mode=0o700)
        settings = default_settings()
        settings["port"] = free_port()
        atomic_json(self.root / "settings.json", settings)
        self.addCleanup(lambda: gateway_service.held(self.root) and gateway_service.stop(self.root))

    def post(self, body, token):
        connection = http.client.HTTPConnection("127.0.0.1", json.loads((self.root / "settings.json").read_text())["port"], timeout=5)
        try:
            connection.request("POST", "/_bridge/shutdown", body, {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            return connection.getresponse().status
        finally:
            connection.close()

    def test_one_detached_gateway_is_shared_and_stops_only_on_an_authenticated_request(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(gateway_service.ensure(self.root))) for _ in range(3)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(60)
        self.assertEqual(len(results), 3)
        self.assertEqual(len({result["pid"] for result in results}), 1, results)
        self.assertEqual(sorted(result["started"] for result in results), [False, False, True])
        pid = results[0]["pid"]
        self.assertNotEqual(os.getsid(pid), os.getsid(0), "the gateway stayed in its starter's session")

        self.assertEqual(self.post(b'{"ifIdle": false}', "wrong"), 401)
        self.assertEqual(self.post(b'{"ifIdle": "yes"}', gateway_token(self.root)), 400)
        self.assertEqual(gateway_service.status(self.root)["pid"], pid)
        self.assertEqual(gateway_service.stop(self.root, if_idle=True), {"running": False, "pid": pid})
        deadline = time.monotonic() + 10
        while alive(pid) and time.monotonic() < deadline: time.sleep(.05)
        self.assertFalse(alive(pid))
        self.assertIsNone(gateway_service.status(self.root))
        self.assertEqual(gateway_service.stop(self.root), {"running": False})

    def test_idle_stop_is_refused_while_requests_are_active(self):
        runtime = Runtime(self.root)
        server = Server(runtime, 0)
        stopped = threading.Event()
        server.stop_service = stopped.set
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        def post(body):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("POST", "/_bridge/shutdown", json.dumps(body),
                                   {"Authorization": "Bearer " + runtime.token, "Content-Type": "application/json"})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        with runtime.lock: runtime.active = 1
        status, body = post({"ifIdle": True})
        self.assertEqual(status, 409)
        self.assertIn("active requests", body["error"]["message"])
        self.assertFalse(stopped.is_set())
        status, body = post({"ifIdle": False})
        self.assertEqual((status, body["active"]), (202, 1))
        self.assertTrue(stopped.wait(5))


if __name__ == "__main__":
    unittest.main()
