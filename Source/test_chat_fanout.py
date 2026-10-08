"""Bounded parallel lanes must overlap without sharing histories or write tools."""
import json
import threading
import unittest
from unittest.mock import patch

import test_chat_agents as fixtures
from chat_agents import validate_delegate
from test_chat_runtime import response


class FanoutTests(unittest.TestCase):
    setUp = fixtures.AgentTests.setUp
    service = fixtures.AgentTests.service
    send = fixtures.AgentTests.send
    finish = fixtures.AgentTests.finish
    wait_for = fixtures.AgentTests.wait_for

    def tasks(self, count=2):
        return {"tasks": [{"task": f"Independent task {index}"} for index in range(count)]}

    def test_lanes_overlap_and_keep_accounts_reasoning_and_results_isolated(self):
        barrier = threading.Barrier(2)
        def lane(label):
            def stream(payload, cancel, delta):
                barrier.wait(timeout=2)
                result = response(label)
                result["content"].insert(0, {"type": "thinking", "thinking": label + "-secret", "signature": label + "-signature"})
                return result
            return stream
        tasks = self.tasks()
        tasks["tasks"][1].update(choice="ollama/other|work", effort="low")
        parent = self.service([fixtures.call("delegate", tasks), response("Combined")], [[lane("first")], [lane("second")]])
        self.send(parent); self.finish(parent)
        records = self.store.load(parent.chat["id"])["agents"]
        self.assertEqual([record["status"] for record in records], ["ready", "ready"])
        self.assertEqual([record["account"] for record in records], ["", "work"])
        for index, transport in enumerate(self.children):
            self.assertEqual({tool["name"] for tool in transport.requests[0]["tools"]}, {"read_file", "search_files"})
            self.assertIn(f"Independent task {index}", json.dumps(transport.requests[0]["messages"]))
            self.assertNotIn(f"Independent task {1-index}", json.dumps(transport.requests[0]["messages"]))
        self.assertIn("first-signature", json.dumps(records[0]["messages"]))
        self.assertNotIn("second-signature", json.dumps(records[0]["messages"]))
        self.assertNotIn("-signature", json.dumps(self.transport.requests))
        self.assertNotIn("-secret", json.dumps(self.events))
        row = next(row for row in parent.chat["entries"] if row["kind"] == "tool")
        self.assertEqual(len(set(row["agentIDs"])), 2)
        self.assertIn("first", row["detail"]); self.assertIn("second", row["detail"])
        self.assertTrue(all(record["readOnly"] for record in records))

    def test_invalid_batches_launch_nothing_and_respect_remaining_budget(self):
        parent = self.service([], [])
        for args in [{"tasks": []}, self.tasks(1), self.tasks(4),
                     {"tasks": [{"task": "valid"}, {"task": "bad", "choice": "missing"}]},
                     {"tasks": [{"task": "valid"}, self.tasks()]},
                     {**self.tasks(), "task": "mixed"}]:
            with self.assertRaises(ValueError): validate_delegate(parent, args)
        self.assertNotIn("agents", parent.chat)
        parent._delegations = 3
        with self.assertRaisesRegex(ValueError, "No lane was launched"): validate_delegate(parent, self.tasks())

    def test_parallel_lanes_cannot_write_or_recurse_even_in_yolo(self):
        patch_text = "*** Begin Patch\n*** Add File: forbidden\n+no\n*** End Patch"
        replies = [[fixtures.call("run_shell", {"command": "touch forbidden"}), response("Denied")],
                   [fixtures.call("apply_patch", {"patch": patch_text}), fixtures.call("delegate", {"task": "Recurse"}), response("Denied too")]]
        parent = self.service([fixtures.call("delegate", self.tasks()), response()], replies)
        parent.chat["approvalMode"] = "yolo"
        self.send(parent); self.finish(parent)
        self.assertFalse((self.root / "forbidden").exists())
        self.assertFalse(any(event["event"] == "approval" for event in self.events))
        for transport in self.children:
            for request in transport.requests[1:]:
                results = [block for message in request["messages"] for block in message["content"] if block["type"] == "tool_result"]
                self.assertTrue(all(result["is_error"] for result in results))
        self.assertEqual(len(parent.chat["agents"]), 2)

    def test_partial_failure_keeps_successful_lane_result(self):
        parent = self.service([fixtures.call("delegate", self.tasks()), response("Parent continued")],
                              [[ValueError("provider limit")], [response("Useful result")]])
        self.send(parent); self.finish(parent)
        records = parent.chat["agents"]
        self.assertEqual([record["status"] for record in records], ["error", "ready"])
        result = self.transport.requests[-1]["messages"][-1]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("Useful result", result["content"][0]["text"])
        self.assertEqual(parent.chat["status"], "ready")

    def test_stop_cancels_every_lane_and_retry_does_not_relaunch(self):
        starts = [threading.Event(), threading.Event()]
        ends = [threading.Event(), threading.Event()]
        def lane(index):
            def stream(payload, cancel, delta):
                starts[index].set(); cancel.wait(2); ends[index].set(); raise InterruptedError("Stopped")
            return stream
        parent = self.service([fixtures.call("delegate", self.tasks()), response("Recovered")], [[lane(0)], [lane(1)]])
        self.send(parent)
        self.assertTrue(all(event.wait(2) for event in starts))
        parent.handle({"command": "stop"}); self.finish(parent)
        self.assertTrue(all(event.is_set() for event in ends))
        self.assertEqual([record["status"] for record in parent.chat["agents"]], ["stopped", "stopped"])
        parent.handle({"command": "retry", "id": parent.chat["id"]}); self.finish(parent)
        self.assertEqual(len(parent.chat["agents"]), 2)
        self.assertEqual([len(transport.requests) for transport in self.children], [1, 1])

    def test_steer_waits_for_all_three_lanes_before_parent_restart(self):
        starts, ends = [threading.Event() for _ in range(3)], [threading.Event() for _ in range(3)]
        def lane(index):
            def stream(payload, cancel, delta):
                starts[index].set(); cancel.wait(2); ends[index].set(); raise InterruptedError("Stopped")
            return stream
        def restarted(payload, cancel, delta):
            self.assertTrue(all(event.is_set() for event in ends)); return response("New direction")
        parent = self.service([fixtures.call("delegate", self.tasks(3)), restarted], [[lane(i)] for i in range(3)])
        self.send(parent); self.assertTrue(all(event.wait(2) for event in starts))
        parent.handle({"command": "steer", "id": parent.chat["id"], "text": "Change direction"})
        self.wait_for(lambda: not parent.busy)
        self.assertEqual(parent.chat["entries"][-1]["text"], "New direction")
        self.assertFalse(parent.lanes)

    def test_parallel_round_limit_is_eight_and_repeated_provider_ids_are_separate(self):
        rounds = [fixtures.call("read_file", {"path": "missing"}, "same-call-id") for _ in range(8)]
        parent = self.service([fixtures.call("delegate", self.tasks()), response()], [rounds, rounds])
        self.send(parent); self.finish(parent)
        self.assertEqual([len(child.requests) for child in self.children], [8, 8])
        self.assertTrue(all(record["status"] == "error" for record in parent.chat["agents"]))
        ids = [entry["id"] for record in parent.chat["agents"] for entry in record["entries"] if entry["kind"] == "tool"]
        self.assertEqual(len(ids), len(set(ids)))

    def test_thread_launch_failure_cancels_and_settles_registered_lanes(self):
        from chat_agents import delegate
        parent = self.service([], [[response("First")], [response("Second")]])
        start = threading.Thread.start
        launches = []
        def fail_second(thread):
            launches.append(thread)
            if len(launches) == 2: raise RuntimeError("Cannot start lane")
            return start(thread)
        with patch("chat_agents.threading.Thread.start", new=fail_second):
            with self.assertRaisesRegex(RuntimeError, "Cannot start lane"): delegate(parent, self.tasks(), "batch")
        self.assertTrue(all(record["status"] != "working" for record in parent.chat["agents"]))
        self.assertFalse(parent.lanes)

    def test_batch_budget_survives_steering_and_allows_only_the_remaining_slot(self):
        starts = [threading.Event() for _ in range(3)]
        def lane(index):
            def stream(payload, cancel, delta):
                starts[index].set(); cancel.wait(2); raise InterruptedError("Stopped")
            return stream
        replies = [fixtures.call("delegate", self.tasks(3), "first-batch"),
                   fixtures.call("delegate", self.tasks(), "excess-batch"),
                   fixtures.call("delegate", {"task":"Use last slot"}, "last-slot"), response("Budget held")]
        parent = self.service(replies, [[lane(i)] for i in range(3)] + [[response("Last result")]])
        self.send(parent); self.assertTrue(all(event.wait(2) for event in starts))
        parent.handle({"command":"steer", "id":parent.chat["id"], "text":"Change direction"})
        self.wait_for(lambda: not parent.busy)
        self.assertEqual(parent.chat["entries"][-1]["text"], "Budget held")
        self.assertEqual(len(parent.chat["agents"]), 4)
        self.assertEqual(parent.chat["delegationsThisTurn"], 4)
        self.assertEqual(self.store.load(parent.chat["id"])["delegationsThisTurn"], 4)
        self.assertEqual([len(child.requests) for child in self.children], [1, 1, 1, 1])


if __name__ == "__main__": unittest.main()
