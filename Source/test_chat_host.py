"""The persistent Chat Host as real processes: a UI detach is not a stop.

Each test runs chat_host.py as its own process against a scripted loopback
Messages endpoint, with real sockets, real tools and real background
processes. Waits are bounded; gated replies are the barriers.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import queue
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import chat_host
import gateway_service
from bridge_core import atomic_json, default_settings, gateway_token
from chat_capabilities import SURFACE_LOST, SURFACE_UNAVAILABLE, NativeActions
from test_chat_runtime import model

SOURCE = Path(__file__).resolve().parent
QUESTION = {"questions": [{"id": "colour", "question": "Which colour?"}]}


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def wait_for(predicate, timeout=15, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value: return value
        time.sleep(.05)
    raise AssertionError("Timed out waiting for " + message)


def alive(pid):
    """True for a running process; a zombie awaiting its reaper is gone."""
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


class ScriptedGateway:
    """A loopback Messages endpoint whose replies the test scripts and gates.

A reply is {"text": [chunks], "gate": Event, "tool": (name, input)}. A gated
reply sends its first chunk and then waits, so a run is provably in flight.
"""
    def __init__(self, root):
        self.root, self.token = root, gateway_token(root)
        self.replies = queue.Queue()
        self.requests = []
        self.gates = []
        self.condition = threading.Condition()
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def authorized(self):
                if self.headers.get("Authorization") != "Bearer " + gateway.token:
                    self.send_response(401); self.end_headers(); return False
                return True

            def do_GET(self):
                if not self.authorized(): return
                body = json.dumps({"models": [model()]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

            def do_POST(self):
                if not self.authorized(): return
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with gateway.condition:
                    gateway.requests.append(payload); gateway.condition.notify_all()
                try: reply = gateway.replies.get(timeout=30)
                except queue.Empty: reply = {"text": ["No scripted reply."]}
                self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                try: gateway.stream(self.wfile, reply)
                except (BrokenPipeError, ConnectionResetError): pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()
        settings = default_settings()
        settings["port"] = self.server.server_address[1]
        atomic_json(Path(root) / "settings.json", settings)

    def reply(self, *text, gate=None, tool=None):
        if gate is not None: self.gates.append(gate)
        self.replies.put({"text": list(text), "gate": gate, "tool": tool})

    def wait_requests(self, count, timeout=20):
        with self.condition:
            if not self.condition.wait_for(lambda: len(self.requests) >= count, timeout):
                raise AssertionError(f"Expected {count} model requests, saw {len(self.requests)}")

    @staticmethod
    def stream(output, reply):
        def send(event):
            output.write(b"data: " + json.dumps(event).encode() + b"\n\n"); output.flush()
        send({"type": "message_start", "message": {"usage": {"input_tokens": 10}}})
        index = 0
        if reply["text"]:
            send({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            for position, chunk in enumerate(reply["text"]):
                send({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": chunk}})
                if position == 0 and reply["gate"] is not None and not reply["gate"].wait(60):
                    raise BrokenPipeError("The test never released this reply")
            send({"type": "content_block_stop", "index": 0})
            index = 1
        if reply["tool"]:
            name, arguments = reply["tool"]
            send({"type": "content_block_start", "index": index,
                  "content_block": {"type": "tool_use", "id": "call-" + name + str(time.monotonic_ns()), "name": name, "input": {}}})
            send({"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": json.dumps(arguments)}})
            send({"type": "content_block_stop", "index": index})
        send({"type": "message_delta", "delta": {"stop_reason": "tool_use" if reply["tool"] else "end_turn"}, "usage": {"output_tokens": 3}})
        send({"type": "message_stop"})

    def close(self):
        for gate in self.gates: gate.set()
        self.server.shutdown(); self.server.server_close()


class HostProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.root, self.workspace = base / "state", base / "work"
        self.root.mkdir(mode=0o700); self.workspace.mkdir()
        self.log = open(base / "host.log", "wb")
        self.addCleanup(self.log.close)
        self.hosts, self.sessions, self.clients = [], [], []
        def quiet():
            self.log.flush()
            text = (base / "host.log").read_text(errors="replace")
            self.assertNotIn("Traceback", text, text[-4000:])
        self.addCleanup(quiet)  # Runs after every process below has ended.
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for session in self.sessions: session.close()
        for client in self.clients:
            if client.poll() is None: client.kill()
            client.wait(5)
        for host in self.hosts:
            if host.poll() is None:
                try:
                    control = chat_host.connect(self.root, role="control")
                    control.send({"command": "shutdown"}); control.read(15); control.close()
                except (OSError, ValueError):
                    pass
                try: host.wait(20)
                except subprocess.TimeoutExpired:
                    host.kill(); host.wait(5)
        if getattr(self, "gateway", None): self.gateway.close()

    def start(self, *extra, ensure_gateway=False, expect_ready=True):
        environment = {"HOME": str(self.workspace)}
        announcement, process = chat_host.launch(self.root, ensure_gateway=ensure_gateway, stderr=self.log,
                                                 extra=extra, env=environment)
        self.hosts.append(process)
        if expect_ready:
            self.assertTrue(announcement.get("ready"), announcement)
            self.assertEqual(announcement["protocol"], chat_host.PROTOCOL)
        return announcement, process

    def attach(self, **options):
        session = chat_host.connect(self.root, client="test", **options)
        self.sessions.append(session)
        events = []
        session.until(lambda event: events.append(event) or event.get("event") == "ready", timeout=20)
        return session, events

    def control(self, command="host_status", **fields):
        session = chat_host.connect(self.root, role="control", client="test control")
        try:
            session.send({"command": command, **fields})
            return session.until(lambda event: event.get("event") in {"host_status", "host_error", "gateway", "host_detached"})
        finally:
            session.close()

    def detached(self, timeout=15):
        """Host status once no window is attached (the asking control is listed too)."""
        def ready():
            status = self.control()
            return status if not [row for row in status["attachments"] if row["role"] == "desktop"] else None
        return wait_for(ready, timeout, "the window to detach")

    def chat(self, events):
        selected = [event for event in events if event.get("event") == "selected"]
        self.assertTrue(selected and selected[-1]["id"], events)
        return selected[-1]

    def crash_client(self, command):
        """A separate UI process that sends one command and is then killed."""
        code = ("import json, sys, chat_host\n"
                "s = chat_host.connect(sys.argv[1], client='crashing window')\n"
                "s.until(lambda e: e.get('event') == 'ready', timeout=20)\n"
                "s.send(json.loads(sys.argv[2])); print('sent', flush=True)\n"
                "while s.read() is not None: pass\n")
        client = subprocess.Popen([sys.executable, "-c", code, str(self.root), json.dumps(command)], cwd=SOURCE,
                                  stdout=subprocess.PIPE, stderr=self.log)
        self.clients.append(client)
        self.assertEqual(client.stdout.readline().strip(), b"sent")
        return client

    def heartbeat(self, path):
        def grew():
            before = path.stat().st_size if path.exists() else 0
            time.sleep(.4)
            return path.exists() and path.stat().st_size > before
        return grew

    def test_ui_crash_keeps_run_and_background_process_until_explicit_shutdown(self):
        self.gateway = ScriptedGateway(self.root)
        _, host = self.start()
        session, events = self.attach()
        chat = self.chat(events)["id"]
        session.send({"command": "configure", "chat": chat, "id": chat, "approvalMode": "yolo"})
        session.until(lambda event: event.get("event") == "chats" and
                      any(row["id"] == chat and row["approvalMode"] == "yolo" for row in event["chats"]))
        session.close()

        beat = self.workspace / "beat"
        gate = threading.Event()
        command = "for i in $(seq 1 600); do echo $i >> beat; sleep 0.1; done"
        self.gateway.reply(tool=("run_shell", {"command": command, "background": True}))
        self.gateway.reply("Still working ", "after the window closed.", gate=gate)
        client = self.crash_client({"command": "send", "chat": chat, "id": chat, "text": "Start the watcher", "request": "r1"})
        self.gateway.wait_requests(2)
        client.send_signal(signal.SIGKILL); client.wait(5)

        status = self.detached()
        self.assertEqual(status["activeChats"], [chat])
        self.assertEqual(status["processes"], 1)
        self.assertTrue(self.heartbeat(beat)(), "the background process stopped with the window")
        self.assertIsNone(host.poll())

        gate.set()
        wait_for(lambda: not self.control()["activeChats"], message="the detached run to finish")
        session, events = self.attach()
        selected = self.chat(events)
        self.assertEqual(selected["id"], chat)
        self.assertFalse(selected["busy"])
        texts = [entry["text"] for entry in selected["entries"] if entry["kind"] == "assistant"]
        self.assertIn("Still working after the window closed.", texts)
        session.send({"command": "processes", "chat": chat})
        rows = session.until(lambda event: event.get("event") == "processes" and event.get("chat") == chat)["processes"]
        self.assertEqual([row["status"] for row in rows], ["running"])
        self.assertTrue(self.heartbeat(beat)())

        # Detach is not a stop; only an explicit shutdown ends the work.
        session.send({"command": "detach"})
        self.assertEqual(session.until(lambda event: event.get("event") == "host_detached")["reason"], "detached")
        self.assertIsNone(host.poll())
        self.assertTrue(self.heartbeat(beat)())
        self.assertEqual(self.control("shutdown")["reason"], "shutdown")
        self.assertEqual(host.wait(20), 0)
        size = beat.stat().st_size
        time.sleep(.4)
        self.assertEqual(beat.stat().st_size, size, "shutdown left the background process running")
        paths = chat_host.HostPaths(self.root)
        self.assertFalse(paths.socket.exists())
        self.assertFalse(paths.credential.exists())
        self.assertEqual(self.gateway.replies.qsize(), 0)
        self.assertEqual(len(self.gateway.requests), 2, "a model request was replayed")

    def test_pending_question_and_approval_survive_detach_and_are_never_answered_for_the_user(self):
        self.gateway = ScriptedGateway(self.root)
        self.start()
        session, events = self.attach()
        chat = self.chat(events)["id"]
        self.gateway.reply(tool=("request_user_input", QUESTION))
        session.send({"command": "send", "chat": chat, "id": chat, "text": "Ask me", "request": "r1"})
        card = session.until(lambda event: event.get("event") == "entry" and event["entry"].get("question"))["entry"]
        session.close()

        status = self.detached()
        self.assertEqual((status["activeChats"], status["pendingQuestions"]), ([chat], 1))
        session, events = self.attach()
        entry = next(item for item in self.chat(events)["entries"] if item["id"] == card["id"])
        self.assertEqual(entry["question"]["status"], "pending")
        target = self.workspace / "approved.txt"
        self.gateway.reply(tool=("run_shell", {"command": "echo approved > approved.txt"}))
        self.gateway.reply("All done.")
        session.send({"command": "answer_question", "chat": chat, "question_id": entry["question"]["id"],
                      "answers": {"colour": ["Blue"]}})
        approval = session.until(lambda event: event.get("event") == "approval")["approval"]
        session.close()

        status = self.detached()
        self.assertEqual(status["pendingApprovals"], 1)
        time.sleep(.3)
        self.assertFalse(target.exists(), "a detach approved the action")
        session, events = self.attach()
        selected = self.chat(events)
        self.assertTrue(selected["busy"])
        self.assertEqual(selected["approval"]["id"], approval["id"])
        session.send({"command": "approve", "chat": chat, "id": approval["id"], "allow": True})
        session.until(lambda event: event.get("event") == "state" and event.get("chat") == chat and not event["busy"], timeout=20)
        self.assertEqual(target.read_text().strip(), "approved")
        self.assertEqual(len(self.gateway.requests), 3)

    def test_slow_reader_is_detached_at_its_bound_while_the_run_completes(self):
        self.gateway = ScriptedGateway(self.root)
        self.start("--client-queue-frames", "256")
        session, events = self.attach()
        chat = self.chat(events)["id"]
        chunks = ["%05d" % index + "x" * 495 for index in range(2_000)]
        self.gateway.reply(*chunks)
        session.send({"command": "send", "chat": chat, "id": chat, "text": "Flood", "request": "r1"})
        # This window never reads again.
        status = wait_for(lambda: (lambda s: s if not s["activeChats"] else None)(self.control()), timeout=60,
                          message="the flooded run to finish")
        self.assertEqual(status["overflows"], 1)
        self.assertEqual([row["role"] for row in status["attachments"]], ["control"])
        drained = 0
        session.connection.settimeout(10)
        while chunk := session.connection.recv(1 << 20): drained += len(chunk)
        # Every delta frame would be over 1.1 MB; the bound stopped far short.
        self.assertLess(drained, 700_000, "the Host delivered without a bound")
        session, events = self.attach()
        texts = [entry["text"] for entry in self.chat(events)["entries"] if entry["kind"] == "assistant"]
        self.assertEqual(texts[-1], "".join(chunks))

    def test_duplicate_launches_share_one_host_and_wrong_clients_change_nothing(self):
        self.gateway = ScriptedGateway(self.root)
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.start(expect_ready=False))) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(60)
        ready = [announcement for announcement, _ in results if announcement.get("ready")]
        running = [(announcement, process) for announcement, process in results if announcement.get("reason") == "running"]
        self.assertEqual((len(ready), len(running)), (1, 1), results)
        self.assertEqual(running[0][1].wait(10), chat_host.EXIT_RUNNING)
        generation = ready[0]["generation"]

        paths = chat_host.HostPaths(self.root)
        self.assertEqual(stat.S_IMODE(paths.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(paths.socket).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(paths.credential.stat().st_mode), 0o600)

        # A silent client does not hold up anyone else's handshake.
        silent = socket.socket(socket.AF_UNIX); silent.connect(str(paths.socket))
        self.addCleanup(silent.close)
        first, _ = self.attach()
        self.assertEqual(first.host["generation"], generation)
        for options, reason in (({"credential": "0" * 64}, "credential"), ({"protocol": 99}, "protocol")):
            with self.assertRaises(chat_host.HostRefused) as refused:
                chat_host.connect(self.root, client="wrong", **options)
            self.assertEqual(refused.exception.reply["reason"], reason)
            self.assertEqual(refused.exception.reply["protocol"], chat_host.PROTOCOL)
        # One byte over the hello limit: the Host must read every byte before
        # it can refuse, so the client's send cannot race the refusal's close.
        for payload in (b"not json\n", b"{" + b"x" * chat_host.HELLO_LIMIT):
            raw = socket.socket(socket.AF_UNIX); raw.connect(str(paths.socket)); raw.sendall(payload)
            reply = chat_host.HostConnection(raw).read(10)
            raw.close()
            self.assertEqual((reply["event"], reply["reason"]), ("host_rejected", "invalid_hello"))
        # An older or newer app may still read status and retire this Host.
        control = chat_host.connect(self.root, role="control", protocol=99)
        control.send({"command": "send", "chat": "x", "text": "no"})
        self.assertEqual(control.read(10)["event"], "host_error")
        control.send({"command": "host_status"})
        status = control.read(10)
        control.close()
        self.assertEqual(status["generation"], generation)
        self.assertEqual({row["id"]: row["role"] for row in status["attachments"]},
                         {first.host["attachment"]: "desktop", control.host["attachment"]: "control"})

        # The newest window supersedes the older one, which is told why.
        second, _ = self.attach()
        detached = first.until(lambda event: event.get("event") == "host_detached")
        self.assertEqual((detached["reason"], detached["by"]), ("superseded", second.host["attachment"]))
        self.assertEqual(second.host["superseded"], first.host["attachment"])

        # A stdio worker cannot open the same chats while the Host owns them.
        environment = {**os.environ, "MISTRAL_BRIDGE_STATE_DIR": str(self.root)}
        worker = subprocess.run([sys.executable, str(SOURCE / "chat_runtime.py")], input=b"", capture_output=True,
                                env=environment, timeout=30)
        self.assertIn("already open in another Provider Hub instance", worker.stdout.decode())

    def test_stale_socket_is_replaced_but_unexpected_files_and_symlinks_are_refused(self):
        self.gateway = ScriptedGateway(self.root)
        paths = chat_host.HostPaths(self.root)
        paths.prepare()
        stale = socket.socket(socket.AF_UNIX); stale.bind(str(paths.socket)); stale.close()
        self.start()
        self.assertEqual(self.control("shutdown")["reason"], "shutdown")
        self.assertEqual(self.hosts[-1].wait(20), 0)

        paths.socket.write_text("not a socket")
        announcement, process = self.start(expect_ready=False)
        self.assertEqual((announcement["ready"], process.wait(20)), (False, 1))
        self.assertIn("unexpected file", announcement["message"])
        self.assertEqual(paths.socket.read_text(), "not a socket")

        shutil.rmtree(paths.directory)
        elsewhere = self.root / "elsewhere"; elsewhere.mkdir(mode=0o700)
        paths.directory.symlink_to(elsewhere)
        announcement, process = self.start(expect_ready=False)
        self.assertEqual((announcement["ready"], process.wait(20)), (False, 1))
        self.assertIn("symbolic link", announcement["message"])
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_gateway_outlives_chat_host_crash_restart_and_shutdown(self):
        settings = default_settings()
        settings["port"] = free_port()
        atomic_json(self.root / "settings.json", settings)
        self.addCleanup(lambda: gateway_service.held(self.root) and gateway_service.stop(self.root))
        _, host = self.start(ensure_gateway=True)
        gateway = self.control()["gateway"]
        self.assertEqual((gateway["state"], gateway["started"]), ("ready", True))
        session, _ = self.attach()
        session.send({"command": "gateway_status"})
        self.assertEqual(session.until(lambda event: event.get("event") == "gateway")["pid"], gateway["pid"])
        session.close()
        # Gateway lifetime is the hub's; a control connection cannot drive it.
        self.assertEqual(self.control("gateway_start")["event"], "host_error")

        # Chat crashing, restarting and shutting down never touch the gateway
        # that the hub's desktop sessions share.
        host.send_signal(signal.SIGKILL); host.wait(5)
        self.assertEqual(gateway_service.status(self.root)["pid"], gateway["pid"])
        _, host = self.start(ensure_gateway=True)
        attached = self.control()["gateway"]
        self.assertEqual((attached["pid"], attached["started"]), (gateway["pid"], False))
        self.assertEqual(self.control("shutdown")["reason"], "shutdown")
        self.assertEqual(host.wait(20), 0)
        self.assertEqual(gateway_service.status(self.root)["pid"], gateway["pid"])
        self.assertEqual(gateway_service.stop(self.root)["pid"], gateway["pid"])
        wait_for(lambda: not alive(gateway["pid"]), message="the gateway to stop on request")
        self.assertFalse(gateway_service.held(self.root))

    def test_stdio_worker_still_serves_its_parent(self):
        self.gateway = ScriptedGateway(self.root)
        self.gateway.reply("Hello over stdio.")
        environment = {**os.environ, "MISTRAL_BRIDGE_STATE_DIR": str(self.root), "HOME": str(self.workspace)}
        worker = subprocess.Popen([sys.executable, str(SOURCE / "chat_runtime.py")], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=self.log, env=environment)
        self.clients.append(worker)
        def until(predicate):
            while line := worker.stdout.readline():
                event = json.loads(line)
                if predicate(event): return event
            raise AssertionError("The stdio worker ended early")
        chat = until(lambda event: event.get("event") == "selected" and event.get("id"))["id"]
        until(lambda event: event.get("event") == "ready")
        from peer_mcp import PeerClient, admin
        endpoint = admin(self.root, {"command": "peer_enroll_chat", "chat": chat})["thread"]
        credential = chat_host.read_credential(chat_host.HostPaths(self.root))
        announcement, competing = self.start(expect_ready=False)
        self.assertEqual(announcement["reason"], "running")
        self.assertEqual(competing.wait(5), chat_host.EXIT_RUNNING)
        self.assertEqual(chat_host.read_credential(chat_host.HostPaths(self.root)), credential)
        foreign = admin(self.root, {"command": "peer_enroll", "app": "codex_desktop", "native_thread_id": "stdio-peer",
                                   "workspace": str(self.workspace)})
        peer = PeerClient(self.root, {"thread_id": foreign["thread"]["thread_id"], "token": foreign["token"]}, heartbeat=False)
        try:
            sent = peer.call("hub_send_message", {"to": endpoint["thread_id"], "body": "Stdio worker mail", "idempotency_key": "stdio"})
            self.assertEqual(sent["message"]["status"], "queued")
            self.assertEqual(self.gateway.requests, [])
            self.assertEqual(self.control("shutdown")["event"], "host_error")
            with self.assertRaises(chat_host.HostRefused):
                chat_host.connect(self.root, role="desktop")
        finally:
            peer.close()
        worker.stdin.write(json.dumps({"command": "send", "chat": chat, "id": chat, "text": "Hi"}).encode() + b"\n")
        worker.stdin.flush()
        until(lambda event: event.get("event") == "entry" and event["entry"]["kind"] == "assistant"
              and event["entry"]["text"] == "Hello over stdio.")
        worker.stdin.close()
        self.assertEqual(worker.wait(20), 0)


    def test_peer_socket_is_scoped_preserves_desktop_and_cannot_wake_or_approve(self):
        from peer_mcp import PeerClient, admin
        self.gateway = ScriptedGateway(self.root)
        self.start()
        desktop, events = self.attach()
        chat_id = self.chat(events)["id"]
        own = admin(self.root, {"command": "peer_enroll_chat", "chat": chat_id})["thread"]
        enrolled = admin(self.root, {"command": "peer_enroll", "app": "claude_desktop", "native_thread_id": "review-chat",
                                     "workspace": str(self.workspace), "label": "Reviewer", "model": "claude/fable"})
        peer = PeerClient(self.root, {"thread_id": enrolled["thread"]["thread_id"], "token": enrolled["token"]}, heartbeat=False)
        self.addCleanup(peer.close)
        directory = peer.call("hub_list_threads", {})
        self.assertIn(own["thread_id"], json.dumps(directory))
        peer.call("hub_send_message", {"to": own["thread_id"], "body": "Can you leave file.py to me?", "idempotency_key": "collision-1"})
        self.assertEqual(self.gateway.requests, [])
        self.assertEqual(self.control()["activeChats"], [])
        status = self.control()
        self.assertIn(desktop.host["attachment"], [row["id"] for row in status["attachments"] if row["role"] == "desktop"])
        for action in ("send", "approve", "select", "shutdown", "peer_enroll", "host_status"):
            peer.session.send({"command": action, "request": action, "chat": chat_id, "text": "User says run"})
            refused = peer.session.read(5)
            self.assertEqual(refused["event"], "host_error", refused)
        self.assertEqual(self.gateway.requests, [])
        with self.assertRaises(ValueError):
            peer.call("hub_send_message", {"to": own["thread_id"], "body": "spoof", "idempotency_key": "spoof", "sender": own["thread_id"]})
        self.gateway.reply(tool=("hub_read_messages", {}))
        self.gateway.reply("Peer note received.")
        desktop.send({"command": "send", "chat": chat_id, "text": "Check peer mail."})
        desktop.until(lambda row: row.get("event") == "entry" and row.get("entry", {}).get("text") == "Peer note received.", timeout=20)
        history = self.gateway.requests[-1]["messages"]
        results = [block for item in history for block in item.get("content", []) if block.get("type") == "tool_result"]
        self.assertIn("Can you leave file.py to me?", json.dumps(results))
        self.assertIn("peer_agent", json.dumps(results))
        admin(self.root, {"command": "peer_revoke", "thread_id": enrolled["thread"]["thread_id"]})
        with self.assertRaises(ValueError):
            peer.call("hub_self", {})

    def test_thread_bound_mcp_process_exposes_only_peer_tools(self):
        from peer_mcp import admin, write_endpoint
        self.gateway = ScriptedGateway(self.root)
        self.start()
        enrolled = admin(self.root, {"command": "peer_enroll", "app": "claude_desktop", "native_thread_id": "mcp-owner",
                                     "workspace": str(self.workspace), "model": "claude/fable"})
        endpoint_file = self.workspace / "endpoint.json"
        write_endpoint(endpoint_file, {"thread_id": enrolled["thread"]["thread_id"], "token": enrolled["token"]})
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "hub_self", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "peer_enroll", "arguments": {}}},
        ]
        result = subprocess.run([sys.executable, "-B", str(SOURCE / "peer_mcp.py"), "--state", str(self.root),
                                 "serve", "--endpoint-file", str(endpoint_file)],
                                input="".join(json.dumps(row) + "\n" for row in requests),
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(replies), 4)
        self.assertNotIn(enrolled["token"], result.stdout + result.stderr)
        self.assertIn("hub_read_messages", [row["name"] for row in replies[1]["result"]["tools"]])
        identity = json.loads(replies[2]["result"]["content"][0]["text"])["thread"]
        self.assertEqual(identity["thread_id"], enrolled["thread"]["thread_id"])
        self.assertEqual(identity["identity_assurance"], "enrolled")
        self.assertTrue(replies[3]["result"]["isError"])
        self.assertEqual(self.gateway.requests, [])

    def test_peer_mail_survives_restart_and_bad_token_cannot_attach(self):
        from peer_mcp import PeerClient, admin
        self.gateway = ScriptedGateway(self.root)
        _, process = self.start()
        credentials = []
        for native_id in ("codex-task", "claude-task"):
            result = admin(self.root, {"command": "peer_enroll", "app": "test_adapter", "native_thread_id": native_id,
                                       "workspace": str(self.workspace)})
            credentials.append({"thread_id": result["thread"]["thread_id"], "token": result["token"]})
        with self.assertRaises(chat_host.HostRefused):
            PeerClient(self.root, {**credentials[0], "token": "incorrect"}, heartbeat=False)
        sender = PeerClient(self.root, credentials[0], heartbeat=False)
        sender.call("hub_send_message", {"to": credentials[1]["thread_id"], "body": "Retained after restart", "idempotency_key": "persist"})
        sender.close()
        self.control("shutdown")
        self.assertEqual(process.wait(20), 0)
        self.start()
        recipient = PeerClient(self.root, credentials[1], heartbeat=False)
        try:
            result = recipient.call("hub_read_messages", {})
            self.assertIn("Retained after restart", json.dumps(result))
            self.assertEqual(self.gateway.requests, [])
        finally:
            recipient.close()


class NativeSurfaceTests(unittest.TestCase):
    def actions(self):
        events = []
        native = NativeActions(events.append, timeout=5)
        return native, events

    def test_no_attached_window_is_a_truthful_refusal_before_dispatch(self):
        native, events = self.actions()
        native.surface = lambda: None
        with self.assertRaisesRegex(ValueError, SURFACE_UNAVAILABLE):
            native.call("chat", "open", {"url": "https://example.com"}, threading.Event())
        self.assertEqual(events, [])

    def test_a_lost_window_leaves_the_outcome_unknown_and_other_windows_cannot_answer(self):
        native, events = self.actions()
        native.surface = lambda: "window-a"
        outcome = {}
        def call():
            try: outcome["result"] = native.call("chat", "open", {}, threading.Event())
            except ValueError as exc: outcome["error"] = str(exc)
        worker = threading.Thread(target=call); worker.start()
        action = wait_for(lambda: next((event for event in events if event["event"] == "native_action"), None))
        result = {"content": [{"type": "text", "text": "ok"}], "is_error": False}
        with self.assertRaisesRegex(ValueError, "another Chat window"):
            native.respond({"id": action["id"], "chat": "chat", "result": result, "_attachment": "window-b"})
        native.surface_lost("window-a")
        worker.join(5)
        self.assertEqual(outcome, {"error": SURFACE_LOST})
        native.respond({"id": action["id"], "chat": "chat", "result": result, "_attachment": "window-a"})
        self.assertEqual([event["event"] for event in events], ["native_action", "native_cancel"])


class DepartedWindowTests(unittest.TestCase):
    def test_a_departed_window_cannot_steer_its_replacement_but_its_input_still_runs(self):
        host = chat_host.PersistentHost(tempfile.gettempdir())
        class Departed:
            id, role, closed = "departed-window", "desktop", True
        for action in ("select", "subscribe", "detach", "host_status", "gateway_status"):
            self.assertTrue(host.intercept({"command": action, "id": "chat"}, Departed()), action)
        for action in ("send", "approve", "answer_question", "stop"):
            self.assertFalse(host.intercept({"command": action, "id": "chat"}, Departed()), action)


class AttachmentBoundTests(unittest.TestCase):
    def test_queue_bound_detaches_without_blocking_the_sender(self):
        near, far = socket.socketpair()
        self.addCleanup(near.close); self.addCleanup(far.close)
        attachment = chat_host.Attachment(near, "desktop", "test", limit_bytes=200_000, limit_frames=10_000)
        attachment.start()
        data = b"x" * 9_999 + b"\n"
        started = time.monotonic()
        sent = 0
        while attachment.send(data): sent += 1
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(attachment.reason, "overflow")
        self.assertFalse(attachment.send(b"late\n"))
        attachment.join(5)
        far.settimeout(5)
        received = 0
        while chunk := far.recv(1 << 20): received += len(chunk)
        self.assertLessEqual(received, sent * len(data))


if __name__ == "__main__":
    unittest.main()
