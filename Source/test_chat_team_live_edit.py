"""Editing a running Team: each member changes at its own safe boundary."""
import json
import threading
import unittest

import chat_team
from chat_runtime import ChatService, ModelRequestError
from chat_tools import ChatToolRunner
from test_chat_agents import call
from test_chat_runtime import response
import test_chat_concurrency
import test_chat_team as fixtures


def spec(member, **changes):
    value = {key: member[key] for key in ("id", "name", "choice", "effort", "responsibility")}
    value.update(changes)
    return value


def blocked(started, release, text="Working"):
    """A model request that streams, then waits until released or cancelled."""
    def run(payload, cancel, delta):
        delta(text)
        started.set()
        while not release.wait(.005):
            if cancel.is_set(): raise InterruptedError("cancelled")
        return response(text + " finished")
    return run


class LiveEditTests(unittest.TestCase):
    setUp = fixtures.TeamTests.setUp
    service = fixtures.TeamTests.service
    send = fixtures.TeamTests.send
    finish = fixtures.TeamTests.finish
    wait_for = fixtures.TeamTests.wait_for
    chips = fixtures.TeamTests.chips

    def team(self, parent, names=("Kimi", "Sol")):
        specs = [{"choice": parent.models[0]["id"], "name": name, "effort": "", "responsibility": "Part " + name}
                 for name in names]
        parent.handle({"command": "configure_team", "id": parent.chat["id"], "enabled": True,
                       "execution": {"mode": "contribution"}, "members": specs, "request": "setup"})
        return parent.chat["team"]["members"]

    def edit(self, parent, members, request="edit", **extra):
        parent.handle({"command": "configure_team", "id": parent.chat["id"], "enabled": True,
                       "members": members, "request": request, **extra})
        return [e for e in self.events if e.get("event") == "team" and e.get("request") == request][-1]

    def notices(self, parent):
        return [e["text"] for e in parent.chat["entries"] if e.get("noticeKind") == "team_member_change"]

    def test_swapping_an_errored_member_resumes_its_slot_while_peers_keep_working(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        limit = ModelRequestError("Kimi Code returned HTTP 403: You've reached your 5-hour usage limit", status=403)
        parent = self.service([[limit], [blocked(started, release, "Sol")], [response("Picked up Kimi's part")]])
        kimi, sol = self.team(parent)
        slot = kimi["id"]
        self.send(parent)
        self.wait_for(lambda: kimi["status"] == "error")
        self.assertTrue(started.wait(2))
        self.assertIn("HTTP 403", kimi["failureReason"])
        event = self.edit(parent, [spec(kimi, choice=parent.models[1]["id"]), spec(sol)])
        self.assertNotIn("notice", event)
        # Sol is still mid-request; the replacement runs beside it at once.
        self.wait_for(lambda: kimi["status"] == "done")
        self.assertEqual(sol["status"], "working")
        release.set(); self.finish(parent)
        self.assertEqual((kimi["id"], kimi["route"], kimi["name"]), (slot, "ollama/other", "Kimi"))
        self.assertFalse(kimi.get("failureReason"))
        self.assertEqual(len(self.transports[1].requests), 1, "the healthy peer was interrupted")
        self.assertEqual(self.notices(parent), ["Kimi (ollama/test) was replaced by Kimi (ollama/other) by the user."])
        notice = next(e for e in parent.chat["entries"] if e.get("noticeKind") == "team_member_change")
        self.assertEqual(notice["memberID"], slot)
        # Portable record of the slot's own failure and the request, nothing private.
        context = json.dumps(self.transports[2].requests[0]["messages"])
        self.assertIn("Inspect the project", context)
        self.assertIn("5-hour usage limit", context)
        self.assertIn("you now continue in its place as Kimi", context)
        reply = next(e for e in parent.chat["entries"] if e["kind"] == "assistant" and e["text"] == "Picked up Kimi's part")
        self.assertEqual(reply["memberID"], slot)
        archived = [a["member"] for a in parent.chat["archives"] if a.get("kind") == "team_member"]
        self.assertEqual(archived[-1]["route"], "ollama/test")
        saved = self.store.load(parent.chat["id"])["team"]["members"]
        self.assertEqual([(m["id"], m["route"]) for m in saved], [(slot, "ollama/other"), (sol["id"], "ollama/test")])
        self.assertFalse(any("pendingChange" in m for m in saved))

    def test_swapping_a_member_mid_tool_waits_for_the_real_result(self):
        tool_started, release_tool = threading.Event(), threading.Event()
        self.addCleanup(release_tool.set)
        executed = []
        class Runner(ChatToolRunner):
            def execute(inner, name, args):
                executed.append(name)
                tool_started.set()
                self.assertTrue(release_tool.wait(3))
                self.assertFalse(inner.cancel_event.is_set(), "a running tool was cancelled for the edit")
                return {"content": [{"type": "text", "text": "real shell output"}], "is_error": False,
                        "summary": "Shell", "changed_files": []}
        two = {"role": "assistant", "stop_reason": "tool_use", "usage": {}, "content": [
            {"type": "tool_use", "id": "shell", "name": "run_shell", "input": {"command": "make"}},
            {"type": "tool_use", "id": "read", "name": "read_file", "input": {"path": "later.txt"}}]}
        parent = self.service([[two], [response("Sol done")], [response("Grok continued")]])
        parent.runner_type = Runner
        parent.chat["approvalMode"] = "yolo"
        kimi, sol = self.team(parent)
        self.send(parent)
        self.assertTrue(tool_started.wait(2))
        self.edit(parent, [spec(kimi, choice=parent.models[1]["id"], name="Grok"), spec(sol)])
        # Pending: nothing about the slot has changed yet, and nothing was cancelled.
        self.assertEqual((kimi["name"], kimi["route"], kimi["status"]), ("Kimi", "ollama/test", "working"))
        self.assertEqual(kimi["pendingChange"]["name"], "Grok")
        published = chat_team.public(parent.chat["team"])["members"][0]
        self.assertEqual(published["pendingChange"]["route"], "ollama/other")
        self.assertFalse(parent.team_children[kimi["id"]].cancel.is_set())
        release_tool.set()
        self.wait_for(lambda: kimi["status"] == "done")
        self.finish(parent)
        self.assertEqual(executed, ["run_shell"], "a call the old model had not started was executed")
        shell = next(e for e in parent.chat["entries"] if e.get("tool") == "run_shell")
        self.assertEqual((shell["detail"], shell["isError"]), ("real shell output", False))
        archived = next(a["member"] for a in parent.chat["archives"] if a.get("kind") == "team_member")
        results = {b["tool_use_id"]: b for m in archived["messages"] for b in m["content"] if b.get("type") == "tool_result"}
        self.assertEqual(results["shell"]["content"][0]["text"], "real shell output")
        self.assertEqual(results["read"]["content"][0]["text"], chat_team.NOT_STARTED)
        self.assertEqual(len(self.transports[0].requests), 1)
        self.assertIn("real shell output", json.dumps(self.transports[2].requests[0]["messages"]))
        self.assertEqual((kimi["name"], kimi["route"]), ("Grok", "ollama/other"))
        self.assertNotIn("pendingChange", kimi)
        self.assertEqual(self.notices(parent), ["Kimi (ollama/test) was replaced by Grok (ollama/other) by the user."])

    def test_swap_after_the_run_ended_leaves_the_slot_ready_to_resume(self):
        limit = ModelRequestError("Kimi Code returned HTTP 403: You've reached your 5-hour usage limit", status=403)
        parent = self.service([[limit], [response("Sol done")], [response("Grok resumed Kimi's part")]])
        kimi, sol = self.team(parent)
        self.send(parent); self.finish(parent)
        self.assertEqual((kimi["status"], parent.chat["team"]["status"]), ("error", "error"))
        # Peers finished first, so this is an ordinary idle edit.
        self.edit(parent, [spec(kimi, choice=parent.models[1]["id"], name="Grok"), spec(sol)])
        team = parent.chat["team"]
        grok = team["members"][0]
        self.assertEqual((grok["id"], grok["status"], team["status"], team["queue"]), (kimi["id"], "interrupted", "interrupted", [kimi["id"]]))
        self.assertEqual(self.notices(parent), ["Kimi (ollama/test) was replaced by Grok (ollama/other) by the user."])
        parent.handle({"command": "team_resume", "id": parent.chat["id"], "request": "resume"})
        self.finish(parent)
        # Only the replaced slot ran; Sol's finished work was not replayed.
        self.assertEqual([m["status"] for m in parent.chat["team"]["members"]], ["done", "ready"])
        self.assertEqual([len(t.requests) for t in self.transports], [1, 1, 1])
        self.assertIn("5-hour usage limit", json.dumps(self.transports[2].requests[0]["messages"]))

    def test_swapping_a_member_mid_request_cancels_only_that_request(self):
        started, never = threading.Event(), threading.Event()
        peer_started, peer_release = threading.Event(), threading.Event()
        self.addCleanup(never.set); self.addCleanup(peer_release.set)
        parent = self.service([[blocked(started, never, "Half a thought")], [blocked(peer_started, peer_release, "Sol")],
                               [response("Fresh start")]])
        kimi, sol = self.team(parent)
        self.send(parent)
        self.assertTrue(started.wait(2) and peer_started.wait(2))
        self.edit(parent, [spec(kimi, effort="high"), spec(sol)])
        self.wait_for(lambda: kimi["status"] == "done")
        self.assertFalse(parent.team_children[sol["id"]].cancel.is_set())
        peer_release.set(); self.finish(parent)
        partial = next(e for e in parent.chat["entries"] if e["text"] == "Half a thought")
        self.assertTrue(partial["recorded"])
        self.assertEqual(self.notices(parent), ["Kimi (ollama/test) was updated by the user: effort default → high."])
        self.assertEqual(self.transports[2].requests[0]["output_config"], {"effort": "high"})

    def test_removing_a_member_a_peer_waits_on_resumes_the_peer(self):
        started, never = threading.Event(), threading.Event()
        self.addCleanup(never.set)
        def wait_on_kimi(payload, cancel, delta):
            return call("team_status", {"state": "waiting", "member_id": kimi["id"], "next_step": "Use Kimi's findings"})
        parent = self.service([[blocked(started, never, "Kimi's notes")], [wait_on_kimi, response("Sol went on alone")]])
        kimi, sol = self.team(parent)
        self.send(parent)
        self.assertTrue(started.wait(2))
        self.wait_for(lambda: sol["status"] == "waiting")
        self.edit(parent, [spec(sol)])
        self.wait_for(lambda: len(parent.chat["team"]["members"]) == 1)
        self.finish(parent)
        self.assertEqual([m["id"] for m in parent.chat["team"]["members"]], [sol["id"]])
        self.assertEqual(sol["status"], "done")
        # Its recorded rows stay in the transcript.
        self.assertTrue(any(e.get("memberID") == kimi["id"] and e["text"] == "Kimi's notes" for e in parent.chat["entries"]))
        self.assertIn("Member Kimi was removed from the Team by the user", json.dumps(self.transports[1].requests[-1]))
        self.assertEqual(self.notices(parent), ["Kimi (ollama/test) was removed from the Team by the user."])
        self.assertEqual([m["id"] for m in self.store.load(parent.chat["id"])["team"]["members"]], [sol["id"]])

    def test_adding_a_member_mid_run_starts_its_contribution_at_once(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        parent = self.service([[blocked(started, release, "Kimi")], [response("Sol joined in")]])
        kimi, = self.team(parent, names=("Kimi",))
        self.send(parent)
        self.assertTrue(started.wait(2))
        self.edit(parent, [spec(kimi), {"name": "Sol", "choice": parent.models[1]["id"], "effort": "", "responsibility": ""}])
        sol = parent.chat["team"]["members"][1]
        self.wait_for(lambda: sol["status"] == "done")
        self.assertEqual(kimi["status"], "working")
        release.set(); self.finish(parent)
        self.assertIn("Inspect the project", json.dumps(self.transports[1].requests[0]["messages"]))
        self.assertEqual(self.notices(parent), ["Sol (ollama/other) was added to the Team by the user."])
        self.assertEqual(len(self.store.load(parent.chat["id"])["team"]["members"]), 2)

    def test_cap_counts_a_member_still_leaving(self):
        events = [threading.Event() for _ in range(chat_team.MAX_MEMBERS)]
        never = threading.Event(); self.addCleanup(never.set)
        parent = self.service([[blocked(event, never)] for event in events])
        members = self.team(parent, names=[f"M{i}" for i in range(chat_team.MAX_MEMBERS)])
        self.send(parent)
        for event in events: self.assertTrue(event.wait(2))
        newcomer = {"name": "New", "choice": parent.models[1]["id"], "effort": "", "responsibility": ""}
        event = self.edit(parent, [spec(m) for m in members[1:]] + [newcomer])
        self.assertIn("Team is full", event["notice"])
        self.assertEqual(len(parent.chat["team"]["members"]), chat_team.MAX_MEMBERS)
        self.assertFalse(any("pendingChange" in m for m in members))
        parent.handle({"command": "stop"}); self.finish(parent)

    def test_stale_chips_are_refused_after_a_rename_or_swap(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        parent = self.service([[blocked(started, release, "Kimi"), response("Kimi read the update")], [response("Sol done")],
                               [response("Swapped Sol")]])
        kimi, sol = self.team(parent)
        self.send(parent)
        self.assertTrue(started.wait(2))
        self.wait_for(lambda: sol["status"] == "done")
        old_kimi, old_sol = self.chips(parent, "@Kimi look"), self.chips(parent, "@Sol look")
        # A rename applies at once, even mid-request; a swap of an idle member too.
        self.edit(parent, [spec(kimi, name="Kai"), spec(sol, choice=parent.models[1]["id"])])
        self.assertEqual((kimi["name"], sol["route"]), ("Kai", "ollama/other"))
        for text, stale in (("@Kimi look", old_kimi), ("@Sol look", old_sol)):
            before = len([e for e in self.events if e.get("event") == "team"])
            with self.assertRaisesRegex(ValueError, "no longer match"):
                parent.handle({"command": "steer", "id": parent.chat["id"], "text": text, "mentions": stale})
            resent = [e for e in self.events if e.get("event") == "team"][before:]
            self.assertEqual([m["name"] for m in resent[-1]["team"]["members"]], ["Kai", "Sol"])
            self.assertFalse(any(e["kind"] == "user" and e["text"] == text for e in parent.chat["entries"]))
        parent.handle({"command": "steer", "id": parent.chat["id"], "text": "@Kai look", "mentions": self.chips(parent, "@Kai look")})
        release.set(); self.finish(parent)
        self.assertEqual(kimi["status"], "done")

    def test_a_later_edit_supersedes_and_stop_discards_a_pending_change(self):
        tool_started, release_tool = threading.Event(), threading.Event()
        self.addCleanup(release_tool.set)
        class Runner(ChatToolRunner):
            def execute(inner, name, args):
                tool_started.set()
                while not release_tool.wait(.005):
                    if inner.cancel_event.is_set(): raise InterruptedError("Stopped")
                return {"content": [{"type": "text", "text": "ok"}], "is_error": False, "summary": "Shell", "changed_files": []}
        parent = self.service([[call("run_shell", {"command": "make"})], [response("Sol done")]])
        parent.runner_type = Runner
        parent.chat["approvalMode"] = "yolo"
        kimi, sol = self.team(parent)
        self.send(parent)
        self.assertTrue(tool_started.wait(2))
        swap = [spec(kimi, choice=parent.models[1]["id"]), spec(sol)]
        self.edit(parent, swap, "first")
        child = parent.team_children[kimi["id"]]
        self.assertTrue(child.team_changing)
        self.edit(parent, [spec(kimi), spec(sol)], "back")
        self.assertNotIn("pendingChange", kimi)
        self.assertFalse(child.team_changing, "a superseded change still ended the contribution")
        self.edit(parent, swap, "again")
        self.assertIn("pendingChange", kimi)
        parent.handle({"command": "stop"}); self.finish(parent)
        self.assertEqual((kimi["route"], kimi["status"]), ("ollama/test", "stopped"))
        self.assertNotIn("pendingChange", kimi)
        self.assertEqual(self.notices(parent), ["Kimi's change to Kimi (ollama/other) was not applied because the Team "
                                                "stopped first; its previous settings are kept."])
        self.assertFalse(any("pendingChange" in m for m in self.store.load(parent.chat["id"])["team"]["members"]))

    def test_team_cannot_be_turned_off_mid_run(self):
        started, never = threading.Event(), threading.Event()
        self.addCleanup(never.set)
        parent = self.service([[blocked(started, never)]])
        kimi, = self.team(parent, names=("Kimi",))
        self.send(parent); self.assertTrue(started.wait(2))
        parent.handle({"command": "configure_team", "id": parent.chat["id"], "enabled": False,
                       "members": [spec(kimi)], "request": "off"})
        refused = [e for e in self.events if e.get("event") == "team" and e.get("request") == "off"][-1]
        self.assertIn("Stop the Team before turning it off", refused["notice"])
        self.assertTrue(chat_team.enabled(parent.chat))
        parent.handle({"command": "stop"}); self.finish(parent)

    def test_run_settings_change_going_forward_without_resetting_the_allowance(self):
        started, never = threading.Event(), threading.Event()
        self.addCleanup(never.set)
        parent = self.service([[blocked(started, never)]])
        kimi, = self.team(parent, names=("Kimi",))
        self.send(parent); self.assertTrue(started.wait(2))
        began = parent.chat["team"]["runUsage"]["started"]
        self.edit(parent, [spec(kimi)], execution={"mode": "contribution", "minutes": 30, "contextTokens": 64000})
        team = parent.chat["team"]
        self.assertEqual((team["execution"]["minutes"], team["runUsage"]["started"]), (30, began))
        parent.handle({"command": "stop"}); self.finish(parent)

    def test_quit_mid_swap_reopens_with_the_previous_roster_and_a_notice(self):
        tool_started, release_tool = threading.Semaphore(0), threading.Event()
        self.addCleanup(release_tool.set)
        class Runner(ChatToolRunner):
            def execute(inner, name, args):
                tool_started.release(); release_tool.wait(3)
                return {"content": [{"type": "text", "text": "ok"}], "is_error": False, "summary": "Shell", "changed_files": []}
        # Both members are inside a tool, so both changes wait for a boundary.
        parent = self.service([[call("run_shell", {"command": "make"})], [call("read_file", {"path": "notes.txt"})],
                               [response("Grok done")]])
        parent.runner_type = Runner
        parent.chat["approvalMode"] = "yolo"
        kimi, sol = self.team(parent)
        self.send(parent)
        for _ in range(2): self.assertTrue(tool_started.acquire(timeout=2))
        self.edit(parent, [spec(kimi, choice=parent.models[1]["id"], name="Grok")])
        with parent._mutex: crashed = self.store.load(parent.chat["id"])
        saved = {m["id"]: m for m in crashed["team"]["members"]}
        self.assertEqual(saved[kimi["id"]]["pendingChange"]["name"], "Grok")
        self.assertEqual(saved[sol["id"]]["pendingChange"], {"remove": True})
        chat_team.recover(crashed, ChatService.settle)
        members = crashed["team"]["members"]
        self.assertEqual([(m["id"], m["name"], m["route"]) for m in members],
                         [(kimi["id"], "Kimi", "ollama/test"), (sol["id"], "Sol", "ollama/test")])
        self.assertFalse(any("pendingChange" in m for m in members))
        notices = [e["text"] for e in crashed["entries"] if e.get("noticeKind") == "team_member_change"]
        self.assertEqual(notices, ["Kimi's change to Grok (ollama/other) was not applied because Chat closed before it "
                                   "could apply; its previous settings are kept.",
                                   "Sol was not removed because Chat closed before it could apply; it stays in the Team."])
        release_tool.set(); self.finish(parent)
        # Landed changes are saved whole: reopening shows the applied roster.
        applied = self.store.load(parent.chat["id"])
        chat_team.recover(applied, ChatService.settle)
        self.assertEqual([(m["id"], m["name"], m["route"]) for m in applied["team"]["members"]],
                         [(kimi["id"], "Grok", "ollama/other")])


class HostLiveEditTests(unittest.TestCase):
    """The worker host lets a working Team be edited, and nothing else."""
    setUp = test_chat_concurrency.ConcurrentChatTests.setUp
    create = test_chat_concurrency.ConcurrentChatTests.create
    send = test_chat_concurrency.ConcurrentChatTests.send
    wait = test_chat_concurrency.ConcurrentChatTests.wait
    blocked = test_chat_concurrency.ConcurrentChatTests.blocked

    def test_host_swap_with_pending_approval_retires_it_without_running_the_tool(self):
        identifier = self.create("approval-swap")
        member = {"name": "Member", "choice": self.host.models[0]["id"], "effort": "", "responsibility": ""}
        self.host.handle({"command": "configure_team", "chat": identifier, "enabled": True,
                          "members": [member], "execution": {"mode": "contribution", "minutes": 10}})
        patch = "*** Begin Patch\n*** Add File: forbidden.txt\n+must not run\n*** End Patch"
        self.pending_children.extend([[response("", patch)], [response("Replacement finished")]])
        service = self.send(identifier, [])
        self.wait(lambda: service.approval is not None)
        approval_id = service.approval["id"]
        slot = service.chat["team"]["members"][0]["id"]
        started = service.chat["team"]["runUsage"]["started"]
        self.host.handle({"command": "configure_team", "chat": identifier, "enabled": True,
                          "members": [{**member, "id": slot, "choice": self.host.models[1]["id"]}]})
        self.wait(lambda: not service.busy)
        self.assertFalse((self.root / "approval-swap" / "forbidden.txt").exists())
        self.assertIsNone(service.approval)
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            self.host.handle({"command": "approve", "chat": identifier, "id": approval_id, "allow": True})
        saved = self.store.load(identifier)
        self.assertEqual(saved["team"]["members"][0]["id"], slot)
        self.assertEqual(saved["team"]["members"][0]["route"], "ollama/other")
        self.assertEqual(saved["team"]["runUsage"]["started"], started)
        self.assertTrue(any(row.get("text") == "Replacement finished" for row in saved["entries"]))

    def test_live_edit_cannot_widen_a_running_allowance(self):
        identifier = self.create("bounded-swap")
        member = {"name": "Member", "choice": self.host.models[0]["id"], "effort": "", "responsibility": ""}
        self.host.handle({"command": "configure_team", "chat": identifier, "enabled": True,
                          "members": [member], "execution": {"mode": "contribution", "minutes": 10, "tokens": 10000}})
        run, started, release = self.blocked()
        self.addCleanup(release.set)
        self.pending_children.append([run])
        service = self.send(identifier, [])
        self.assertTrue(started.wait(2))
        slot = service.chat["team"]["members"][0]
        for execution in ({"minutes": 11, "tokens": 10000}, {"minutes": 10, "tokens": None}):
            self.host.handle({"command": "configure_team", "chat": identifier, "enabled": True,
                              "request": "widen", "members": [spec(slot)], "execution": execution})
            self.assertIn("Stop the Team", [e for e in self.events if e.get("request") == "widen"][-1]["notice"])
        self.assertEqual(service.chat["team"]["execution"]["tokens"], 10000)
        release.set(); self.wait(lambda: not service.busy)

    def test_host_swap_queued_for_workspace_gate_keeps_running_peer_tool(self):
        identifier = self.create("gate-swap")
        members = [{"name": name, "choice": self.host.models[0]["id"], "effort": "", "responsibility": ""}
                   for name in ("Running", "Queued")]
        self.host.handle({"command": "configure_team", "chat": identifier, "enabled": True,
                          "members": members, "execution": {"mode": "contribution"}})
        entered, queued, release = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        executed = []
        class Runner(ChatToolRunner):
            def execute(inner, name, args):
                executed.append(args["command"])
                entered.set()
                self.assertTrue(release.wait(4))
                self.assertFalse(inner.cancel_event.is_set())
                return {"content": [{"type": "text", "text": "real peer result"}], "is_error": False,
                        "summary": "Shell", "changed_files": []}
        def queued_reply(payload, cancel, delta):
            self.assertTrue(entered.wait(2))
            queued.set()
            return call("run_shell", {"command": "queued-action"})
        self.pending_children.extend([[call("run_shell", {"command": "running-action"}), response("Peer done")],
                                      [queued_reply], [response("Replacement done")]])
        service = self.host.session(identifier)
        service.runner_type = Runner
        service.chat["approvalMode"] = "yolo"
        self.send(identifier, [])
        self.assertTrue(queued.wait(2))
        first, second = service.chat["team"]["members"]
        self.host.handle({"command": "configure_team", "chat": identifier, "enabled": True,
                          "members": [spec(first), spec(second, choice=self.host.models[1]["id"])]})
        self.wait(lambda: second["status"] == "done")
        self.assertEqual(first["status"], "working")
        self.assertEqual(executed, ["running-action"])
        release.set(); self.wait(lambda: not service.busy)
        saved = self.store.load(identifier)
        self.assertTrue(any(row.get("detail") == "real peer result" for row in saved["entries"]))
        archived = next(item["member"] for item in saved["archives"] if item.get("kind") == "team_member")
        results = [block for message in archived["messages"] for block in message["content"]
                   if block.get("type") == "tool_result"]
        self.assertEqual(results[0]["content"][0]["text"], chat_team.NOT_STARTED)

    def test_host_accepts_a_team_edit_while_it_works_but_not_a_solo_change(self):
        identifier = self.create("live-team")
        member = {"name": "Member", "choice": self.host.models[0]["id"], "effort": "", "responsibility": ""}
        self.host.handle({"command": "configure_team", "chat": identifier, "request": "roster", "enabled": True, "members": [member]})
        run, started, release = self.blocked("Team result")
        self.pending_children.append([run])
        team = self.send(identifier, [])
        self.assertTrue(started.wait(2))
        slot = team.chat["team"]["members"][0]["id"]
        self.host.handle({"command": "configure_team", "chat": identifier, "request": "live", "enabled": True,
                          "members": [{**member, "id": slot, "name": "Renamed"}]})
        self.assertEqual(team.chat["team"]["members"][0]["name"], "Renamed")
        with self.assertRaisesRegex(ValueError, "Stop this chat"):
            self.host.handle({"command": "configure", "chat": identifier, "id": identifier, "effort": "low"})
        release.set(); self.wait(lambda: not team.busy)


if __name__ == "__main__": unittest.main()
