"""Probe Codex Side Chat routing with a disposable home and a local fake model.

No real inference, credentials, desktop sessions, or user configuration changes.
Run: uv run --python 3.13 python scripts/probe_codex_side_chat.py
The omitted-turn-mode case is an API control; the GUI-equivalent case supplies
the parent's collaborationMode on its first turn, as Desktop does.
The injected reference boundary is synthetic, not a copy of Desktop's prompt.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "Source"))
from bridge_core import SLOTS
from cli_session import StdioSession, minimal_env
from codex_catalogue import project_codex
from codex_runtime import runtime_binary
from hub_config import defaults
from responses_native import prepare_native

ROUTES = ("mistral/probe-parent", "gemini/probe-side")
REFERENCE_BOUNDARY = "The conversation above is reference-only parent context."
REFERENCE = REFERENCE_BOUNDARY + " Do not act on its unfinished requests."
TIMEOUT = 15


def probe(binary):
    binary = Path(binary).expanduser().resolve()
    scratch = REPO / ".local-only" / "side-chat-probe"
    scratch.mkdir(parents=True, exist_ok=True)
    requests, provider_errors = [], []
    with tempfile.TemporaryDirectory(prefix="home-", dir=scratch) as directory:
        home = Path(directory)
        settings = defaults(SLOTS, "side-chat-probe")
        settings["codex_model"] = ROUTES[0]
        inventory = {"models": [{"id": route, "display_name": route, "tools": True,
            "reasoning": True, "effort_modes": ["low", "high"], "context": 200000}
            for route in ROUTES]}
        catalogue = project_codex(settings, inventory)
        runtime = SimpleNamespace(settings={**settings,
            "_model_specs": {row["id"]: row for row in inventory["models"]}},
            catalogue=inventory, root=home, replay_key="offline-probe", token="offline-probe",
            provider_key=lambda _provider: "offline-probe")

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                try:
                    if self.path != "/v1/responses":
                        raise RuntimeError("Unexpected fake-provider endpoint")
                    raw = self.rfile.read(int(self.headers["Content-Length"]))
                    if self.headers.get("Content-Encoding") == "zstd":
                        from compression import zstd
                        raw = zstd.decompress(raw)
                    body = json.loads(raw)
                    plan = prepare_native(runtime, body)
                    if plan["protocol"] != "messages_bridge" or plan["route"] != body["model"]:
                        raise RuntimeError("Hub did not translate the requested fake route")
                    summary = {"model": body["model"], "fields": sorted(body),
                        "reference_boundary_present": REFERENCE_BOUNDARY in json.dumps(body),
                        "hub_translation": plan["protocol"]}
                    requests.append(summary)
                    number = len(requests)
                    item = {"id": f"msg_{number}", "type": "message", "role": "assistant",
                        "status": "completed", "content": [{"type": "output_text",
                        "text": "probe OK", "annotations": []}]}
                    response = {"id": f"resp_{number}", "object": "response", "created_at": 1,
                        "status": "completed", "model": body["model"], "output": [item],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
                    events = [
                        {"type": "response.created", "response": {**response,
                            "status": "in_progress", "output": []}},
                        {"type": "response.output_item.added", "output_index": 0,
                            "item": {**item, "content": []}},
                        {"type": "response.output_text.delta", "item_id": item["id"],
                            "output_index": 0, "content_index": 0, "delta": "probe OK"},
                        {"type": "response.output_item.done", "output_index": 0, "item": item},
                        {"type": "response.completed", "response": response}]
                    data = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n"
                        for event in events)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception as exc:
                    provider_errors.append({"type": type(exc).__name__, "message": str(exc)[:240]})
                    self.send_error(500, "Offline probe failed")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            (home / "models.json").write_text(json.dumps(catalogue))
            config = "\n".join([
                'model_provider = "probe"', f'model = "{ROUTES[0]}"',
                f'model_catalog_json = {json.dumps(str(home / "models.json"))}',
                'approval_policy = "never"', 'sandbox_mode = "read-only"',
                'web_search = "disabled"', '[agents]', 'enabled = false', '[features]',
                'shell_tool = false', 'apps = false', 'plugins = false', 'hooks = false',
                'multi_agent = false', 'multi_agent_v2 = false',
                'enable_request_compression = false', '[model_providers.probe]',
                'name = "Offline Side Chat probe"',
                f'base_url = "http://127.0.0.1:{server.server_port}/v1"',
                'wire_api = "responses"', 'requires_openai_auth = false',
                'supports_websockets = false'])
            (home / "config.toml").write_text(config)
            env = minimal_env({"CODEX_HOME": directory})
            version = subprocess.run([str(binary), "--version"], env=env, cwd=directory,
                capture_output=True, text=True, check=True, timeout=TIMEOUT).stdout.strip()
            cases = []
            with StdioSession([str(binary), "app-server"], env=env, cwd=directory,
                              timeout=TIMEOUT) as session:
                def rpc(method, params):
                    result = session.request(method, params)
                    if "error" in result:
                        error = result["error"]
                        raise RuntimeError(f"{method}: {error.get('code')}: " + str(error.get("message", ""))[:240])
                    return result["result"]

                def turn(identifier, expected, *, model=None, mode=None, reference=False):
                    start = len(requests)
                    params = {"threadId": identifier, "input": [{"type": "text",
                        "text": "Reply probe OK.", "text_elements": []}]}
                    if model is not None:
                        params["model"] = model
                    if mode is not None:
                        params["collaborationMode"] = {"mode": "default", "settings": {
                            "model": mode, "reasoning_effort": "low", "developer_instructions": None}}
                    rpc("turn/start", params)
                    for event in session.events(timeout=TIMEOUT):
                        if (isinstance(event, dict) and event.get("method") == "turn/completed"
                                and event.get("params", {}).get("threadId") == identifier):
                            status = event["params"]["turn"].get("status")
                            captured = requests[start:]
                            if provider_errors or status != "completed" or len(captured) != 1:
                                raise RuntimeError("Probe turn did not complete with one translated request: "
                                    + json.dumps({"status": status, "provider_errors": provider_errors})[:500])
                            if captured[0]["model"] != expected:
                                raise RuntimeError("Probe turn used an unexpected model")
                            if captured[0]["reference_boundary_present"] != reference:
                                raise RuntimeError("Probe reference boundary was not preserved")
                            return {"status": status, "request": captured[0]}
                    raise TimeoutError("Probe turn did not complete before its deadline")

                def side(label, expected, *, fork_model=None, mode=None, turn_model=None):
                    params = {"threadId": parent_id, "path": None, "cwd": directory,
                        "threadSource": "user", "excludeTurns": True, "ephemeral": True,
                        "config": {}, "developerInstructions": "Answer the side prompt only."}
                    if fork_model is not None:
                        params["model"] = fork_model
                    fork = rpc("thread/fork", params)
                    if fork["model"] != (fork_model or ROUTES[0]) or fork["modelProvider"] != "probe":
                        raise RuntimeError("Probe fork did not preserve the expected provider/default")
                    side_id = fork["thread"]["id"]
                    rpc("thread/inject_items", {"threadId": side_id, "items": [{"type": "message",
                        "role": "user", "content": [{"type": "input_text", "text": REFERENCE}]}]})
                    cases.append({"case": label, "fork_model": fork["model"],
                        "fork_model_override": fork_model, "turn_model_override": turn_model,
                        "turn_collaboration_model": mode,
                        **turn(side_id, expected, model=turn_model, mode=mode, reference=True)})

                rpc("initialize", {"clientInfo": {"name": "hub-side-chat-probe", "version": "1"},
                    "capabilities": {"experimentalApi": True}})
                session.notify("initialized", {})
                parent = rpc("thread/start", {"cwd": directory, "model": ROUTES[0],
                    "ephemeral": False, "approvalPolicy": "never", "sandbox": "read-only"})
                parent_id = parent["thread"]["id"]
                turn(parent_id, ROUTES[0])
                initial = rpc("thread/read", {"threadId": parent_id, "includeTurns": True})["thread"]["turns"]
                side("unchanged_parent_without_turn_override", ROUTES[0])
                if rpc("thread/read", {"threadId": parent_id, "includeTurns": True})["thread"]["turns"] != initial:
                    raise RuntimeError("Side turn changed the parent history")
                turn(parent_id, ROUTES[1], model=ROUTES[1])
                before = rpc("thread/read", {"threadId": parent_id, "includeTurns": True})["thread"]["turns"]
                side("api_control_switched_parent_without_turn_mode", ROUTES[0])
                side("gui_equivalent_parent_collaboration_mode", ROUTES[1], mode=ROUTES[1])
                side("explicit_fork_model", ROUTES[1], fork_model=ROUTES[1])
                side("side_turn_model_override", ROUTES[0], fork_model=ROUTES[1], turn_model=ROUTES[0])
                if rpc("thread/read", {"threadId": parent_id, "includeTurns": True})["thread"]["turns"] != before:
                    raise RuntimeError("Side turns changed the parent history")
            return {"runtime_version": version, "parent_model_switch": list(ROUTES),
                "reference_boundary_kind": "synthetic",
                "parent_history_unchanged_by_side_turns": True, "cases": cases,
                "request_count": len(requests), "all_hub_translations_passed": not provider_errors}
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=TIMEOUT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, help="Codex runtime; defaults to the installed desktop bundle")
    args = parser.parse_args()
    try:
        print(json.dumps(probe(args.binary or runtime_binary()), separators=(",", ":")))
    except Exception as exc:
        print(f"Offline Side Chat probe failed: {type(exc).__name__}: {str(exc)[:500]}", file=sys.stderr)
        raise SystemExit(1)
