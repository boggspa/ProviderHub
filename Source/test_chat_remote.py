"""Replay, dedup, revocation, tamper, and TaskWraith E2EE golden vectors.

Vectors are copied from AGBench src/shared/e2ee/crossImplVectors.test.ts
(2026-06-11). They lock this port to the audited taskwraith-e2ee-v1 bytes.
"""
import json
import socket
import threading
import unittest
import tempfile
import queue
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

from chat_remote import (
    E2EE_PROTOCOL, MacBridge, PhoneClient, RemoteError, LoopbackAdapter,
    b64d, b64e, confirm_code, derive_session_keys, ed25519_public, exchange,
    open_frame, seal, sign_transcript, transcript_hash, validate_bind_host,
    verify_transcript, x25519_public,
)
from chat_remote import RemoteServer, encode_line
from chat_remote import project_event


class PresentationProjectionTests(unittest.TestCase):
    def test_question_projection_excludes_private_metadata_and_keeps_identity(self):
        from chat_questions import validate
        question = {"id":"ab"*16, "status":"pending", "mode":"wait", "asker":"Helper", "answers":{},
                    "questions":validate("request_user_input", {"questions":[{"id":"choice", "question":"Which one?"}]}),
                    "private":"do not send"}
        projected = project_event({"event":"entry", "chat":"cd"*16, "entry":{"id":"ef"*16,"kind":"question","question":question}})
        self.assertEqual(projected["entry"]["question"]["id"], question["id"])
        self.assertNotIn("private", projected["entry"]["question"])

    def test_long_stream_and_midstream_subscription_keep_bounded_offsets(self):
        bridge = MacBridge(lambda _: None, "projection")
        bridge.publish({"event":"snapshot", "chat":"ab"*16, "entries":[{
            "id":"a1", "kind":"assistant", "text":"Hello ☀", "recorded":False}]})
        self.assertTrue(bridge.view["selected"]["entries"][0]["isLive"])
        bridge.publish({"event":"delta", "chat":"ab"*16, "id":"a1", "offset":7, "text":" again"})
        self.assertEqual(bridge.view["selected"]["live"], "Hello ☀ again")
        first = project_event({"event":"delta", "id":"a1", "offset":0, "text":"😀"*10000})
        second = project_event({"event":"delta", "id":"a1", "offset":10000, "text":" end"})
        self.assertEqual(len(first["text"]), 8000)
        self.assertEqual(second["offset"], len(first["text"]))
        self.assertEqual(second["text"], "")

    def test_display_metadata_has_no_connection_or_reasoning_fields(self):
        projected = project_event({"event":"chats", "chats":[{
            "id":"ab"*16, "title":"Task", "modelLabel":"Sol", "providerLabel":"Codex", "accent":"#705AFF",
            "approvalMode":"manual", "account":"secret", "scope":"private", "workspace":"/private/home"}]})
        row = projected["chats"][0]
        self.assertEqual(row["modelLabel"], "Sol")
        self.assertEqual(row["accent"], "#705AFF")
        self.assertFalse(set(row) & {"account", "scope", "workspace"})
        entry = project_event({"event":"entry", "chat":"ab"*16, "entry":{
            "id":"tool1", "kind":"tool", "summary":"Read source", "detail":"Returned file", "tool":"read_file",
            "thinking":"secret reasoning", "signature":"opaque", "attachments":[{"path":"/private/home"}]}})["entry"]
        self.assertEqual(entry["id"], "tool1")
        self.assertEqual(entry["detail"], "Returned file")
        self.assertFalse(set(entry) & {"thinking", "signature", "attachments"})

    def test_snapshot_is_byte_bounded_and_tools_update_in_place(self):
        event = project_event({"event":"snapshot", "chat":"ab"*16, "entries":[{
            "id":str(i), "kind":"assistant", "text":"é"*4000, "detail":"é"*2000} for i in range(30)]})
        self.assertLess(len(json.dumps(event,ensure_ascii=False).encode()), 50000)
        bridge = MacBridge(lambda _: None, "projection")
        bridge.publish({"event":"snapshot", "chat":"ab"*16, "entries":[]})
        for detail in ("Running…", "Tests passed"):
            bridge.publish({"event":"entry", "chat":"ab"*16, "entry":{"id":"t1", "kind":"tool", "summary":"Test", "detail":detail}})
        self.assertEqual(len(bridge.view["selected"]["entries"]),1)
        self.assertEqual(bridge.view["selected"]["entries"][0]["detail"],"Tests passed")


class NetworkTests(unittest.TestCase):
    def test_private_transport_pair_reconnect_revoke_and_queue(self):
        tasks = queue.Queue()
        commands = []
        owner_threads = []
        def worker():
            while True:
                task = tasks.get()
                if task is None:
                    return
                task()
        thread = threading.Thread(target=worker)
        thread.start()
        with tempfile.TemporaryDirectory() as directory:
            server = RemoteServer(lambda command: (commands.append(command), owner_threads.append(threading.get_ident())),
                                  Path(directory) / "identity.json", dispatch=tasks.put)
            self.assertFalse(server.status()["listening"])
            for address in ("0.0.0.0", "8.8.8.8", "::", "169.254.1.1"):
                with self.assertRaises(ValueError): server.start(address)
            port = server.start("127.0.0.1", 0)
            identity = server.bridge.identity_b64()
            phone = PhoneClient("providerhub-phone")
            def connect():
                sock = socket.create_connection(("127.0.0.1", port), timeout=2)
                reader = sock.makefile("rb")
                phone.start()
                for frame in phone.take(): sock.sendall(encode_line(frame))
                phone.feed(json.loads(reader.readline()))
                for frame in phone.take(): sock.sendall(encode_line(frame))
                return sock, reader
            sock, reader = connect()
            deadline = time.monotonic() + 2
            while server.status()["pendingPairing"] is None and time.monotonic() < deadline: time.sleep(.01)
            pending = server.status()["pendingPairing"]
            self.assertEqual(pending["confirmCode"], phone.confirm)
            self.assertFalse(server.bridge.established)
            server.confirm_pairing(pending["identity"], pending["confirmCode"], True)
            phone.feed(json.loads(reader.readline()))
            for frame in phone.take(): sock.sendall(encode_line(frame))
            phone.feed(json.loads(reader.readline()))
            phone.send_text("ab" * 16, "from socket", "request-1")
            for frame in phone.take(): sock.sendall(encode_line(frame))
            phone.feed(json.loads(reader.readline()))
            self.assertTrue(phone.acks["request-1"]["ok"])
            self.assertEqual(owner_threads, [thread.ident])
            reader.close(); sock.close()
            deadline = time.monotonic() + 2
            while server.status()["connected"] and time.monotonic() < deadline: time.sleep(.01)
            sock, reader = connect()
            phone.feed(json.loads(reader.readline()))
            for frame in phone.take(): sock.sendall(encode_line(frame))
            phone.feed(json.loads(reader.readline()))
            phone.send_text("ab" * 16, "from socket", "request-1")
            for frame in phone.take(): sock.sendall(encode_line(frame))
            phone.feed(json.loads(reader.readline()))
            self.assertEqual(len(commands), 1)
            server.revoke(phone.identity_b64())
            self.assertFalse(server.status()["connected"])
            reader.close(); sock.close(); server.stop()
            restored = RemoteServer(lambda _: None, Path(directory) / "identity.json", dispatch=tasks.put)
            self.assertEqual(restored.bridge.identity_b64(), identity)
            self.assertTrue(restored.bridge.registry.revoked(phone.identity_b64()))
            self.assertEqual((Path(directory) / "identity.json").stat().st_mode & 0o777, 0o600)
            restored.stop()
        tasks.put(None); thread.join(2)

    def test_approval_cannot_cross_chat(self):
        host, mac, phone = pair()
        mac.publish({"event": "approval", "chat": CHAT, "approval": {"id": APPROVAL, "summary": "tool"}})
        exchange(mac, phone)
        phone.approve(OTHER, APPROVAL, True, "wrong-chat")
        exchange(mac, phone)
        self.assertEqual(host.commands, [])
        self.assertFalse(phone.acks["wrong-chat"]["ok"])

CHAT = "ab" * 16
OTHER = "cd" * 16
APPROVAL = "12" * 16


class FakeHost:
    def __init__(self):
        self.commands = []
        self.fail = False

    def handle(self, command):
        if self.fail:
            raise ValueError("This approval is no longer pending.")
        self.commands.append(command)


def pair(host=None, *, accept=True, session_id="pair-1"):
    host = host or FakeHost()
    mac = MacBridge(host.handle, session_id, auto_accept=accept)
    phone = PhoneClient(session_id)
    phone.start()
    exchange(mac, phone)
    return host, mac, phone


class GoldenVectorTests(unittest.TestCase):
    def test_taskwraith_cross_impl_vectors(self):
        mac_eph = X25519PrivateKey.from_private_bytes(bytes([0x11]) * 32)
        phone_eph = X25519PrivateKey.from_private_bytes(bytes([0x22]) * 32)
        mac_id = Ed25519PrivateKey.from_private_bytes(bytes([0x33]) * 32)
        phone_id = Ed25519PrivateKey.from_private_bytes(bytes([0x44]) * 32)
        self.assertEqual(x25519_public(mac_eph).hex(),
                         "7b4e909bbe7ffe44c465a220037d608ee35897d31ef972f07f74892cb0f73f13")
        self.assertEqual(x25519_public(phone_eph).hex(),
                         "0faa684ed28867b97f4a6a2dee5df8ce974e76b7018e3f22a1c4cf2678570f20")
        self.assertEqual(ed25519_public(mac_id).hex(),
                         "17cb79fb2b4120f2b1ec65e4198d6e08b28e813feb01e4a400839b85e18080ce")
        self.assertEqual(ed25519_public(phone_id).hex(),
                         "d759793bbc13a2819a827c76adb6fba8a49aee007f49f2d0992d99b825ad2c48")
        client_nonce, server_nonce = bytes([0xAA]) * 16, bytes([0xBB]) * 16
        digest = transcript_hash(
            "vector-session", b64e(x25519_public(phone_eph)), b64e(x25519_public(mac_eph)),
            b64e(client_nonce), b64e(server_nonce), b64e(ed25519_public(mac_id)),
            b64e(ed25519_public(phone_id)))
        self.assertEqual(digest.hex(), "9b9764575b8a73a88377d24588d88331e84bc7969972b2a50019fe1c0fdb264f")
        self.assertEqual(confirm_code(digest), "390103")
        keys = derive_session_keys(mac_eph, x25519_public(phone_eph), client_nonce, server_nonce)
        self.assertEqual(mac_eph.exchange(
            X25519PublicKey.from_public_bytes(x25519_public(phone_eph))
        ).hex(), "9e004098efc091d4ec2663b4e9f5cfd4d7064571690b4bea97ab146ab9f35056")
        self.assertEqual(keys["mac->iphone"].hex(), "c328ecb05aed3e0c14dc8b25c0f2a4abf4300495c6a158f586d0833bd0b30a0c")
        self.assertEqual(keys["iphone->mac"].hex(), "9a72c741f010d393760f6fab2eb408d047e8014d0d5bda52c50ffb2171a863f0")
        plaintext = json.dumps({"msgId": 1, "method": "bridge.runEvent", "params": {"n": 42}},
                               separators=(",", ":")).encode()
        sealed = seal(keys["mac->iphone"], "mac->iphone", "vector-session", 0, plaintext)
        self.assertEqual(sealed["nonce"].hex(), "000000010000000000000000")
        self.assertEqual(sealed["ct"].hex(),
                         "9eaacd940b78937c9689aa5b22b9d8151f7da06583bb95e9b4f72329fe2ac832066a363840346e697e774e5bbea2ddaa4d5512d17ed28f6d")
        self.assertEqual(sealed["tag"].hex(), "e02aef7c8b154d6fce01878e3e6d649d")
        self.assertEqual(open_frame(keys["mac->iphone"], "mac->iphone", "vector-session", 0, sealed), plaintext)
        signature = sign_transcript(mac_id, digest)
        self.assertEqual(signature.hex(),
                         "7e293c15123713d7392c0e4902313b51aca0d7fb5bb37cee64ac7e76d1e556cf5e6067f5c380ddd7600a5e7bc44a40ebc5db72d0a5fe838d9997d3f2e5635708")
        self.assertTrue(verify_transcript(ed25519_public(mac_id), digest, signature))

    def test_tamper_and_nonce_mismatch_fail_closed(self):
        key = bytes.fromhex("c328ecb05aed3e0c14dc8b25c0f2a4abf4300495c6a158f586d0833bd0b30a0c")
        sealed = seal(key, "mac->iphone", "vector-session", 0, b"hello")
        flipped = dict(sealed, ct=bytes([sealed["ct"][0] ^ 1]) + sealed["ct"][1:])
        with self.assertRaises(RemoteError):
            open_frame(key, "mac->iphone", "vector-session", 0, flipped)
        with self.assertRaises(RemoteError):
            open_frame(key, "mac->iphone", "vector-session", 1, sealed)
        with self.assertRaises(RemoteError):
            seal(key, "mac->iphone", "vector-session", True, b"no")


class BridgeTests(unittest.TestCase):
    def test_paired_question_reply_is_exact_and_deduplicated(self):
        host, mac, phone = pair()
        params = {"chat":CHAT, "questionId":"ab"*16, "answers":{"choice":["Blue"]}, "requestId":"question-answer"}
        phone._send("chat.answer_question", params); exchange(mac, phone)
        phone._send("chat.answer_question", params); exchange(mac, phone)
        self.assertEqual(len(host.commands), 1)
        self.assertEqual(host.commands[0], {"command":"answer_question", "chat":CHAT, "question_id":"ab"*16,
                                             "answers":{"choice":["Blue"]}, "dismiss":False})
        self.assertTrue(phone.acks["question-answer"]["ok"])
    def test_unicode_and_escaped_display_snapshots_fit_the_encrypted_frame(self):
        for character in ("😀", "\u0000"):
            with self.subTest(character=repr(character)):
                host, mac, phone = pair()
                mac.publish({"event":"chats", "chats":[{
                    "id":f"{i:032x}", "title":character*120, "status":character*80,
                    "modelLabel":character*120, "providerLabel":character*60} for i in range(256)]})
                mac.publish({"event":"snapshot", "chat":CHAT, "entries":[{
                    "id":f"a{i}", "kind":"assistant", "text":character*8000,
                    "detail":character*2000} for i in range(30)]})
                mac.publish({"event":"delta", "chat":CHAT, "id":"a29", "offset":0, "text":character*8000})
                mac.publish({"event":"approval", "chat":CHAT, "approval":{
                    "id":APPROVAL, "summary":character*500, "detail":character*2000}})
                for frame in mac.take():
                    self.assertLess(len(frame["ct"]), 200000)
                    self.assertLess(len(encode_line(frame)), 262144)
                    phone.feed(frame)
                phone._send("session.resume", {"afterCursor":-1})
                for frame in phone.take(): mac.feed(frame)
                for frame in mac.take():
                    self.assertLess(len(frame["ct"]), 200000)
                    self.assertLess(len(encode_line(frame)), 262144)
                    phone.feed(frame)
                self.assertEqual(phone.selected["live"], character*8000)
                self.assertTrue(0 < len(phone.chats) < 256)
                self.assertEqual(phone.chats[0]["id"], "0"*32)

    def test_pair_snapshot_send_stop_select_and_exact_approval(self):
        host, mac, phone = pair()
        self.assertTrue(mac.established and phone.established)
        self.assertEqual(mac.confirm, phone.confirm)
        self.assertEqual(len(mac.confirm), 6)
        mac.publish({"event": "chats", "chats": [
            {"id": CHAT, "title": "Notes", "status": "Ready", "busy": False, "pendingApproval": False,
             "account": "secret-account", "route": "openai"},
        ]})
        mac.publish({"event": "catalogue", "models": [{"account": "sk-secret"}]})
        mac.publish({"event": "approval", "chat": CHAT, "approval": {
            "id": APPROVAL, "summary": "Run tests", "detail": "pytest", "workspace": "/tmp/work",
        }})
        exchange(mac, phone)
        self.assertEqual(phone.chats[0]["title"], "Notes")
        self.assertNotIn("account", phone.chats[0])
        self.assertEqual(phone.approval["id"], APPROVAL)
        self.assertNotIn("workspace", phone.approval)
        phone.send_text(CHAT, "hello from the phone", "req-send")
        phone.stop(CHAT, "req-stop")
        phone.select(CHAT, "req-select")
        phone.approve(CHAT, APPROVAL, True, "req-approve")
        exchange(mac, phone)
        self.assertEqual([item["command"] for item in host.commands], ["send", "stop", "subscribe", "approve"])
        self.assertEqual(host.commands[0]["text"], "hello from the phone")
        self.assertEqual(host.commands[0]["request"], "req-send")
        self.assertIs(host.commands[3]["allow"], True)
        self.assertEqual(host.commands[3]["id"], APPROVAL)
        self.assertTrue(phone.acks["req-approve"]["ok"])

    def test_replayed_request_and_approval_do_not_execute_twice(self):
        host, mac, phone = pair()
        phone.send_text(CHAT, "once", "same-request")
        exchange(mac, phone)
        phone.send_text(CHAT, "once", "same-request")
        exchange(mac, phone)
        self.assertEqual(len(host.commands), 1)
        mac.publish({"event": "approval", "chat": CHAT, "approval": {"id": APPROVAL, "summary": "tool"}})
        exchange(mac, phone)
        phone.approve(CHAT, APPROVAL, False, "approve-1")
        exchange(mac, phone)
        phone.approve(CHAT, APPROVAL, True, "approve-2")
        exchange(mac, phone)
        approvals = [item for item in host.commands if item["command"] == "approve"]
        self.assertEqual(len(approvals), 1)
        self.assertIs(approvals[0]["allow"], False)
        self.assertIs(phone.acks["approve-2"]["allow"], False)

    def test_wrong_approval_string_allow_and_unknown_method_do_not_execute(self):
        host, mac, phone = pair()
        mac.publish({"event": "approval", "chat": CHAT, "approval": {"id": APPROVAL, "summary": "tool"}})
        exchange(mac, phone)
        phone.approve(CHAT, "ff" * 16, True, "wrong-id")
        phone._send("chat.approve", {"chat": CHAT, "approvalId": APPROVAL, "allow": "true", "requestId": "bad-bool"})
        phone._send("chat.configure", {"chat": CHAT, "requestId": "nope", "approvalMode": "yolo"})
        phone._send("chat.shutdown", {"requestId": "stop-everything"})
        exchange(mac, phone)
        self.assertEqual(host.commands, [])
        self.assertFalse(phone.acks["wrong-id"]["ok"])
        self.assertFalse(phone.acks["nope"]["ok"])

    def test_reconnect_dedups_and_replays_contiguous_deltas(self):
        host, mac, phone = pair()
        mac.publish({"event": "notice", "message": "one"})
        mac.publish({"event": "notice", "message": "two"})
        exchange(mac, phone)
        self.assertEqual(phone.cursor, 2)
        self.assertEqual(phone.notices, ["one", "two"])
        phone.send_text(CHAT, "kept", "durable-request")
        exchange(mac, phone)
        phone.start()
        exchange(mac, phone)
        self.assertTrue(mac.established)
        phone.send_text(CHAT, "kept", "durable-request")
        mac.publish({"event": "notice", "message": "three"})
        exchange(mac, phone)
        self.assertEqual(sum(item["command"] == "send" for item in host.commands), 1)
        self.assertEqual(phone.notices, ["one", "two", "three"])
        self.assertFalse(phone.needs_snapshot)

    def test_gap_requests_snapshot_instead_of_applying_a_hole(self):
        host, mac, phone = pair(session_id="gap-1")
        mac.retain = 2
        for name in ("a", "b", "c"):
            mac.publish({"event": "notice", "message": name})
        mac.take()
        phone.cursor = 0
        phone.resume()
        exchange(mac, phone)
        self.assertEqual(phone.cursor, mac.cursor)
        self.assertEqual(phone.notices, [])
        self.assertFalse(phone.needs_snapshot)

    def test_revoked_device_cannot_reconnect_or_command(self):
        host, mac, phone = pair()
        identity = phone.identity_b64()
        mac.revoke(identity)
        phone.send_text(CHAT, "after revoke", "revoked-send")
        with self.assertRaises(RemoteError):
            exchange(mac, phone)
        self.assertEqual(host.commands, [])
        self.assertFalse(mac.established)
        phone.start()
        with self.assertRaises(RemoteError):
            exchange(mac, phone)
        self.assertFalse(mac.registry.trusted(identity))
        self.assertEqual(host.commands, [])

    def test_rejected_pairing_does_not_trust(self):
        host = FakeHost()
        mac = MacBridge(host.handle, "nope", auto_accept=False)
        phone = PhoneClient("nope")
        phone.start()
        with self.assertRaises(RemoteError):
            exchange(mac, phone)
        self.assertFalse(mac.established)
        self.assertFalse(phone.established)
        self.assertEqual(host.commands, [])

    def test_tampered_command_never_reaches_the_host(self):
        host, mac, phone = pair()
        phone.send_text(CHAT, "legit", "tamper-1")
        frame = phone.take()[0]
        raw = bytearray(b64d(frame["ct"]))
        raw[0] ^= 1
        frame["ct"] = b64e(bytes(raw))
        with self.assertRaises(RemoteError):
            mac.feed(frame)
        self.assertEqual(host.commands, [])
        self.assertFalse(mac.established)

    def test_replayed_transport_seq_is_dropped(self):
        host, mac, phone = pair()
        phone.send_text(CHAT, "legit", "seq-1")
        frame = phone.take()[0]
        mac.feed(frame)
        mac.feed(dict(frame))
        self.assertEqual(len(host.commands), 1)

    def test_protocol_constant_is_the_audited_one(self):
        self.assertEqual(E2EE_PROTOCOL, "taskwraith-e2ee-v1")


class LoopbackTests(unittest.TestCase):
    def test_public_binds_are_refused_before_a_socket_exists(self):
        for host in ("0.0.0.0", "", "10.0.0.8", "192.168.1.20"):
            with self.assertRaises(ValueError):
                validate_bind_host(host)
            adapter = LoopbackAdapter()
            with self.assertRaises(ValueError):
                adapter.start(host, 9)
            self.assertIsNone(adapter._server)

    def test_loopback_carries_one_frame_and_stops(self):
        adapter = LoopbackAdapter()
        received = []

        def serve():
            try:
                conn = adapter.accept(2)
            except Exception as exc:
                received.append(exc)
                return
            try:
                received.append(conn.recv(1024))
            finally:
                conn.close()

        adapter.start("127.0.0.1", 0)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            sock = socket.create_connection(("127.0.0.1", adapter.port), timeout=2)
            try:
                sock.sendall(b'{"t":"clientHello"}\n')
            finally:
                sock.close()
            thread.join(2)
            self.assertEqual(received, [b'{"t":"clientHello"}\n'])
        finally:
            adapter.stop()


if __name__ == "__main__":
    unittest.main()
