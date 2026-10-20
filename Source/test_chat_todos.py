"""Per-lane to-do lists: validation, Team sharing, persistence and resume."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

import chat_team
import chat_todos as todos
import cli_routes
import grok_cli_agent
from chat_remote_controls import project
from chat_runtime import ChatService, ChatStore
from cli_host_mcp import HostToolset
from test_chat_agents import call
from test_chat_runtime import FakeTransport, model, response

NOW = "2026-10-20T12:00:00+00:00"
PLAN = [{"id": "read", "content": "Read the config", "status": "completed"},
        {"id": "parser", "content": "Write the parser", "status": "in_progress"},
        {"id": "tests", "content": "Add tests", "status": "pending"}]
SOL, LUNA = {"id": "a" * 32, "name": "Sol"}, {"id": "b" * 32, "name": "Luna"}


def item(identifier, status="pending", content=None):
    return {"id": identifier, "content": content or "Do " + identifier, "status": status}


def references(request):
    """Every saved to-do reference the model was shown, wherever it rode."""
    return json.dumps(request["messages"]).count("[Saved to-dos:")


class TodoListTests(unittest.TestCase):
    def setUp(self):
        self.chat = {"id": "c" * 32, "route": "ollama/test", "account": "",
                     "team": {"enabled": True, "members": [{**SOL, "route": "r1"}, {**LUNA, "route": "r2"}]}}

    def test_items_are_validated_and_content_is_one_line(self):
        rows = todos.items([item("a.1"), {"id": "B_2", "content": "  Write\n the   parser ", "status": "completed"}])
        self.assertEqual(rows[1], {"id": "B_2", "content": "Write the parser", "status": "completed"})
        for bad, message in (([item("x"), item("x")], "Duplicate"),
                             ([{"id": "x", "content": "y", "status": "pending", "activeForm": "Doing y"}], "remove activeForm"),
                             ([{"id": "x", "content": "y"}], "exactly id, content and status"),
                             ([item("has space")], "letters, digits"),
                             ([item("x", "blocked")], "pending, in_progress or completed"),
                             ([item("x", content="z" * 201)], "1-200"),
                             ([item(str(i)) for i in range(25)], "at most 24"),
                             ({"id": "x"}, "a list")):
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                todos.items(bad)
        self.assertEqual(todos.items([]), [])

    def test_a_lane_holds_one_item_in_progress(self):
        with self.assertRaisesRegex(ValueError, r"one to-do can be in_progress; this list has 2 \(a, b\)"):
            todos.write(self.chat, SOL["id"], todos.items([item("a", "in_progress"), item("b", "in_progress")]), now=NOW)
        self.assertNotIn("todos", self.chat)
        todos.write(self.chat, SOL["id"], todos.items([item("a", "in_progress"), item("b")]), now=NOW)
        todos.write(self.chat, LUNA["id"], todos.items([item("a", "in_progress")]), now=NOW)
        self.assertEqual(todos.snapshot(self.chat)["lanes"][0]["items"][0]["status"], "in_progress")

    def test_shared_claims_stay_with_their_member_one_each(self):
        todos.write(self.chat, todos.SHARED, todos.items([item("api", "in_progress"), item("docs")]), now=NOW, writer=SOL, seen=0)
        # Luna keeps Sol's claim and takes another item; Sol's claim is not hers.
        saved = todos.write(self.chat, todos.SHARED, todos.items([item("api", "in_progress"), item("docs", "in_progress")]),
                            now=NOW, writer=LUNA, seen=1)
        self.assertEqual([(row["id"], row.get("ownerName")) for row in saved["items"]], [("api", "Sol"), ("docs", "Luna")])
        self.assertEqual(saved["revision"], 2)
        with self.assertRaisesRegex(ValueError, "Luna would hold docs, tests"):
            todos.write(self.chat, todos.SHARED, todos.items([item("api", "in_progress"), item("docs", "in_progress"),
                                                              item("tests", "in_progress")]), now=NOW, writer=LUNA, seen=2)
        done = todos.write(self.chat, todos.SHARED, todos.items([item("api", "completed"), item("docs")]), now=NOW, writer=LUNA, seen=2)
        self.assertEqual([(row["status"], row.get("ownerName")) for row in done["items"]], [("completed", "Luna"), ("pending", None)])
        self.assertIn("- [completed by Luna] api: Do api", todos.lines(done["items"]))

    def test_a_shared_write_from_a_stale_copy_is_refused_with_the_current_list(self):
        todos.write(self.chat, todos.SHARED, todos.items([item("api")]), now=NOW, writer=SOL, seen=0)
        with self.assertRaises(todos.StaleShared) as raised:
            todos.write(self.chat, todos.SHARED, todos.items([item("docs")]), now=NOW, writer=LUNA, seen=0)
        self.assertIn("changed since you last saw it", str(raised.exception))
        self.assertIn("- [pending] api: Do api", str(raised.exception))
        self.assertEqual([row["id"] for row in todos.lists(self.chat)[todos.SHARED]["items"]], ["api"])

    def test_lists_are_kept_per_member_and_pruned_to_the_roster(self):
        self.chat["todos"] = {"gone": {"items": [], "updated": NOW}}
        todos.write(self.chat, SOL["id"], todos.items([item("x")]), now=NOW)
        self.assertEqual(set(self.chat["todos"]), {SOL["id"]})
        snapshot = todos.snapshot(self.chat)
        self.assertEqual([(lane["name"], lane["done"], lane["total"]) for lane in snapshot["lanes"]], [("Sol", 0, 1), ("Luna", 0, 0)])
        self.assertIsNone(snapshot["shared"])

    def test_invalid_saved_lists_never_reach_a_model_or_the_inspector(self):
        self.chat["todos"] = {"main": {"items": [item("x", "blocked")], "updated": NOW}}
        self.assertIn("invalid and were not included", json.dumps(todos.reference(self.chat, "main")))
        events = []
        owner = SimpleNamespace(chat=self.chat, _mutex=threading.RLock(), emit=events.append)
        todos.publish(owner)
        self.assertIsNone(events[0]["todos"]); self.assertIn("Invalid saved", events[0]["notice"])

    def test_turning_team_on_and_off_carries_the_chat_list(self):
        solo = {"id": "c" * 32, "todos": {"main": {"items": [item("x")], "updated": NOW}}}
        team = {**solo, "team": {"enabled": True, "members": [SOL, LUNA]}}
        todos.carry(team, solo)
        self.assertEqual(list(team["todos"]), [SOL["id"]])
        off = {**team, "team": {"enabled": False, "members": [SOL, LUNA]}}
        todos.carry(off, team)
        self.assertEqual(list(off["todos"]), ["main"])
        self.assertEqual(list(solo["todos"]), ["main"], "the previous chat stays intact for a failed save")


class SoloTodoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = ChatStore(self.root, lock=False); self.events = []

    def service(self, responses, rows=None):
        transport = FakeTransport(responses)
        if rows: transport.rows = rows
        service = ChatService(self.store, transport, self.events.append)
        service.refresh(); service.create(transport.rows[0]["id"], str(self.root))
        return service, transport

    def turn(self, service, text="Go"):
        service.handle({"command": "send", "id": service.chat["id"], "text": text})
        service.thread.join(3)
        self.assertFalse(service.busy)

    def test_todo_write_saves_the_lane_draws_a_checklist_row_and_publishes(self):
        service, transport = self.service([call("todo_write", {"todos": PLAN}), response("ok")])
        self.turn(service)
        self.assertIn("todo_write", {tool["name"] for tool in transport.requests[0]["tools"]})
        self.assertNotIn("todo_read", {tool["name"] for tool in transport.requests[0]["tools"]})
        self.assertIn("todo_write", transport.requests[0]["system"])
        row = next(entry for entry in service.chat["entries"] if entry.get("tool") == "todo_write")
        self.assertEqual((row["summary"], row["isError"]), ("To-dos: 1/3 done\nWrite the parser", False))
        self.assertEqual(row["detail"].splitlines(), ["Saved your to-dos (1/3 completed).", "- [completed] read: Read the config",
                                                       "- [in_progress] parser: Write the parser", "- [pending] tests: Add tests"])
        saved = self.store.load(service.chat["id"])["todos"]["main"]
        self.assertEqual([entry["status"] for entry in saved["items"]], ["completed", "in_progress", "pending"])
        event = [e for e in self.events if e.get("event") == "todos"][-1]
        self.assertEqual((event["todos"]["team"], event["todos"]["lanes"][0]["done"], event["todos"]["lanes"][0]["total"]), (False, 1, 3))
        # The list is reference context, never saved provider history.
        self.assertIn("Write the parser", json.dumps(transport.requests[1]["messages"][0]))
        self.assertNotIn("[Saved to-dos:", json.dumps(service.chat["messages"]))

    def test_a_new_turn_and_a_restart_start_from_the_saved_list(self):
        service, transport = self.service([call("todo_write", {"todos": PLAN}), response("ok")])
        self.turn(service)
        restarted = ChatService(self.store, FakeTransport([response("resumed")]), self.events.append)
        restarted.initialize()
        self.assertEqual(restarted.chat["id"], service.chat["id"])
        self.turn(restarted, "Carry on")
        first = restarted.transport.requests[0]["messages"][0]
        self.assertIn("[in_progress] parser: Write the parser", json.dumps(first))
        self.assertTrue(any(e.get("event") == "todos" and e["todos"]["lanes"][0]["total"] == 3 for e in self.events))

    def test_switching_models_hands_the_list_to_the_new_model(self):
        service, transport = self.service([call("todo_write", {"todos": PLAN}), response("ok"), response("picked up")])
        self.turn(service)
        service.handle({"command": "configure", "id": service.chat["id"], "choice": transport.rows[1]["id"]})
        self.assertEqual(service.chat["route"], "ollama/other")
        self.turn(service, "Continue")
        latest = transport.requests[-1]
        self.assertEqual(latest["model"], "ollama/other")
        self.assertIn("Your to-dos (1/3 completed)", latest["messages"][0]["content"][0]["text"])

    def test_a_cli_route_gets_the_list_once_and_again_only_when_it_changes(self):
        rows = [{**model("claude/sonnet"), "source": "cli"}, {**model("codex/gpt-6"), "source": "cli"}]
        service, transport = self.service([
            call("todo_write", {"todos": PLAN}, "c1"), call("read_file", {"path": "a.txt"}, "c2"),
            call("read_file", {"path": "a.txt"}, "c3"), response("ok"), response("picked up")], rows)
        (self.root / "a.txt").write_text("hello")
        self.turn(service)
        counts = [references(request) for request in transport.requests]
        # Absent before the first write, delivered after it, then not again
        # while it stays the same over later tool steps.
        self.assertEqual(counts, [0, 1, 1, 1])
        service.handle({"command": "configure", "id": service.chat["id"], "choice": rows[1]["id"]})
        self.turn(service, "Continue")
        self.assertEqual(transport.requests[-1]["model"], "codex/gpt-6")
        self.assertEqual(references(transport.requests[-1]), 1)
        self.assertIn("Write the parser", json.dumps(transport.requests[-1]["messages"]))

    def test_solo_chats_refuse_team_arguments(self):
        service, transport = self.service([call("todo_write", {"todos": PLAN, "list": "shared"}, "c1"),
                                           call("todo_read", {}, "c2"), response("ok")])
        self.turn(service)
        rows = [entry for entry in service.chat["entries"] if entry.get("tool") in todos.TOOLS]
        self.assertEqual([row["isError"] for row in rows], [True, True])
        self.assertIn("one to-do list", rows[0]["detail"]); self.assertIn("only to Team members", rows[1]["detail"])
        self.assertNotIn("todos", service.chat)


class TeamTodoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ChatStore(self.root, lock=False); self.events = []

    def team(self, scripts, count=2, rows=None):
        self.transports = [FakeTransport(script) for script in scripts]
        if rows:
            for transport in self.transports: transport.rows = rows
        primary = FakeTransport([])
        if rows: primary.rows = rows
        available = iter(self.transports)
        parent = ChatService(self.store, primary, self.events.append, child_factory=lambda: next(available))
        parent.refresh(); parent.create(parent.models[0]["id"], str(self.root))
        specs = [{"choice": parent.models[i % len(parent.models)]["id"], "name": ["Sol", "Luna", "Vega", "Orion", "Lyra"][i],
                  "effort": "", "responsibility": ""} for i in range(count)]
        parent.handle({"command": "configure_team", "id": parent.chat["id"], "enabled": True,
                       "execution": {"mode": "contribution"}, "members": specs, "request": "config"})
        return parent

    def run_team(self, parent, text="Work on it"):
        parent.handle({"command": "send", "id": parent.chat["id"], "text": text})
        parent.thread.join(5)
        self.assertFalse(parent.busy)

    def wait(self, check):
        end = time.monotonic() + 3
        while not check() and time.monotonic() < end: time.sleep(.005)
        return check()

    def test_members_keep_their_own_lists_and_read_each_others(self):
        parent = self.team([[call("todo_write", {"todos": PLAN}), response("Planned")]], count=1)
        self.run_team(parent)
        sol = parent.chat["team"]["members"][0]
        self.assertEqual(list(parent.chat["todos"]), [sol["id"]])
        reader = self.team([[call("todo_read", {}), response("Read")], [response("Idle")]], count=2)
        luna = reader.chat["team"]["members"][1]
        todos.write(reader.chat, luna["id"], todos.items(PLAN[:1]), now=NOW)
        self.run_team(reader)
        offered = {tool["name"] for tool in self.transports[0].requests[0]["tools"]}
        self.assertTrue({"todo_write", "todo_read"} <= offered)
        row = next(entry for entry in reader.chat["entries"] if entry.get("tool") == "todo_read")
        self.assertIn("Sol (yours): none.", row["detail"])
        self.assertIn(f"Luna (member_id {luna['id']}) (1/1 completed):\n- [completed] read: Read the config", row["detail"])
        self.assertIn("Shared Team to-dos: none.", row["detail"])

    def test_a_shared_write_from_a_stale_copy_is_refused_until_the_member_has_seen_the_list(self):
        shared = [item("api"), item("docs")]
        def sol(payload, cancel, delta):
            # Both members' first requests are built before Sol writes.
            self.assertTrue(self.wait(lambda: self.transports[1].requests))
            return call("todo_write", {"todos": shared, "list": "shared"}, "s1")
        def luna(payload, cancel, delta):
            self.assertTrue(self.wait(lambda: "shared" in parent.chat.get("todos", {})))
            return call("todo_write", {"todos": [item("docs", "in_progress")], "list": "shared"}, "l1")
        retry = [item("api"), item("docs", "in_progress")]
        parent = self.team([[sol, response("Shared")], [luna, call("todo_write", {"todos": retry, "list": "shared"}, "l2"),
                                                         response("Claimed")]])
        self.run_team(parent)
        rows = [entry for entry in parent.chat["entries"] if entry.get("tool") == "todo_write"]
        refused = next(row for row in rows if row["memberName"] == "Luna" and row["isError"])
        self.assertIn("changed since you last saw it", refused["detail"])
        saved = parent.chat["todos"]["shared"]
        self.assertEqual((saved["revision"], [(r["id"], r["status"], r.get("ownerName")) for r in saved["items"]]),
                         (2, [("api", "pending", None), ("docs", "in_progress", "Luna")]))
        accepted = [row for row in rows if not row["isError"]]
        self.assertEqual([row["summary"].splitlines()[0] for row in accepted], ["Shared to-dos: 0/2 done"] * 2)
        self.assertEqual(accepted[-1]["summary"].splitlines()[1], "Do docs")
        # Peers see the shared list as reference, alongside their own.
        self.assertIn("Shared Team to-dos (revision 1", json.dumps(self.transports[1].requests[1]["messages"]))
        snapshot = [e for e in self.events if e.get("event") == "todos"][-1]["todos"]
        self.assertEqual((snapshot["team"], snapshot["shared"]["name"], snapshot["shared"]["done"], snapshot["shared"]["total"]),
                         (True, "Shared", 0, 2))

    def test_lists_ride_the_team_checkpoint_journal(self):
        parent = self.team([[call("todo_write", {"todos": PLAN}), response("Planned")]], count=1)
        member = parent.chat["team"]["members"][0]
        chat_team.start_run(parent, new_input=True)
        parent.save()
        todos.write(parent.chat, member["id"], todos.items(PLAN), now=NOW)
        chat_team.checkpoint(parent, member, {})
        loaded = self.store.load(parent.chat["id"])
        self.assertEqual(loaded["todos"][member["id"]]["items"][1]["content"], "Write the parser")

    def test_a_replacement_model_resumes_from_the_members_list(self):
        parent = self.team([[call("todo_write", {"todos": PLAN}), response("Planned")], [response("Resumed")]], count=1)
        self.run_team(parent)
        member = parent.chat["team"]["members"][0]
        parent.handle({"command": "configure_team", "id": parent.chat["id"], "enabled": True, "execution": {"mode": "contribution"},
                       "members": [{"id": member["id"], "choice": parent.models[1]["id"], "name": "Sol", "effort": "", "responsibility": ""}]})
        self.assertEqual(parent.chat["team"]["members"][0]["route"], "ollama/other")
        self.run_team(parent, "Keep going")
        first = self.transports[1].requests[0]
        self.assertEqual(first["model"], "ollama/other")
        self.assertIn("Your to-dos (1/3 completed):\n- [completed] read", json.dumps(first["messages"][0]["content"]).replace("\\n", "\n"))

    def test_turning_team_on_gives_the_chat_list_to_the_first_member(self):
        transport = FakeTransport([call("todo_write", {"todos": PLAN}), response("ok")])
        parent = ChatService(self.store, transport, self.events.append, child_factory=lambda: FakeTransport([]))
        parent.refresh(); parent.create(parent.models[0]["id"], str(self.root))
        parent.handle({"command": "send", "id": parent.chat["id"], "text": "Plan"}); parent.thread.join(3)
        parent.handle({"command": "configure_team", "id": parent.chat["id"], "enabled": True, "execution": {"mode": "contribution"},
                       "members": [{"choice": parent.models[0]["id"], "name": "Sol", "effort": "", "responsibility": ""}]})
        member = parent.chat["team"]["members"][0]
        self.assertEqual(list(self.store.load(parent.chat["id"])["todos"]), [member["id"]])
        lanes = [e for e in self.events if e.get("event") == "todos"][-1]["todos"]["lanes"]
        self.assertEqual([(lane["name"], lane["total"]) for lane in lanes], [("Sol", 3)])


class RouteTests(unittest.TestCase):
    ROWS = [{**model("ollama/test"), "source": "keychain"},
            {**model("claude/sonnet"), "source": "cli"}, {**model("codex/gpt-6"), "source": "cli"},
            {**model("grok/grok-4.7"), "source": "cli"}, {**model("antigravity/gemini-3.1-pro"), "source": "cli"}]

    def test_every_route_type_offers_the_todo_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = ChatStore(root, lock=False)
            transport = FakeTransport([]); transport.rows = copy.deepcopy(self.ROWS)
            service = ChatService(store, transport, lambda event: None)
            service.refresh()
            for row in self.ROWS:
                service.create(row["id"], str(root))
                payload = service.payload(service.choice())
                names = [tool["name"] for tool in payload["tools"]]
                with self.subTest(route=row["route"]):
                    self.assertIn("todo_write", names)
                    provider, upstream = row["route"].split("/", 1)
                    if provider == "ollama": continue
                    plan = cli_routes.plan_turn(provider, upstream, payload, {}, wanted_output=128)
                    self.assertIn("todo_write", [tool["name"] for tool in plan["body"]["tools"]])
            # Native names: Claude's MCP prefix, and Grok's catalog key, which
            # its removal list for the CLI's own todo_write does not touch.
            served = HostToolset(todos.TEAM_TOOLS, prefix="mcp__host__")
            self.assertEqual(served.host_name("mcp__host__todo_write"), "todo_write")
            catalog = HostToolset(todos.TEAM_TOOLS, prefix=grok_cli_agent._HOST_KEY_PREFIX, limit=grok_cli_agent._CATALOG_KEY_LIMIT)
            self.assertEqual(catalog.model_name("todo_write"), "host__todo_write")
            self.assertNotIn("host__todo_write", grok_cli_agent._DISALLOWED_TOOLS)

    def test_team_members_on_every_route_type_get_the_team_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ChatStore(root, lock=False)
            transports = [FakeTransport([response("Done")]) for _ in self.ROWS]
            for transport in transports: transport.rows = copy.deepcopy(self.ROWS)
            primary = FakeTransport([]); primary.rows = copy.deepcopy(self.ROWS)
            available = iter(transports)
            parent = ChatService(store, primary, lambda event: None, child_factory=lambda: next(available))
            parent.refresh(); parent.create(parent.models[0]["id"], str(root))
            parent.handle({"command": "configure_team", "id": parent.chat["id"], "enabled": True, "execution": {"mode": "contribution"},
                           "members": [{"choice": row["id"], "name": f"M{i}", "effort": "", "responsibility": ""}
                                       for i, row in enumerate(self.ROWS)]})
            parent.handle({"command": "send", "id": parent.chat["id"], "text": "Go"})
            parent.thread.join(5)
            seen = {transport.requests[0]["model"]: {tool["name"] for tool in transport.requests[0]["tools"]} for transport in transports}
            self.assertEqual(set(seen), {row["route"] for row in self.ROWS})
            for route, names in seen.items():
                with self.subTest(route=route):
                    self.assertTrue({"todo_write", "todo_read"} <= names)


class RemoteTodoTests(unittest.TestCase):
    def test_phone_panel_keeps_every_lane_and_the_hosts_counts(self):
        long = [{"id": f"i{n}", "content": "x" * 200, "status": "completed" if n < 5 else "pending"} for n in range(24)]
        lanes = [{"id": str(n) * 32, "name": f"M{n}", "route": "r", "choice": "r|", "items": long, "done": 5, "total": 24,
                  "updated": NOW} for n in range(8)]
        event = {"event": "todos", "chat": "c" * 32, "todos": {"team": True, "lanes": lanes,
                 "shared": {"id": "shared", "name": "Shared", "items": long[:2], "done": 2, "total": 2, "revision": 3, "updated": NOW,
                            "secret": "x"}}}
        projected = project(event, None)
        self.assertEqual(projected["panel"], "todos")
        value = projected["value"]
        self.assertEqual([lane["name"] for lane in value["lanes"]], [f"M{n}" for n in range(8)])
        self.assertTrue(all(lane["total"] == 24 and lane["done"] == 5 for lane in value["lanes"]))
        self.assertLess(len(value["lanes"][7]["items"]), 24, "a long list is shortened in transit")
        self.assertEqual((value["shared"]["revision"], value["shared"]["name"]), (3, "Shared"))
        self.assertNotIn("secret", json.dumps(value))
        self.assertLess(len(json.dumps(value).encode()), 24000)
        self.assertEqual(project({"event": "todos", "chat": "c" * 32, "todos": None, "notice": "bad"}, None)["value"], None)


class SourceTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent

    def test_mac_inspector_shows_lanes_with_progress_and_the_tool_row_a_checklist(self):
        inspector = (self.ROOT / "ChatInspector.swift").read_text()
        self.assertIn("ChatTodosSection(model: model)", inspector)
        self.assertLess(inspector.index("ChatTeamControls(model: model)"), inspector.index("ChatTodosSection(model: model)"))
        self.assertLess(inspector.index("ChatTodosSection(model: model)"), inspector.index("ChatBlackboardSection(model: model, chat: chat)"))
        section = (self.ROOT / "ChatTodos.swift").read_text()
        for needle in ("ChatSharedTodoLane(", "todos.shared", "lane.done", "lane.total", "ChatProviderIcon", "HubTheme.Semantic"):
            self.assertIn(needle, section)
        rows = (self.ROOT / "ChatTranscriptRows.swift").read_text()
        self.assertIn('entry.tool == "todo_write", let items = ChatTodoChecklist.parse(detail)', rows)
        shared = (self.ROOT / "ChatSharedInspector.swift").read_text()
        self.assertIn('Text("\\(done)/\\(total)")', shared)
        self.assertIn('"$SOURCE_DIR/ChatTodos.swift"', (self.ROOT / "build.sh").read_text())
        self.assertIn(" chat_todos ", (self.ROOT / "build.sh").read_text())
        model = (self.ROOT / "ChatInspectorModel.swift").read_text()
        self.assertIn('case "todos":', model)

    def test_companion_reads_the_panel_without_a_protocol_bump(self):
        package = self.ROOT.parent / "Packages/ProviderHubRemote/Sources/ProviderHubRemote"
        self.assertIn('CompanionTodoLists(model.store.controls["todos"])', (package / "CompanionInspector.swift").read_text())
        self.assertIn('item.tool == "todo_write", let todos = ChatTodoChecklist.parse', (package / "CompanionTranscript.swift").read_text())
        self.assertIn('"extensionVersion": 5', (self.ROOT / "chat_remote.py").read_text())


if __name__ == "__main__":
    unittest.main()
