"""Actual encrypted phone requests enter the production ChatHost serial queue."""
from concurrent.futures import Future
import json
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
import unittest

from chat_remote import PhoneClient, encode_line
from chat_runtime import ChatStore
from chat_sessions import ChatHost
from test_chat_runtime import FakeTransport, response


class RemoteHostTests(unittest.TestCase):
    def test_phone_chat_subscription_send_and_revocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = queue.Queue()
            events = []
            def dispatch(callback):
                future = Future(); commands.put((callback, future)); return future.result(timeout=5)
            def dispatch_event(callback):
                commands.put((callback, None)); return True
            def run():
                while True:
                    item = commands.get()
                    if item is None: return
                    callback, future = item
                    try:
                        value = callback()
                        if future: future.set_result(value)
                    except Exception as exc:
                        if future: future.set_exception(exc)
                        else: events.append({"event": "test_error", "message": str(exc)})
            thread = threading.Thread(target=run, daemon=True); thread.start()
            def client():
                transport = FakeTransport([response("Hello from Mac")]); transport.root = root; return transport
            host = ChatHost(ChatStore(root, lock=False), FakeTransport([]), events.append,
                            child_factory=client, dispatch=dispatch, dispatch_event=dispatch_event)
            host.models = FakeTransport([]).catalogue()
            for folder in ("one", "two"):
                workspace = root / folder; workspace.mkdir()
                dispatch(lambda workspace=workspace: host.handle({"command": "create", "workspace": str(workspace), "choice": host.models[0]["id"]}))
            second = host.view_id
            first = next(id for id in host.headers if id != second)
            self.assertIsNone(host.remote)
            dispatch(lambda: host.handle({"command": "remote_start", "host": "127.0.0.1", "port": 0}))
            port = host.remote.status()["port"]
            phone = PhoneClient("providerhub-phone")
            connection = socket.create_connection(("127.0.0.1", port), timeout=3)
            reader = connection.makefile("rb")
            def flush():
                for frame in phone.take(): connection.sendall(encode_line(frame))
            def receive_until(predicate):
                deadline = time.monotonic() + 5
                while not predicate():
                    if time.monotonic() >= deadline: self.fail("Phone state did not settle")
                    line = reader.readline()
                    self.assertTrue(line, "Phone unexpectedly disconnected")
                    phone.feed(json.loads(line)); flush()
            try:
                phone.start(); flush()
                phone.feed(json.loads(reader.readline())); flush()
                deadline = time.monotonic() + 3
                pending = None
                while pending is None and time.monotonic() < deadline:
                    pending = dispatch(lambda: host.remote.status()["pendingPairing"])
                    time.sleep(.01)
                self.assertIsNotNone(pending)
                dispatch(lambda: host.handle({"command": "remote_confirm", "identity": pending["identity"], "code": pending["confirmCode"], "allow": True}))
                receive_until(lambda: phone.established and len(phone.chats) == 2)
                phone.select(first, "select-1"); flush()
                receive_until(lambda: "select-1" in phone.acks)
                self.assertEqual(host.view_id, second, "Phone selection stole desktop focus")
                self.assertEqual(host.subscriptions["remote"], {first})
                phone.send_text(first, "From phone", "send-1"); flush()
                receive_until(lambda: "send-1" in phone.acks)
                self.assertTrue(phone.acks["send-1"]["ok"])
                dispatch(lambda: None)
                self.assertEqual([entry["text"] for entry in host.sessions[first].chat["entries"] if entry["kind"] == "user"], ["From phone"])
                phone.send_text(first, "From phone", "send-1"); flush()
                # Replay uses a new transport seq but the same command identity.
                for _ in range(20):
                    phone.feed(json.loads(reader.readline())); flush()
                    if not host.sessions[first].busy: break
                self.assertEqual(sum(entry["kind"] == "user" for entry in host.sessions[first].chat["entries"]), 1)
                dispatch(lambda: host.handle({"command": "remote_revoke", "identity": phone.identity_b64()}))
                self.assertFalse(host.remote.status()["connected"])
                self.assertTrue(host.remote.status()["devices"][0]["revoked"])
                self.assertFalse(any(e["event"] == "test_error" for e in events), events)
            finally:
                reader.close(); connection.close()
                dispatch(host.shutdown)
                commands.put(None); thread.join(3)


if __name__ == "__main__": unittest.main()
