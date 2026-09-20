"""Exercise installed Codex delegation against an isolated, local fake provider.

No real model requests, credentials, user config changes, or workspace tools.
Run with uv run python scripts/probe_codex_subagents.py.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Source"))
from cli_session import StdioSession, minimal_env
from codex_cli_agent import _TRANSPORT_CONFIG, _thread_params, _tool_alias
from codex_catalogue import project_codex
from codex_runtime import runtime_binary
from hub_config import defaults
from bridge_core import SLOTS


def probe(binary, *, controls=(), fork_turns="none", target_index=6, multi_agent_version="v2",
          forward_host=False, ephemeral=False):
    routes = [f"mistral/probe-{n}" for n in range(7)]
    routes[-1] = "gemini/probe-flash"
    target = routes[target_index]
    settings = defaults(SLOTS, "probe-0")
    settings["codex_model"] = routes[0]
    catalogue = project_codex(settings, {"models": [
        {"id": route, "display_name": f"Probe {n}", "tools": True,
         "reasoning": True, "effort_modes": ["low", "high"], "context": 200000}
        for n, route in enumerate(routes)]})
    for row in catalogue["models"]:
        row["multi_agent_version"] = multi_agent_version
    requests = []
    host_calls = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "zstd":
                from compression import zstd
                raw = zstd.decompress(raw)
            body = json.loads(raw)
            requests.append(body)
            previous = any(item.get("type") == "function_call_output"
                           for item in body.get("input", []) if isinstance(item, dict))
            if body["model"] == routes[0] and not previous:
                item = {"id": "fc_probe", "type": "function_call", "namespace": "host" if forward_host else "collaboration",
                        "name": _tool_alias("spawn_agent") if forward_host else "spawn_agent", "call_id": "call_probe",
                        "arguments": json.dumps({"task_name": "probe_child", "message": "Reply probe OK.",
                                                 "model": target, "fork_turns": fork_turns})}
            else:
                item = {"id": "msg_probe", "type": "message", "role": "assistant",
                        "status": "completed", "content": [{"type": "output_text", "text": "probe OK", "annotations": []}]}
            response = {"id": "resp_probe", "object": "response", "created_at": 1,
                        "status": "completed", "model": body["model"], "output": [item],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
            events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                      {"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": ""}},
                      {"type": "response.output_item.done", "output_index": 0, "item": item},
                      {"type": "response.completed", "response": response}]
            data = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="hub-subagent-probe-") as directory:
            base = Path(directory)
            (base / "models.json").write_text(json.dumps(catalogue))
            config = '\n'.join([
                'model_provider = "probe"', f'model = "{routes[0]}"',
                f'model_catalog_json = {json.dumps(str(base / "models.json"))}',
                'approval_policy = "never"', 'sandbox_mode = "read-only"',
                '[features]', 'multi_agent = true', 'multi_agent_v2 = true',
                'shell_tool = false', 'apps = false', 'plugins = false', 'hooks = false',
                'enable_request_compression = false',
                '[model_providers.probe]', 'name = "Local probe"',
                f'base_url = "http://127.0.0.1:{server.server_port}/v1"',
                'wire_api = "responses"', 'requires_openai_auth = false',
            ])
            (base / "config.toml").write_text(config)
            argv = [str(binary)]
            for value in controls:
                argv.extend(["-c", value])
            argv.append("app-server")
            env = minimal_env({"CODEX_HOME": directory})
            with StdioSession(argv, env=env, cwd=directory, timeout=15) as session:
                def request(method, params):
                    result = session.request(method, params)
                    if "error" in result:
                        raise RuntimeError(result["error"])
                    return result["result"]
                request("initialize", {"clientInfo": {"name": "hub-subagent-probe", "version": "1"},
                                       "capabilities": {"experimentalApi": True}})
                session.notify("initialized", {})
                effective = request("config/read", {"includeLayers": False})["config"]
                params = {"cwd": directory, "model": routes[0],
                    "ephemeral": ephemeral, "approvalPolicy": "never", "sandbox": "read-only",
                    "config": {"model_reasoning_effort": "high"}}
                if forward_host:
                    from types import SimpleNamespace
                    params = _thread_params({"model": routes[0], "effort": "high", "tools": [{
                        "name": "spawn_agent", "description": "Delegate through the desktop's Provider Hub catalogue.",
                        "input_schema": {"type": "object", "properties": {
                            key: {"type": "string"} for key in ("model", "task_name", "message", "fork_turns")},
                            "required": ["task_name", "message"]}}]}, SimpleNamespace(cwd=directory))
                result = request("thread/start", params)
                parent_id = result["thread"]["id"]
                request("turn/start", {"threadId": parent_id,
                    "input": [{"type": "text", "text": "Run the local delegation probe.", "text_elements": []}]})
                for event in session.events(timeout=12):
                    if isinstance(event, dict) and event.get("method") == "item/tool/call":
                        host_calls.append(event["params"])
                        break
                    if (isinstance(event, dict) and event.get("method") == "turn/completed"
                            and event.get("params", {}).get("threadId") == parent_id):
                        if controls or any(body["model"] == target for body in requests):
                            break
                    if any(body["model"] == target for body in requests):
                        break
            spawn = []
            for tool in requests[0].get("tools", []) if requests else []:
                for child in tool.get("tools", [tool]):
                    if child.get("name") == "spawn_agent":
                        spawn.append(child)
            outputs = [item.get("output") for body in requests for item in body.get("input", [])
                       if isinstance(item, dict) and item.get("type") == "function_call_output"]
            return {"controls": controls, "fork_turns": fork_turns, "target": target,
                    "forward_host": forward_host, "host_calls": host_calls,
                    "multi_agent_version": multi_agent_version,
                    "effective_agents": effective.get("agents"),
                    "effective_features": {key: value for key, value in effective.get("features", {}).items()
                                           if key.startswith("multi_agent")},
                    "models_requested": [body["model"] for body in requests],
                    "spawn_tools": spawn, "tool_outputs": outputs}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--control", action="append", default=[])
    parser.add_argument("--fork-turns", default="none")
    parser.add_argument("--target-index", type=int, default=6)
    parser.add_argument("--forward-host", action="store_true")
    parser.add_argument("--ephemeral", action="store_true",
                        help="Reproduce nested CLI history-fork failures with an ephemeral parent.")
    parser.add_argument("--transport-controls", action="store_true",
                        help="Use the Codex CLI adapter's actual transport configuration.")
    args = parser.parse_args()
    controls = [*(_TRANSPORT_CONFIG if args.transport_controls else ()), *args.control]
    print(json.dumps(probe(args.binary or runtime_binary(), controls=controls,
                           fork_turns=args.fork_turns, target_index=args.target_index,
                           forward_host=args.forward_host, ephemeral=args.ephemeral), indent=2))
