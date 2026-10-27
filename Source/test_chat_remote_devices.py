"""Independent authenticated devices against the real serial ChatHost."""
from concurrent.futures import Future
import errno
import json
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from chat_remote import MacBridge, PhoneClient, RemoteError, encode_line
from chat_runtime import ChatStore, entry
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

    def test_repeated_backpressure_refreshes_both_devices_without_closing_listener(self):
        phone, ipad = Device(self), Device(self)
        phone.pair(); ipad.pair()
        phone.select(self.chats[0]); ipad.select(self.chats[1])
        listener = self.server.listener
        original_dispatch = self.host.dispatch_event

        def bounded_dispatch(callback):
            if self.tasks.qsize() >= 32:
                return False
            return original_dispatch(callback)

        self.host.dispatch_event = bounded_dispatch
        for wave in range(2):
            expected = {}

            def burst():
                for chat in self.chats:
                    service = self.host.sessions[chat]
                    row = entry("assistant", f"Recovered wave {wave} for {chat}")
                    service.chat["entries"].append(row)
                    expected[chat] = row["text"]
                    for offset in range(350):
                        self.host.emit({"event": "delta", "chat": chat, "id": row["id"],
                                        "text": "x", "offset": offset})
                self.host.sessions[self.chats[0]].approval = (
                    {"id": "current-approval", "summary": "Still pending", "detail": "Current action"}
                    if wave else None)
                self.host.emit({"event": "approval", "chat": self.chats[0],
                                "approval": {"id": "stale", "summary": "Already answered"}})

            self.dispatch(burst)
            for device, chat in [(phone, self.chats[0]), (ipad, self.chats[1])]:
                device.until(lambda: any(row["text"] == expected[chat] for row in (device.phone.selected or {}).get("entries", [])))
                self.assertFalse(device.phone.needs_snapshot)
                self.assertEqual(device.phone.selected["id"], chat)
            self.assertEqual((phone.phone.approval or {}).get("id"), "current-approval" if wave else None)
            self.assertIsNone(ipad.phone.approval)
            self.assertIs(self.server.listener, listener)
            self.assertEqual(self.server.status()["connectedCount"], 2)
            self.assertEqual(self.host.view_id, self.desktop)
        # A refreshed cursor must continue accepting ordinary incremental updates.
        self.dispatch(lambda: self.host.emit({"event": "delta", "chat": self.chats[0], "id": "after-recovery", "text": "Still live", "offset": 0}))
        phone.until(lambda: (phone.phone.selected or {}).get("live") == "Still live")

    def test_busy_dispatch_retries_without_needing_another_event(self):
        phone = Device(self); phone.pair(); phone.select(self.chats[0])
        listener = self.server.listener
        original_dispatch = self.host.dispatch_event
        attempts = []

        def busy_dispatch(callback):
            attempts.append(True)
            return False if len(attempts) <= 3 else original_dispatch(callback)

        self.host.dispatch_event = busy_dispatch

        def update():
            row = entry("assistant", "Delivered after the queue became available")
            self.host.sessions[self.chats[0]].chat["entries"].append(row)
            self.host.emit({"event": "entry", "chat": self.chats[0], "entry": row})

        self.dispatch(update)
        phone.until(lambda: any(row["text"] == "Delivered after the queue became available" for row in (phone.phone.selected or {}).get("entries", [])))
        self.assertGreaterEqual(len(attempts), 4)
        self.assertIs(self.server.listener, listener)

    def test_updates_during_refresh_cannot_overwrite_newer_snapshot_state(self):
        phone = Device(self); phone.pair(); phone.select(self.chats[0])
        dispatch = self.host.dispatch_event
        attempted = []

        def busy_once(callback):
            attempted.append(True)
            return False if len(attempted) == 1 else dispatch(callback)

        self.host.dispatch_event = busy_once
        row = entry("assistant", "Newest authoritative text")
        publish_workspaces = self.host.publish_workspaces
        raced = threading.Event()

        def concurrent_update():
            if not raced.is_set():
                raced.set()
                # An already-created producer event arrives while the refresh
                # is reading a newer copy of that same transcript entry.
                producer = threading.Thread(target=lambda: self.host.emit(
                    {"event": "entry", "chat": self.chats[0], "entry": {**row, "text": "Older text"}}))
                producer.start(); producer.join(1)
                self.assertFalse(producer.is_alive())
            publish_workspaces()

        with patch.object(self.host, "publish_workspaces", side_effect=concurrent_update):
            def update():
                self.host.sessions[self.chats[0]].chat["entries"].append(row)
                self.host.emit({"event": "entry", "chat": self.chats[0], "entry": row})
            self.dispatch(update)
            phone.until(lambda: any(item["id"] == row["id"] for item in (phone.phone.selected or {}).get("entries", [])))
            self.wait_for(lambda: not self.host._remote_scheduled)
        self.assertTrue(raced.is_set())
        self.dispatch(lambda: self.host.emit({"event": "delta", "chat": self.chats[0], "id": "after-refresh", "text": "Settled", "offset": 0}))
        phone.until(lambda: (phone.phone.selected or {}).get("live") == "Settled")
        self.assertEqual(next(item["text"] for item in phone.phone.selected["entries"] if item["id"] == row["id"]), row["text"])

    def test_publish_failure_retries_a_fresh_snapshot_without_stopping(self):
        phone = Device(self); phone.pair(); phone.select(self.chats[0])
        listener = self.server.listener
        publish = self.server.publish
        attempts = []

        def transient_failure(event):
            attempts.append(True)
            if len(attempts) == 1:
                raise RemoteError("temporary delivery failure")
            return publish(event)

        with patch.object(self.server, "publish", side_effect=transient_failure):
            def update():
                row = entry("assistant", "Recovered from a publish failure")
                self.host.sessions[self.chats[0]].chat["entries"].append(row)
                self.host.emit({"event": "entry", "chat": self.chats[0], "entry": row})
            self.dispatch(update)
            phone.until(lambda: any(row["text"] == "Recovered from a publish failure" for row in (phone.phone.selected or {}).get("entries", [])))
        self.assertIs(self.server.listener, listener)
        self.assertEqual(self.server.status()["connectedCount"], 1)

    def test_user_disable_cancels_pending_delivery_retries(self):
        attempted = threading.Event()

        def busy_dispatch(callback):
            attempted.set()
            return False

        self.host.dispatch_event = busy_dispatch
        self.dispatch(lambda: self.host.emit({"event": "chats", "chats": []}))
        self.assertTrue(attempted.wait(1))
        self.dispatch(lambda: self.host.handle({"command": "remote_stop"}))
        attempted.clear()
        self.assertFalse(attempted.wait(.35), "Remote access kept retrying after the user disabled it")
        self.assertIsNone(self.server.listener)

    def test_one_device_publish_error_preserves_listener_and_other_device(self):
        phone, ipad = Device(self), Device(self)
        phone.pair(); ipad.pair()
        phone.select(self.chats[0]); ipad.select(self.chats[1])
        listener = self.server.listener
        bridge = self.dispatch(lambda: next(bridge for bridge in self.server.connections.values()
                                             if bridge.peer == phone.phone.identity_b64()))
        with patch.object(bridge, "publish", side_effect=RemoteError("invalid device frame")):
            self.dispatch(lambda: self.host.emit({"event": "delta", "chat": self.chats[1], "text": "iPad stays live", "offset": 0}))
            ipad.until(lambda: (ipad.phone.selected or {}).get("live") == "iPad stays live")
            self.wait_for(lambda: self.server.status()["connectedCount"] == 1)
        self.assertIs(self.server.listener, listener)
        reconnect = Device(self, PhoneClient("providerhub-phone", identity=phone.phone.identity))
        reconnect.until(lambda: reconnect.phone.established and len(reconnect.phone.chats) == 2)
        reconnect.select(self.chats[0], request="select-after-recovery")
        self.assertEqual(self.server.status()["connectedCount"], 2)

    def test_transient_accept_error_does_not_strand_listener(self):
        listener = self.server.listener
        accept = socket.socket.accept
        failed = threading.Event()

        def interrupted_accept(connection):
            if connection is listener and not failed.is_set():
                failed.set()
                raise OSError(errno.EMFILE, "temporary file descriptor pressure")
            return accept(connection)

        with patch.object(socket.socket, "accept", interrupted_accept):
            self.assertTrue(failed.wait(2))
            phone = Device(self)
            phone.pair(); phone.select(self.chats[0])
        self.assertIs(self.server.listener, listener)
        self.assertEqual(self.server.status()["connectedCount"], 1)

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

    def test_closed_socket_reader_cannot_overwrite_healthy_pairing_status(self):
        device = Device(self)
        device.pair()
        class RetiredSocket:
            def recv(self, size):
                raise OSError(errno.EBADF, "Bad file descriptor")
        # This models recv completing after reconnect/revocation has retired
        # that socket. A healthy device remains on the same server.
        self.server._reader(RetiredSocket())
        self.dispatch(lambda: None)
        self.assertIsNone(self.server.status()["error"])
        self.assertEqual(self.server.status()["connectedCount"], 1)
        device.select(self.chats[0])

    def test_live_socket_reader_failure_is_still_reported_and_disconnected(self):
        class FailingSocket:
            closed = False
            def recv(self, size):
                raise OSError(errno.ECONNRESET, "Connection reset by peer")
            def shutdown(self, how):
                pass
            def close(self):
                self.closed = True
        connection = FailingSocket()
        def attach():
            self.server.connections[connection] = MacBridge(lambda _: None, "providerhub-phone")
        self.dispatch(attach)
        self.server._reader(connection)
        self.wait_for(lambda: connection not in self.server.connections)
        self.assertIn("Connection reset by peer", self.server.status()["error"])
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
