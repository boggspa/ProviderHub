"""Test-only stdin peer for the Swift/Python signed handshake integration test."""
import json
import sys
import queue
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "Source"))
from chat_remote import MacBridge
from chat_remote import RemoteServer

if "--tcp" in sys.argv:
    tasks = queue.Queue()
    with tempfile.TemporaryDirectory() as directory:
        server = RemoteServer(lambda command: None, Path(directory) / "state.json", "wire-integration", dispatch=tasks.put)
        server.start("127.0.0.1", 0)
        server.publish({"event": "chats", "chats": [{"id": "ab" * 16, "title": "TCP chat"}]})
        print(json.dumps(server.bootstrap("127.0.0.1")), flush=True)
        while True:
            try:
                tasks.get(timeout=.05)()
            except queue.Empty:
                pass
            pending = server.status()["pendingPairing"]
            if pending:
                server.confirm_pairing(pending["identity"], pending["confirmCode"], True)

def execute(command):
    bridge.publish({"event": "delta", "chat": "ab" * 16, "text": "Wire response"})
    bridge.publish({"event": "entry", "chat": "ab" * 16, "entry": {"role": "assistant", "text": "Wire response"}})

bridge = MacBridge(execute, "wire-integration", auto_accept=True)
bridge.publish({"event": "chats", "chats": [{"id": "ab" * 16, "title": "Wire chat"}]})
bridge.publish({"event": "snapshot", "chat": "ab" * 16, "entries": [{"role": "user", "text": "Cross-language transcript"}]})
print(json.dumps({"host": "127.0.0.1", "port": 1, "sessionId": "wire-integration", "macIdentityPubKey": bridge.identity_b64()}), flush=True)
for line in sys.stdin:
    bridge.feed(json.loads(line))
    for frame in bridge.take():
        print(json.dumps(frame), flush=True)
