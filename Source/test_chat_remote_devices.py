"""Independent authenticated devices against the real serial ChatHost."""
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


class Device:
    def __init__(self, test, phone=None, label=None):
        self.test = test
        self.phone = phone or PhoneClient("providerhub-phone")
        self.socket = socket.create_connection(("127.0.0.1", test.port), timeout=3)
        self.reader = self.socket.makefile("rb")
        test.devices.append(self)
        self.phone.start()
        self.flush()
        self.receive(flush=False)
        frames = self.phone.take()
        for frame in frames:
            if label:
                frame["deviceName"] = label
            self.socket.sendall(encode_line(frame))

    def flush(self):
        for frame in self.phone.take():
            self.socket.sendall(encode_line(frame))

    def receive(self, flush=True):
        line = self.reader.readline()
        self.test.assertTrue(line, "Device unexpectedly disconnected")
        self.phone.feed(json.loads(line))
        if flush:
            self.flush()

    def until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            self.test.assertLess(time.monotonic(), deadline, "Device state did not settle")
            self.receive()

    def pair(self):
        identity = self.phone.identity_b64()
        self.test.wait_for(lambda: any(row["identity"] == identity for row in self.test.server.status()["pendingPairings"]))
        self.test.dispatch(lambda: self.test.server.confirm_pairing(identity, self.phone.confirm, True))
        self.until(lambda: self.phone.established and len(self.phone.chats) == 2)

    def select(self, chat, request="select"):
        self.phone.select(chat, request)
        self.flush()
        self.until(lambda: request in self.phone.acks and (self.phone.selected or {}).get("id") == chat)
        self.test.assertTrue(self.phone.acks[request]["ok"])

    def close(self):
        self.reader.close()
        self.socket.close()


class RemoteDeviceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.tasks = queue.Queue()
        self.events = []
        self.devices = []

        def run():
            while True:
                item = self.tasks.get()
                if item is None:
                    return
                callback, future = item
                try:
                    value = callback()
                    if future:
                        future.set_result(value)
                except Exception as exc:
                    if future:
                        future.set_exception(exc)
                    else:
                        self.events.append({"event": "test_error", "message": str(exc)})

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

        def dispatch_event(callback):
            self.tasks.put((callback, None))
            return True

        self.host = ChatHost(ChatStore(root, lock=False), FakeTransport([]), self.events.append,
                             child_factory=lambda: FakeTransport([response("Done")]),
                             dispatch=self.dispatch, dispatch_event=dispatch_event)
        self.host.models = FakeTransport([]).catalogue()
        for folder in ("one", "two"):
            workspace = root / folder
            workspace.mkdir()
            self.dispatch(lambda: self.host.handle({"command": "create", "workspace": str(workspace), "choice": self.host.models[0]["id"]}))
        self.chats = list(self.host.headers)
        self.desktop = self.host.view_id
        self.dispatch(lambda: self.host.handle({"command": "remote_start", "host": "127.0.0.1", "port": 0}))
        self.server = self.host.remote
        self.port = self.server.port

    def dispatch(self, callback):
        future = Future()
        self.tasks.put((callback, future))
        return future.result(timeout=5)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while not self.dispatch(predicate):
            self.assertLess(time.monotonic(), deadline, "Host state did not settle")
            time.sleep(.01)

    def tearDown(self):
        for device in self.devices:
            device.close()
        self.dispatch(self.host.shutdown)
        self.tasks.put(None)
        self.thread.join(3)
        self.assertFalse(any(row["event"] == "test_error" for row in self.events), self.events)
        self.directory.cleanup()

    def test_phone_and_ipad_pair_select_stream_and_disconnect_independently(self):
        phone = Device(self, label="iPhone")
        ipad = Device(self, label="iPad")
        self.wait_for(lambda: len(self.server.status()["pendingPairings"]) == 2)
        phone.pair()
        ipad.pair()
        self.assertEqual(self.server.status()["connectedCount"], 2)
        self.assertEqual({row["label"] for row in self.server.status()["devices"]}, {"iPhone", "iPad"})
        phone.select(self.chats[0])
        ipad.select(self.chats[1])
        self.assertEqual(self.host.view_id, self.desktop)
        self.assertEqual(set(self.server.selections.values()), set(self.chats))
        self.assertEqual(set().union(*self.host.subscriptions.values()), set(self.chats))
        for device, chat, text in [(phone, self.chats[0], "Phone reply"), (ipad, self.chats[1], "iPad reply")]:
            self.dispatch(lambda: self.host.emit({"event": "delta", "chat": chat, "text": text}))
            device.until(lambda: (device.phone.selected or {}).get("live") == text)
            self.assertEqual(device.phone.selected["id"], chat)
            self.assertFalse(device.phone.needs_snapshot)
        # Equal request IDs belong to each device, so both commands execute.
        for device, chat in [(phone, self.chats[0]), (ipad, self.chats[1])]:
            device.phone.stop(chat, "same-request")
            device.flush()
            device.until(lambda: "same-request" in device.phone.acks)
            self.assertTrue(device.phone.acks["same-request"]["ok"])
        phone.close()
        self.wait_for(lambda: self.server.status()["connectedCount"] == 1)
        self.assertEqual(list(self.host.subscriptions.values()), [{self.chats[1]}])
        ipad.phone.stop(self.chats[1], "still-connected")
        ipad.flush()
        ipad.until(lambda: "still-connected" in ipad.phone.acks)
        self.assertTrue(ipad.phone.acks["still-connected"]["ok"])

    def test_removed_older_companion_can_pair_again_only_after_fresh_confirmation(self):
        # No deviceName/new fields: this is the original companion wire format.
        device = Device(self)
        device.pair()
        identity = device.phone.identity_b64()
        self.dispatch(lambda: self.server.revoke(identity))
        self.assertFalse(self.server.status()["connected"])
        reconnect = Device(self, PhoneClient("providerhub-phone", identity=device.phone.identity))
        self.wait_for(lambda: bool(self.server.status()["pendingPairings"]))
        self.assertFalse(reconnect.phone.established)
        self.assertTrue(self.server.bridge.registry.revoked(identity))
        with self.assertRaisesRegex(Exception, "no longer pending"):
            self.dispatch(lambda: self.server.confirm_pairing(identity, "wrong-code", True))
        self.assertTrue(self.server.bridge.registry.revoked(identity))
        reconnect.pair()
        self.assertFalse(self.server.bridge.registry.revoked(identity))
        reconnect.select(self.chats[0])
        saved = json.loads(self.server.path.read_text())
        self.assertFalse(saved["devices"][identity]["revoked"])

    def test_untrusted_and_revoked_devices_do_not_evict_paired_ipad(self):
        ipad = Device(self, label="iPad")
        ipad.pair()
        ipad.select(self.chats[1])
        unknown = Device(self)
        self.wait_for(lambda: bool(self.server.status()["pendingPairings"]))
        self.dispatch(lambda: self.server.confirm_pairing(unknown.phone.identity_b64(), unknown.phone.confirm, False))
        self.assertEqual(self.server.status()["connectedCount"], 1)
        ipad.phone.stop(self.chats[1], "after-rejection")
        ipad.flush()
        ipad.until(lambda: "after-rejection" in ipad.phone.acks)
        self.assertTrue(ipad.phone.acks["after-rejection"]["ok"])
        phone = Device(self)
        phone.pair()
        self.dispatch(lambda: self.server.revoke(phone.phone.identity_b64()))
        self.assertEqual(self.server.status()["connectedCount"], 1)
        removed = Device(self, PhoneClient("providerhub-phone", identity=phone.phone.identity))
        self.wait_for(lambda: bool(self.server.status()["pendingPairings"]))
        self.assertFalse(removed.phone.established)
        self.assertEqual(self.server.status()["connectedCount"], 1)
        ipad.phone.stop(self.chats[1], "after-other-device-revoked")
        ipad.flush()
        ipad.until(lambda: "after-other-device-revoked" in ipad.phone.acks)
        self.assertTrue(ipad.phone.acks["after-other-device-revoked"]["ok"])

    def test_shared_approval_is_executed_once_when_two_devices_answer(self):
        executed = []
        self.server.bridge.execute = executed.append
        phone = Device(self)
        phone.pair()
        ipad = Device(self)
        ipad.pair()
        chat, approval = self.chats[0], "ab" * 16
        self.dispatch(lambda: self.server.publish({"event": "approval", "chat": chat,
                                                 "approval": {"id": approval, "summary": "Edit a file"}}))
        for device, allow in [(phone, True), (ipad, False)]:
            device.until(lambda: (device.phone.approval or {}).get("id") == approval)
            device.phone.approve(chat, approval, allow, "answer")
            device.flush()
            device.until(lambda: "answer" in device.phone.acks)
            self.assertTrue(device.phone.acks["answer"]["ok"])
            self.assertTrue(device.phone.acks["answer"]["allow"])
        self.assertEqual(executed, [{"command": "approve", "chat": chat, "id": approval, "allow": True}])
        self.assertTrue(ipad.phone.acks["answer"]["duplicate"])

    def test_same_identity_reconnect_replaces_only_its_own_connection(self):
        phone = Device(self)
        phone.pair()
        ipad = Device(self, label="iPad")
        ipad.pair()
        ipad.select(self.chats[1])
        reconnect = Device(self, PhoneClient("providerhub-phone", identity=phone.phone.identity))
        reconnect.until(lambda: reconnect.phone.established and len(reconnect.phone.chats) == 2)
        self.wait_for(lambda: len(self.server.connections) == 2)
        self.assertEqual(self.server.status()["connectedCount"], 2)
        self.assertEqual(list(self.server.selections.values()), [self.chats[1]])

    def test_chat_event_producers_can_read_selections_without_network_lock(self):
        phone = Device(self)
        phone.pair()
        phone.select(self.chats[0])
        ready = threading.Event()
        result = []
        def read_selection():
            result.append(self.server.selections)
            ready.set()
        with self.server.lock:
            producer = threading.Thread(target=read_selection)
            producer.start()
            completed = ready.wait(1)
        producer.join(2)
        self.assertTrue(completed, "Chat/output-lock holders must not wait on the network lock")
        self.assertEqual(list(result[0].values()), [self.chats[0]])


if __name__ == "__main__":
    unittest.main()
