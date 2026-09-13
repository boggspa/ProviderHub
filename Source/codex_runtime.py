"""Read-only qualification of the installed Codex catalogue parser."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time

from bridge_core import BridgeError, gateway_token
from codex_catalogue import project_codex
from codex_profile import CodexProfile, installed_app


def runtime_binary():
    app = installed_app()
    if not app:
        raise BridgeError("Install Codex / ChatGPT Desktop before preparing this harness.")
    path = Path(app) / "Contents/Resources/codex"
    if not path.is_file() or not os.access(path, os.X_OK):
        raise BridgeError("The installed desktop app has no usable Codex runtime.")
    return path


def runtime_signature(binary=None):
    path = Path(binary) if binary is not None else runtime_binary()
    stat = path.stat()
    return hashlib.sha256(json.dumps([str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]).encode()).hexdigest()


def qualify_runtime(settings, inventory, *, binary=None, timeout=25):
    """Model-list only in disposable process state; no turn or inference call."""
    binary = Path(binary) if binary is not None else runtime_binary()
    signature = runtime_signature(binary)
    expected = {model["slug"]: model for model in project_codex(settings, inventory)["models"]}
    with tempfile.TemporaryDirectory(prefix="provider-hub-catalogue-check-") as directory:
        base = Path(directory)
        root, config = base / "hub", base / "codex"
        profile = CodexProfile(root, config_home=config, running=lambda: False)
        profile.activate({**settings, "port": 9}, inventory)
        gateway_token(root)
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL"}}
        env["CODEX_HOME"] = str(config)
        events = queue.Queue()
        with (base / "diagnostics.log").open("w") as diagnostics:
            process = subprocess.Popen([str(binary), "-c", 'cli_auth_credentials_store="file"', "app-server"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=diagnostics,
                                       text=True, env=env, cwd=base)

            def read_output():
                for line in process.stdout:
                    try:
                        events.put(json.loads(line))
                    except ValueError:
                        continue
                events.put(None)
            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            deadline = time.monotonic() + timeout

            def send(value):
                process.stdin.write(json.dumps(value) + "\n")
                process.stdin.flush()

            def receive(identifier):
                while time.monotonic() < deadline:
                    try:
                        value = events.get(timeout=max(.01, deadline - time.monotonic()))
                    except queue.Empty:
                        break
                    if value is None:
                        break
                    if isinstance(value, dict) and value.get("id") == identifier:
                        if value.get("error"):
                            break
                        return value.get("result", {})
                raise BridgeError("The installed Codex runtime could not verify the provider catalogue. Its current configuration has not been switched.")
            try:
                send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "provider-hub-catalogue-check", "version": "0.5.0"},
                                                                 "capabilities": {"experimentalApi": True}}})
                receive(1)
                send({"method": "initialized", "params": {}})
                rows, cursor, identifier = [], None, 2
                while True:
                    send({"id": identifier, "method": "model/list", "params": {"includeHidden": True, "limit": 1000, "cursor": cursor}})
                    result = receive(identifier)
                    data = result.get("data")
                    if not isinstance(data, list):
                        raise BridgeError("The installed Codex runtime returned an invalid model catalogue.")
                    rows.extend(data)
                    cursor = result.get("nextCursor")
                    if not cursor:
                        break
                    identifier += 1
                    if identifier > 12:
                        raise BridgeError("Codex returned too many catalogue pages.")
                actual = {row.get("model"): row for row in rows if isinstance(row, dict)}
                if set(actual) != set(expected):
                    raise BridgeError("The installed Codex runtime did not load the Provider Hub model IDs. Its current configuration has not been switched.")
                for slug, model in expected.items():
                    row = actual[slug]
                    efforts = [entry.get("reasoningEffort") for entry in row.get("supportedReasoningEfforts", [])]
                    tiers = [entry.get("id") for entry in row.get("serviceTiers", [])]
                    if (row.get("displayName") != model["display_name"] or row.get("hidden") is True
                            or efforts != [entry["effort"] for entry in model["supported_reasoning_levels"]]
                            or tiers != [entry["id"] for entry in model["service_tiers"]]):
                        raise BridgeError("Codex did not accept the model names or controls in this catalogue. Its current configuration has not been switched.")
                    # When we advertise multi-agent v2, verify the runtime
                    # echoes it back. A null advertisement need not match.
                    expected_ma = model.get("multi_agent_version")
                    if expected_ma is not None:
                        actual_ma = row.get("multiAgentVersion")
                        if actual_ma != expected_ma:
                            raise BridgeError(
                                "Codex did not accept the multi-agent capability for this catalogue. "
                                "Its current configuration has not been switched.")
                return {"accepted": True, "model_count": len(rows), "runtime_signature": signature}
            except (OSError, ValueError, TypeError) as exc:
                raise BridgeError("The installed Codex runtime could not validate this catalogue. Its current configuration has not been switched.") from exc
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                reader.join(timeout=2)
                process.stdout.close()
