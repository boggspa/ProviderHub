"""Opt-in live qualification using Desktop's Claude runtime and real CLIs.

Usage: uv run python Source/verify_claude_cli_tools.py --claude /path/to/claude --provider muse
Uses the selected vendor CLI's existing login and may incur inference charges.
Only a temporary fixture and temporary gateway/configuration state are written.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import threading

from bridge_core import SLOTS, atomic_json, default_settings
from cli_session import minimal_env
from gateway import Runtime, Server
from hub_config import connection_signature


def verify(claude, provider):
    model = "muse-spark-1.3" if provider == "muse" else "grok-4.6"
    route = provider + "/" + model
    with tempfile.TemporaryDirectory(prefix="hub_claude_tool_check_") as directory:
        base = Path(directory)
        workspace, root = base / "workspace", base / "gateway"
        workspace.mkdir()
        fixture = workspace / "sample.py"
        before = "# A deliberately plain comment.\nVALUE = 7\n"
        fixture.write_text(before)
        settings = default_settings()
        settings["providers"][provider]["credential_mode"] = "cli"
        settings["mappings"] = {slot[0]: route for slot in SLOTS}
        atomic_json(root / "settings.json", settings)
        atomic_json(root / "catalogues" / (provider + ".json"), {
            "provider_id": provider, "source": "live-cli-qualification",
            "connection_signature": connection_signature(provider, settings["providers"][provider]),
            "models": [{"id": model, "canonical_id": model, "display_name": model,
                        "context": 1048576, "max_output": 131072, "aliases": [model],
                        "tools": True, "vision": False, "reasoning": True,
                        "effort_modes": ["low", "medium", "high"], "source": "cli"}],
        })
        runtime = Runtime(root, key="")
        server = Server(runtime, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = minimal_env({
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            "ANTHROPIC_API_KEY": runtime.token,
            "CLAUDE_CONFIG_DIR": str(base / "claude-config"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        })
        prompt = ("Read sample.py, replace its plain comment with '# The answer is seven; "
                  "the other 35 are on holiday.', then read the file again to verify it. "
                  "Preserve VALUE = 7 exactly. Complete the edit now and report the verified result.")
        argv = [str(claude), "--bare", "--restricted", "--setting-sources", "",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--no-session-persistence", "--model", "claude-fable-5", "--effort", "high",
                "--permission-mode", "acceptEdits", "--tools", "Read,Edit",
                "--output-format", "stream-json", "--verbose", "--max-turns", "8", "-p", prompt]
        try:
            print(json.dumps({"provider": provider, "phase": "started"}), flush=True)
            try:
                result = subprocess.run(argv, cwd=workspace, env=env, text=True,
                                        stdin=subprocess.DEVNULL, capture_output=True, timeout=180)
            except subprocess.TimeoutExpired as exc:
                def decoded(value):
                    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value or ""
                result = subprocess.CompletedProcess(argv, -1, decoded(exc.stdout), decoded(exc.stderr))
            events = []
            for line in result.stdout.splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
            calls, texts, results = [], [], []
            for event in events:
                if event.get("type") == "assistant":
                    for block in (event.get("message") or {}).get("content", []):
                        if block.get("type") == "tool_use":
                            calls.append(block["name"])
                        elif block.get("type") == "text":
                            texts.append(block.get("text", ""))
                elif event.get("type") == "result":
                    results.append({key: event.get(key) for key in
                                    ("subtype", "is_error", "result", "permission_denials")})
            content = fixture.read_text()
            passed = (result.returncode == 0 and calls.count("Read") >= 2 and "Edit" in calls
                      and content == "# The answer is seven; the other 35 are on holiday.\nVALUE = 7\n"
                      and results and results[-1].get("is_error") is False)
            report = {"provider": provider, "passed": bool(passed), "returncode": result.returncode,
                      "calls": calls, "texts": texts, "results": results, "fixture": content,
                      "gateway_completed": runtime.completed, "gateway_failed": runtime.failed,
                      "gateway_error": runtime.last_error, "stderr": result.stderr[-1500:]}
            if not passed:
                report["stdout_tail"] = result.stdout[-4000:].replace(runtime.token, "[redacted]")
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
            return bool(passed)
        finally:
            runtime.stopping.set()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude", required=True, type=Path)
    parser.add_argument("--provider", required=True, choices=("muse", "grok"))
    args = parser.parse_args()
    raise SystemExit(0 if verify(args.claude, args.provider) else 1)
