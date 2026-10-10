"""Background shell processes: ownership, bounded output, Stop and cleanup."""
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest import mock

import chat_processes
from chat_processes import OwnerEvent, ProcessRegistry, _complete_utf8, scope
from chat_runtime import ChatStore
from chat_sessions import ChatHost
from chat_tools import ChatToolRunner, TOOL_DEFINITIONS
from test_chat_runtime import FakeTransport, response


class Conversation:
    """The few ChatService attributes that process ownership reads."""
    def __init__(self, chat_id="chat-a", role="parent", label="Model A", member=None, parent=None):
        self.chat = {"id": chat_id, "route": "ollama/a", "account": ""}
        self.models = [{"route": "ollama/a", "account": "", "label": label}]
        self.role, self.team_member, self.team_parent, self.helper_parent = role, member, None, None
        if parent is not None and member: self.team_parent = parent
        elif parent is not None: self.helper_parent = parent
        self.cancel = OwnerEvent(self)


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline: return False
        time.sleep(0.02)
    return True


def gone(pid):
    try: os.kill(pid, 0)
    except ProcessLookupError: return True
    except PermissionError: return False
    return False


NEEDS_WAITID = unittest.skipUnless(chat_processes.NONREAPING, "background processes need os.waitid with WNOWAIT")


@NEEDS_WAITID
class ProcessRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = str(Path(self.temp.name).resolve())
        self.changes = []
        self.registry = ProcessRegistry(self.changes.append)
        self.addCleanup(self.registry.shutdown)
        self.chat = Conversation()

    def start(self, command, owner=None):
        owner = owner or self.chat
        return self.registry.start(owner, self.workspace, command, dict(os.environ), owner.cancel)

    def rows(self, chat="chat-a"):
        return {row["id"]: row for row in self.registry.snapshot(chat)}

    def test_keeps_running_reports_new_output_once_and_stop_ends_its_group(self):
        text, error = self.start("echo ready; sleep 30 & echo child=$!; wait")
        self.assertFalse(error)
        self.assertIn("p1 (pid", text); self.assertIn("is running", text); self.assertIn("ready", text)
        self.assertIn("read_process", text)
        child = int(re.search(r"child=(\d+)", text).group(1))
        row = self.rows()["p1"]
        self.assertEqual((row["status"], row["owner"], row["memberID"], row["workspace"]), ("running", "Model A", None, self.workspace))
        self.assertIn("ready", row["output"])
        self.assertEqual(self.registry.snapshot("chat-a", output=False)[0]["output"], "")
        text, _ = self.registry.read(self.chat, "p1", 0, self.chat.cancel)
        self.assertIn("No new output.", text)
        text, error = self.registry.stop(self.chat, "p1", self.chat.cancel)
        self.assertIn("was stopped", text); self.assertFalse(error)
        row = self.rows()["p1"]
        self.assertEqual((row["status"], row["stoppedBy"]), ("stopped", "agent"))
        self.assertIsNotNone(row["ended"])
        self.assertTrue(wait_for(lambda: gone(child)), "a process left in the group survived Stop")
        self.assertIn("chat-a", self.changes)

    def test_quick_failure_is_an_error_with_its_exit_code(self):
        text, error = self.start("echo oops >&2; exit 3")
        self.assertTrue(error)
        self.assertIn("exited (exit code 3)", text); self.assertIn("oops", text)
        self.assertNotIn("read_process", text)
        row = self.rows()["p1"]
        self.assertEqual((row["status"], row["code"], row["stoppedBy"]), ("exited", 3, None))

    def test_wait_returns_when_the_process_finishes_and_cancel_ends_a_wait(self):
        self.start("sleep 1.3; echo finished")
        started = time.monotonic()
        text, error = self.registry.read(self.chat, "p1", 10, self.chat.cancel)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("exited (exit code 0)", text); self.assertIn("finished", text); self.assertFalse(error)
        self.start("sleep 30")
        self.chat.cancel.set()
        started = time.monotonic()
        text, _ = self.registry.read(self.chat, "p2", 20, self.chat.cancel)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn("is running", text)

    def test_term_is_escalated_to_kill_after_the_grace_period(self):
        with mock.patch.object(chat_processes, "STOP_GRACE", 0.3):
            self.start("trap '' TERM; while :; do sleep 0.1; done")
            text, _ = self.registry.stop(self.chat, "p1", self.chat.cancel)
        self.assertIn("was stopped", text)
        self.assertEqual(self.rows()["p1"]["status"], "stopped")

    def test_output_is_bounded_for_the_model_and_the_inspector(self):
        text, _ = self.start("head -c 200000 /dev/zero | tr '\\0' x; echo; echo END")
        self.assertIn("earlier bytes of output were not kept", text)
        self.assertIn("[earlier new output truncated]", text)
        self.assertIn("END", text)
        self.assertLess(len(text), chat_processes.REPORT_CHARS + 1000)
        row = self.rows()["p1"]
        self.assertTrue(row["truncated"])
        self.assertLessEqual(len(row["output"]), chat_processes.SNAPSHOT_CHARS)
        self.assertEqual(_complete_utf8(b"ab\xe2\x82"), 2)
        self.assertEqual(_complete_utf8("é".encode()), 2)
        self.assertEqual(_complete_utf8("a😀".encode()), 5)

    def test_owners_limits_and_ids_stay_within_their_chat(self):
        parent = Conversation()
        member = Conversation("member-1", role="team", member={"id": "m1", "name": "Sol"}, parent=parent)
        helper = Conversation("helper-1", role="delegate", label="Opus", parent=parent)
        other = Conversation("chat-b")
        self.assertEqual(scope(member), ("chat-a", {"owner": "Sol", "route": "ollama/a", "account": "", "memberID": "m1"}))
        self.assertEqual(scope(helper), ("chat-a", {"owner": "Helper · Opus", "route": "ollama/a", "account": "", "memberID": None}))
        with mock.patch.object(chat_processes, "FIRST_LOOK", 0.05), mock.patch.object(chat_processes, "MAX_RUNNING", 5):
            for owner in (parent, member, helper, member):
                self.start("sleep 30", owner)
            rows = self.rows()
            self.assertEqual(sorted(rows), ["p1", "p2", "p3", "p4"])
            self.assertEqual([rows[key]["owner"] for key in ("p1", "p2", "p3")], ["Model A", "Sol", "Helper · Opus"])
            with self.assertRaisesRegex(ValueError, "already has 4"):
                self.start("sleep 30")
            self.start("sleep 30", other)
            self.assertEqual(list(self.rows("chat-b")), ["p1"])
            with self.assertRaisesRegex(ValueError, "5 background processes are already running"):
                self.start("sleep 30", other)
        with self.assertRaisesRegex(ValueError, "No background process 'p2'"):
            self.registry.stop(other, "p2", other.cancel)
        self.assertIsNone(self.registry.stop_by_user("chat-a", "p2"))
        self.assertTrue(wait_for(lambda: self.rows()["p2"]["status"] == "stopped"))
        self.assertEqual(self.rows()["p2"]["stoppedBy"], "user")
        self.assertEqual(self.registry.stop_by_user("chat-a", "p2"), "That process has already finished.")
        self.assertEqual(self.rows("chat-b")["p1"]["status"], "running")

    def test_clear_forget_and_shutdown(self):
        self.start("exit 0")
        self.start("sleep 30")
        self.registry.clear("chat-a")
        self.assertEqual([row["id"] for row in self.registry.snapshot("chat-a")], ["p2"])
        pid = self.rows()["p2"]["pid"]
        self.registry.forget("chat-a")
        self.assertEqual(self.registry.snapshot("chat-a"), [])
        self.assertTrue(wait_for(lambda: gone(pid)))
        self.start("sleep 30", Conversation("chat-b"))
        pid = self.rows("chat-b")["p1"]["pid"]
        self.registry.shutdown(timeout=1)
        self.assertTrue(wait_for(lambda: gone(pid)))
        with self.assertRaisesRegex(ValueError, "closing"):
            self.start("true")

    def process(self, identifier, chat="chat-a"):
        return next(item for item in self.registry._chats[chat]["processes"] if item.id == identifier)

    def test_group_is_never_signalled_after_its_leader_is_reaped(self):
        real, calls = os.killpg, []
        def record(pgid, number):
            for record in self.registry._chats.values():
                for item in record["processes"]:
                    if item.pid == pgid: calls.append((number, item.popen.returncode))
            return real(pgid, number)
        with mock.patch.object(chat_processes.os, "killpg", side_effect=record):
            self.start("sleep 0.2")
            self.assertTrue(wait_for(lambda: self.rows()["p1"]["status"] == "exited"))
            process = self.process("p1")
            self.assertTrue(process.reaped)
            # The group kill after exit happens while the leader is still a zombie.
            self.assertEqual(calls, [(9, None)])
            calls.clear()
            process.signal(15)
            self.assertEqual(calls, [])

    def test_exit_is_detected_under_the_lock_and_ends_what_the_shell_left(self):
        real, owned = chat_processes._leader_exited, []
        def detect(popen):
            owned.append(self.registry._condition._is_owned())
            return real(popen)
        with mock.patch.object(chat_processes, "_leader_exited", side_effect=detect):
            text, error = self.start("sleep 30 & echo child=$!; exit 0")
        child = int(re.search(r"child=(\d+)", text).group(1))
        self.assertFalse(error)
        self.assertTrue(owned and all(owned), "exit detection ran outside the registry lock")
        self.assertTrue(wait_for(lambda: gone(child)), "a child the shell left behind survived its exit")
        self.assertEqual(self.rows()["p1"]["status"], "exited")

    def test_signal_deaths_are_reported_as_signals(self):
        text, error = self.start("kill -9 $$")
        self.assertTrue(error)
        self.assertIn("exited (signal 9)", text)
        self.assertEqual(self.rows()["p1"]["code"], -9)

    def test_finished_rows_are_pruned_as_processes_finish_together(self):
        with mock.patch.object(chat_processes, "MAX_FINISHED_PER_CHAT", 2), mock.patch.object(chat_processes, "FIRST_LOOK", 0):
            for _ in range(4):
                self.start("sleep 0.3")
            self.assertTrue(wait_for(lambda: not any(row["status"] == "running" for row in self.registry.snapshot("chat-a"))))
            self.assertTrue(wait_for(lambda: len(self.registry.snapshot("chat-a")) == 2))
            for _ in range(3):
                self.start("exit 0")
            self.assertTrue(wait_for(lambda: all(row["status"] == "exited" for row in self.registry.snapshot("chat-a"))))
            self.assertEqual(len(self.registry.snapshot("chat-a")), 2)


class BackgroundToolTests(unittest.TestCase):
    def test_launch_is_refused_without_non_reaping_exit_detection(self):
        registry = ProcessRegistry()
        self.addCleanup(registry.shutdown)
        owner = Conversation()
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(chat_processes, "NONREAPING", False), \
                mock.patch.object(chat_processes.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(ValueError, "os.waitid"):
                registry.start(owner, folder, "sleep 30", dict(os.environ), owner.cancel)
        launch.assert_not_called()
        self.assertEqual(registry.snapshot("chat-a"), [])

    def test_contract_validation_and_approval(self):
        self.assertTrue({"read_process", "stop_process"} <= {tool["name"] for tool in TOOL_DEFINITIONS})
        with tempfile.TemporaryDirectory() as folder:
            runner = ChatToolRunner(folder)
            result = runner.execute("run_shell", {"command": "true", "background": True})
            self.assertTrue(result["is_error"]); self.assertIn("not available", result["content"][0]["text"])
            result = runner.execute("run_shell", {"command": "true", "background": "yes"})
            self.assertTrue(result["is_error"]); self.assertIn("true or false", result["content"][0]["text"])
            for args in ({"id": "p1", "wait": 31}, {"id": "p1", "extra": 1}, {"id": ""}):
                self.assertTrue(runner.execute("read_process", args)["is_error"])
            described = runner.describe("run_shell", {"command": "npm run dev", "background": True})
            self.assertTrue(described["requires_approval"]); self.assertIn("in background", described["summary"])
            self.assertFalse(runner.describe("read_process", {"id": "p1"})["requires_approval"])
            self.assertFalse(runner.describe("stop_process", {"id": "p1"})["requires_approval"])
            self.assertIn("Exit code: 0", runner.execute("run_shell", {"command": "true", "background": False})["content"][0]["text"])


@NEEDS_WAITID
class HostProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.events = []

        def client():
            transport = FakeTransport([])
            transport.root = self.root
            return transport
        self.host = ChatHost(ChatStore(self.root, lock=False), FakeTransport([]), self.events.append, child_factory=client)
        self.host.models = FakeTransport([]).catalogue()
        self.addCleanup(self.host.shutdown)
        workspace = self.root / "project"; workspace.mkdir()
        self.host.handle({"command": "create", "choice": self.host.models[0]["id"], "workspace": str(workspace)})
        self.chat = self.host.view_id

    def published(self):
        return [event for event in self.events if event["event"] == "processes" and event["chat"] == self.chat]

    def command(self, command):
        command = {"chat": self.chat, "id": self.chat, **command}
        try: self.host.handle(command)
        except Exception as exc: self.host.reject(command, exc)

    def test_agent_process_is_published_stopped_by_the_user_and_ended_with_its_chat(self):
        service = self.host.session(self.chat)
        service.chat["approvalMode"] = "yolo"
        calls = [{"type": "tool_use", "id": f"call-{n}", "name": "run_shell", "input": {"command": "echo serving; sleep 30", "background": True}}
                 for n in (1, 2)]
        service.transport.client.responses = [{"role": "assistant", "content": calls, "stop_reason": "tool_use", "usage": {"input_tokens": 12}},
                                              response("Both servers are up.")]
        self.host.handle({"command": "send", "chat": self.chat, "id": self.chat, "request": "r1", "text": "Start the servers"})
        self.assertTrue(wait_for(lambda: not service.busy, 10))
        requests = service.transport.client.requests
        self.assertTrue({"read_process", "stop_process"} <= {tool["name"] for tool in requests[0]["tools"]})
        tools = [item for item in service.chat["entries"] if item["kind"] == "tool"]
        self.assertEqual([item["isError"] for item in tools], [False, False])
        self.assertIn("Started background process p1", tools[0]["detail"])
        self.assertIn("serving", tools[0]["detail"])
        rows = {row["id"]: row for row in self.published()[-1]["processes"]}
        self.assertEqual({key: rows[key]["status"] for key in rows}, {"p1": "running", "p2": "running"})
        self.assertEqual(rows["p1"]["owner"], "ollama/test")

        self.command({"command": "stop_process", "process": "p1"})
        self.assertTrue(wait_for(lambda: any(row["id"] == "p1" and row["status"] == "stopped"
                                             for row in self.published()[-1]["processes"])))
        stopped = next(row for row in self.published()[-1]["processes"] if row["id"] == "p1")
        self.assertEqual(stopped["stoppedBy"], "user")

        before = len(self.events)
        self.command({"command": "stop_process", "process": "p9"})
        self.assertIn("No background process 'p9'", self.published()[-1]["notice"])
        self.assertFalse([event for event in self.events[before:] if event["event"] in {"error", "rejected", "state"}])

        pid = next(row["pid"] for row in self.published()[-1]["processes"] if row["id"] == "p2")
        self.command({"command": "delete"})
        self.assertEqual(self.host.processes.snapshot(self.chat), [])
        self.assertTrue(wait_for(lambda: gone(pid)))

    def test_shutdown_ends_processes_when_an_earlier_step_fails(self):
        service = self.host.session(self.chat)
        self.host.processes.start(service, service.chat["workspace"], "sleep 30", dict(os.environ), service.cancel)
        pid = self.host.processes.snapshot(self.chat)[0]["pid"]
        # A closed UI pipe makes Side Chat cleanup raise before the registry runs.
        with mock.patch("chat_agents.close_all_sides", side_effect=BrokenPipeError):
            with self.assertRaises(BrokenPipeError):
                self.host.shutdown()
        self.assertTrue(self.host.processes.closing)
        self.assertTrue(wait_for(lambda: gone(pid)))


if __name__ == "__main__":
    unittest.main()
