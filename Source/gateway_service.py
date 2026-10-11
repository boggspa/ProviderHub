"""The gateway's lifetime as a service: started once per state root, stopped on request.

No client process is the gateway's lifetime authority. The hub app, the Chat
Host and the Chat window each ensure a gateway is running and leave it running
when they exit, crash or restart, so restarting one never interrupts requests
another started (a desktop session's, or another chat's). The gateway's own
flock on gateway.lock keeps one gateway per state root; a second start attaches
to the first. Stopping is an authenticated request to that gateway, never a
signal sent to a remembered PID.

    python gateway_service.py ensure|status|stop [--if-idle]
"""
from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

from bridge_core import BridgeError, gateway_token, load_settings, state_root

READY_SECONDS = 30
STOP_SECONDS = 15


class GatewayError(ValueError):
    pass


class GatewayBusy(GatewayError):
    pass


def held(root):
    """Whether some process holds this state root's gateway lock right now."""
    path = Path(root) / "gateway.lock"
    if path.is_symlink(): raise GatewayError("The gateway lock must not be a symbolic link.")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)


def request(root, method, path, body=None, *, timeout=5):
    """One authenticated loopback request; the token proves the state root."""
    connection = http.client.HTTPConnection("127.0.0.1", load_settings(Path(root))["port"], timeout=timeout)
    try:
        headers = {"Authorization": "Bearer " + gateway_token(Path(root))}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read(1_000_000)
        try: value = json.loads(raw or b"{}")
        except ValueError: value = {}
        return response.status, value if isinstance(value, dict) else {}
    finally:
        connection.close()


def message(value, fallback):
    error = value.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str): return error["message"]
    return error if isinstance(error, str) else fallback


def status(root, *, timeout=5):
    """The serving gateway's own report, or None when none answers for this root."""
    try:
        code, value = request(root, "GET", "/_bridge/status", timeout=timeout)
    except (OSError, ValueError, BridgeError):
        return None
    return value if code == 200 else None


def spawn(root, *, python=None, timeout=READY_SECONDS, env=None):
    """Start `gateway.py serve --detach`; returns its first line, parsed."""
    environment = {**os.environ, **(env or {}), "MISTRAL_BRIDGE_STATE_DIR": str(root)}
    process = subprocess.Popen([python or sys.executable, str(Path(__file__).resolve().with_name("gateway.py")), "serve", "--detach"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env=environment, start_new_session=True, close_fds=True)
    deadline = time.monotonic() + timeout
    data = b""
    try:
        while b"\n" not in data and len(data) < 65_536:
            remaining = deadline - time.monotonic()
            if remaining <= 0: break
            readable, _, _ = select.select([process.stdout], [], [], remaining)
            if not readable: continue
            chunk = os.read(process.stdout.fileno(), 65_536)
            if not chunk: break
            data += chunk
    finally:
        process.stdout.close()
    try:
        reply = json.loads(data.split(b"\n", 1)[0])
    except ValueError:
        reply = {}
    if not isinstance(reply, dict) or reply.get("ready") is not True:
        if process.poll() is None:
            # It never reported readiness: this caller started it, so it ends it.
            process.terminate()
            try: process.wait(5)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        return reply if isinstance(reply, dict) else {}
    return reply


def ensure(root, *, timeout=READY_SECONDS, python=None, env=None):
    """Return the serving gateway's status, starting a detached one if none runs.

`started` says whether this call started it. A gateway that holds the lock but
does not yet answer is still starting; this waits for it instead of competing.
"""
    deadline = time.monotonic() + timeout
    while True:
        if held(root):
            current = status(root, timeout=2)
            if current: return {**current, "started": False}
        else:
            reply = spawn(root, python=python, timeout=max(1, deadline - time.monotonic()), env=env)
            if reply.get("ready"):
                current = status(root)
                if current: return {**current, "started": True}
            elif "already running" not in str(reply.get("error", "")):
                raise GatewayError(reply.get("error") or "The gateway could not start. Check the runtime and gateway port.")
        if time.monotonic() >= deadline:
            raise GatewayError("The gateway did not become ready in time.")
        time.sleep(.1)


def stop(root, *, if_idle=False, timeout=STOP_SECONDS):
    """Ask this root's gateway to stop and wait until its lock is released."""
    if not held(root): return {"running": False}
    try:
        code, value = request(root, "POST", "/_bridge/shutdown", {"ifIdle": if_idle})
    except (OSError, BridgeError) as exc:
        raise GatewayError("The running gateway did not accept a stop request: " + str(exc)) from exc
    if code == 409: raise GatewayBusy(message(value, "The gateway is busy."))
    if code != 202: raise GatewayError(message(value, f"The gateway refused to stop ({code})."))
    deadline = time.monotonic() + timeout
    while held(root):
        if time.monotonic() >= deadline: raise GatewayError("The gateway did not stop in time.")
        time.sleep(.05)
    return {"running": False, "pid": value.get("pid")}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Provider Hub gateway service lifetime")
    parser.add_argument("action", choices=["ensure", "status", "stop"])
    parser.add_argument("--if-idle", action="store_true")
    arguments = parser.parse_args(argv)
    root = state_root()
    try:
        if arguments.action == "ensure": result = ensure(root)
        elif arguments.action == "stop": result = stop(root, if_idle=arguments.if_idle)
        else: result = status(root) or {"running": False}
    except (GatewayError, BridgeError, OSError) as exc:
        print(json.dumps({"ok": False, "busy": isinstance(exc, GatewayBusy), "error": str(exc)}), flush=True)
        return 1
    print(json.dumps({"ok": True, **result}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
