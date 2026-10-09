"""Probe the installed Codex runtime's thread feature replacement semantics.

Only a local scripted provider is contacted, under an isolated CODEX_HOME.
No account credentials, image generation or real model requests are used.
The host tool is never executed. --reproduce-legacy exposes a harmless
native printf in the read-only scratch directory to reproduce build 59.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "Source"))
from bridge_core import SLOTS
from cli_session import StdioSession, minimal_env
from codex_catalogue import project_codex
from codex_cli_agent import _TRANSPORT_CONFIG, _stream_turn, _thread_params, _tool_alias
from codex_runtime import runtime_binary
from hub_config import defaults

MODEL = "gpt-6-luna"
COMMAND = "printf NATIVE_COMMAND_PROBE"
HOST_TOOL = {"name": "exec_command", "description": "Execute through the host.",
             "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}


def probe(binary, *, images=True, legacy=False):
    settings = defaults(SLOTS, MODEL)
    settings["codex_model"] = "codex/" + MODEL
    card = project_codex(settings, {"models": [{"id": "codex/" + MODEL,
        "display_name": "Offline Luna probe", "context": 272000, "tools": True,
        "reasoning": True, "effort_modes": ["low", "high"]}]})["models"][0]
    card.update(slug=MODEL, shell_type="unified_exec", tool_mode="code_mode_only",
                base_instructions="Offline integration fixture. Use host tools.")
    requests, errors = [], []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            try:
                if self.path != "/v1/responses":
                    raise ValueError("Unexpected probe endpoint")
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                if len(requests) == 1:
                    item = {"id": "ct_native", "type": "custom_tool_call", "namespace": "functions",
                            "name": "exec", "call_id": "call_native", "input":
                            'text(await tools.exec_command({cmd: "' + COMMAND + '", login: false, yield_time_ms: 1000}));'}
                else:
                    if len(requests) > 2:
                        raise ValueError("The runtime did not stop at the host tool handoff")
                    item = {"id": "fc_host", "type": "function_call", "namespace": "host",
                            "name": _tool_alias("exec_command"), "call_id": "call_host",
                            "arguments": json.dumps({"cmd": "printf HOST_COMMAND_PROBE"})}
                response = {"id": "resp_probe", "object": "response", "created_at": 1,
                            "status": "completed", "model": MODEL, "output": [item],
                            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
                started = {**item, "input": ""} if item["type"] == "custom_tool_call" else {**item, "arguments": ""}
                events = [
                    {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                    {"type": "response.output_item.added", "output_index": 0, "item": started},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": response}]
                raw = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                errors.append(str(exc))
                self.send_error(500)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    scratch = REPO / ".local-only" / "native-command-probe"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="home-", dir=scratch) as directory:
            root = Path(directory)
            (root / "models.json").write_text(json.dumps({"models": [card]}))
            (root / "config.toml").write_text("\n".join([
                'model_provider="probe"', f'model="{MODEL}"',
                f'model_catalog_json={json.dumps(str(root / "models.json"))}',
                'approval_policy="never"', 'sandbox_mode="read-only"',
                '[features]', 'enable_request_compression=false',
                '[model_providers.probe]', 'name="Offline probe"',
                f'base_url="http://127.0.0.1:{server.server_port}/v1"',
                'wire_api="responses"', 'requires_openai_auth=false', 'supports_websockets=false']))
            argv = [str(binary)]
            for setting in _TRANSPORT_CONFIG:
                argv += ["-c", setting]
            argv.append("app-server")
            with StdioSession(argv, env=minimal_env({"CODEX_HOME": directory}), cwd=directory, timeout=15) as session:
                def rpc(method, params):
                    response = session.request(method, params, timeout=15)
                    if "error" in response:
                        raise RuntimeError(f"{method}: {response['error']}")
                    return response["result"]

                rpc("initialize", {"clientInfo": {"name": "hub-command-probe", "version": "1"},
                                   "capabilities": {"experimentalApi": True}})
                session.notify("initialized", {})
                params = _thread_params({"model": MODEL, "effort": "low", "tools": [HOST_TOOL],
                                         "native_image_generation": images}, SimpleNamespace(cwd=directory))
                if legacy:
                    params.setdefault("config", {})["features"] = {"image_generation": True}
                thread_id = rpc("thread/start", params)["thread"]["id"]
                turn_id = rpc("turn/start", {"threadId": thread_id, "approvalPolicy": "never",
                    "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                    "input": [{"type": "text", "text": "Offline fixture", "text_elements": []}]})["turn"]["id"]
                emitted = list(_stream_turn(session, turn_id=turn_id, thread_id=thread_id,
                    tools=[HOST_TOOL], native_images=images, deadline=time.monotonic() + 20))
        native_error = any(event.get("type") == "error" and "commandExecution" in event.get("message", "")
                           for event in emitted)
        visible = [event for event in emitted if event.get("type") not in {"usage", "ping"}]
        host_handoff = ([event["type"] for event in visible] == ["tool_call", "message_stop"]
                        and visible[0]["name"] == "exec_command"
                        and visible[-1].get("stop_reason") == "tool_use")
        outputs = [item.get("output") for request in requests for item in request.get("input", [])
                   if item.get("type") == "custom_tool_call_output"]
        rejected_natively = "tools.exec_command is not a function" in json.dumps(outputs)
        passed = native_error if legacy else not native_error and host_handoff and rejected_natively
        if errors or not passed:
            raise RuntimeError(json.dumps({"errors": errors, "events": emitted, "request_count": len(requests)}))
        return {"images": images, "legacy_table": legacy, "native_command_guard_fired": native_error,
                "unavailable_native_tool_returned_to_model": rejected_natively,
                "host_handoff_without_terminal_error": host_handoff, "request_count": len(requests)}
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--reproduce-legacy", action="store_true")
    args = parser.parse_args()
    binary = args.binary or runtime_binary()
    version = subprocess.check_output([str(binary), "--version"], text=True).strip()
    cases = [probe(binary, images=False), probe(binary)]
    if args.reproduce_legacy:
        cases.append(probe(binary, legacy=True))
    print(json.dumps({"runtime": version, "cases": cases}, indent=2))
