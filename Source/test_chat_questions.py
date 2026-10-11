"""Real tool-loop question ownership, safe waits, durable answers and async delivery."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import chat_questions as questions
from chat_agents import make_child
from chat_runtime import ChatService, ChatStore
from test_chat_agents import call
from test_chat_runtime import FakeTransport, response
import test_chat_team

ARGS = {"questions": [{"id": "colour", "header": "Colour", "question": "Which colour?",
                       "options": [{"label": "Blue", "description": "Cool"}, {"label": "Orange"}]}]}


class QuestionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.events = []
        self.store = ChatStore(self.root, lock=False)
        self.service = self.make_service([])

    def make_service(self, replies):
        transport = FakeTransport(replies)
        service = ChatService(self.store, transport, lambda event: self.events.append(copy.deepcopy(event)), child_factory=lambda: FakeTransport([]))
        service.refresh(); service.create(service.models[0]["id"], str(self.root))
        self.addCleanup(lambda: service.handle({"command": "stop"}))
        return service

    def wait_for(self, check):
        end = time.monotonic() + 3
        while not check() and time.monotonic() < end: time.sleep(.005)
        self.assertTrue(check())

    def record(self, service=None):
        return next(row["question"] for row in (service or self.service).chat["entries"] if row.get("question"))

    def answer(self, record, **values):
        self.service.handle({"command":"answer_question", "chat":self.service.chat["id"],
                             "question_id":record["id"], "answers":{"colour":["Blue"]}, **values})

    def send(self, service=None):
        service = service or self.service
        service.handle({"command":"send", "id":service.chat["id"], "text":"Do the work"})

    def test_waiting_tool_resolves_once_and_retains_answer(self):
        self.service.transport.responses = [call("request_user_input", ARGS), response("Blue chosen")]
        self.send(); self.wait_for(lambda: any(row.get("question") for row in self.service.chat["entries"]))
        record = self.record()
        other = self.make_service([response("Independent work")]); self.send(other)
        other.thread.join(2); self.assertFalse(other.busy)
        self.answer(record); self.answer(record, answers={"colour":["Orange"]})
        self.service.thread.join(2); self.assertFalse(self.service.busy)
        stored = self.store.load(self.service.chat["id"])
        self.assertEqual(next(e["question"] for e in stored["entries"] if e.get("question"))["answers"], {"colour":["Blue"]})
        payload = self.service.transport.requests[-1]["messages"][-1]["content"][0]["content"][0]["text"]
        self.assertEqual(json.loads(payload)["status"], "answered")
        self.assertIsNone(self.service.approval)

    def test_wrong_chat_and_wrong_requester_cannot_answer_or_wait(self):
        result = questions.execute(self.service, "request_user_input_async", ARGS)
        record = self.record()
        with self.assertRaisesRegex(ValueError, "another chat"):
            self.answer(record, chat="another")
        child = make_child(self.service, self.service.choice(), "", "lane", lambda _: None)
        with self.assertRaisesRegex(ValueError, "this agent"):
            questions.execute(child, "wait_for_user_input", {"request_id":record["id"], "timeout_ms":0})
        self.assertIn('"pending"', result["content"][0]["text"])

    def test_async_answer_arrives_at_boundary_without_cancel(self):
        entered, release = threading.Event(), threading.Event()
        def working(payload, cancel, delta):
            entered.set(); self.assertTrue(release.wait(3)); self.assertFalse(cancel.is_set())
            return response("Independent work done")
        self.service.transport.responses = [call("request_user_input_async", ARGS), working, response("Now using Blue")]
        self.send(); self.assertTrue(entered.wait(3))
        self.answer(self.record()); self.assertFalse(self.service.cancel.is_set()); release.set()
        self.wait_for(lambda: not self.service.busy)
        self.assertEqual(len(self.service.transport.requests), 3)
        self.assertIn("Blue", json.dumps(self.service.transport.requests[-1]["messages"]))

    def test_async_answer_restarts_an_idle_chat(self):
        self.service.transport.responses = [call("request_user_input_async", ARGS), response("Waiting whenever you are ready"), response("Resumed")]
        self.send(); self.wait_for(lambda: not self.service.busy)
        self.answer(self.record()); self.wait_for(lambda: len(self.service.transport.requests) == 3 and not self.service.busy)
        self.assertEqual(self.service.chat["entries"][-1]["text"], "Resumed")

    def test_timeout_keeps_question_open_and_stop_unblocks(self):
        questions.execute(self.service, "request_user_input_async", ARGS)
        identifier = self.record()["id"]
        result = questions.execute(self.service, "wait_for_user_input", {"request_id":identifier, "timeout_ms":1})
        self.assertIn('"pending"', result["content"][0]["text"])
        ended = threading.Event()
        def wait():
            try: questions.execute(self.service, "wait_for_user_input", {"request_id":identifier, "timeout_ms":60000})
            except InterruptedError: pass
            finally: ended.set()
        worker = threading.Thread(target=wait); worker.start()
        self.service.handle({"command":"stop"}); self.assertTrue(ended.wait(1)); worker.join()
        self.assertEqual(self.record()["status"], "cancelled")

    def test_dismissal_and_recovery_never_supply_an_answer(self):
        questions.execute(self.service, "request_user_input_async", ARGS)
        self.service.cancel.set()  # The record can settle without restarting work.
        self.answer(self.record(), dismiss=True)
        self.assertEqual(self.record()["status"], "dismissed"); self.assertEqual(self.record()["answers"], {})
        chat = {"entries":[{"question":{"status":"pending", "answers":{}}}]}
        self.assertTrue(questions.recover(chat)); self.assertFalse(questions.recover(chat))
        self.assertEqual(chat["entries"][0]["question"]["status"], "cancelled")

    def test_validation_and_save_failure_leave_pending(self):
        for args in ({"questions":[]}, {"questions":ARGS["questions"]*2}, {"questions":[{"id":"a","question":"", "options":[]}]}):
            with self.assertRaises(ValueError): questions.validate("request_user_input", args)
        questions.execute(self.service, "request_user_input_async", ARGS)
        with self.assertRaises(ValueError): self.answer(self.record(), answers={"colour":[]})
        with patch.object(self.service, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): self.answer(self.record())
        self.assertEqual(self.record()["status"], "pending")

    def test_team_member_wait_does_not_block_peer_and_keeps_asker(self):
        scripts = [[call("request_user_input", ARGS), response("Got the answer")], [response("Peer finished")]]
        helper = test_chat_team.TeamTests(); helper.store = self.store; helper.root = self.root; helper.events = self.events
        parent = helper.service(scripts); helper.configure(parent)
        self.service = parent
        self.send(parent)
        self.wait_for(lambda: any(e.get("question") for e in parent.chat["entries"]))
        self.wait_for(lambda: any(e.get("text") == "Peer finished" for e in parent.chat["entries"]))
        self.assertEqual(self.record()["asker"], "Member 1")
        self.answer(self.record()); self.wait_for(lambda: not parent.busy)
        self.assertEqual(parent.chat["status"], "ready")

    def test_helper_question_surfaces_in_root_and_stop_keeps_peers_open(self):
        first = make_child(self.service, self.service.choice(), "", "lane", lambda _: None)
        second = make_child(self.service, self.service.choice(), "", "lane", lambda _: None)
        questions.execute(first, "request_user_input_async", ARGS)
        questions.execute(second, "request_user_input_async", ARGS)
        first.handle({"command":"stop"})
        records = [row["question"] for row in self.service.chat["entries"] if row.get("question")]
        self.assertEqual([row["status"] for row in records], ["cancelled", "pending"])
        self.assertEqual(len(first.chat["entries"]), 0)

    def test_team_async_reply_targets_asking_member_without_cancelling_peer(self):
        helper = test_chat_team.TeamTests(); helper.store = self.store; helper.root = self.root; helper.events = self.events
        parent = helper.service([[]]); members = helper.configure(parent)
        child = make_child(parent, parent.choice(), "", "team", lambda _: None)
        child.team_parent, child.team_member = parent, members[0]
        parent._working = True
        members[0]["status"] = "working"; members[1]["status"] = "working"
        questions.execute(child, "request_user_input_async", ARGS)
        self.service = parent
        self.answer(self.record())
        self.assertEqual(parent.chat["entries"][-1]["mentions"][0]["id"], members[0]["id"])
        self.assertFalse(child.cancel.is_set()); self.assertFalse(parent.cancel.is_set())
        self.assertEqual(members[1]["status"], "working")
        parent._working = False


if __name__ == "__main__": unittest.main()
