import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

from chat_plugin_mcp import MCPError, StdioMCPClient


SERVER = '''import json,sys,time
for line in sys.stdin:
    request=json.loads(line)
    if "id" not in request: continue
    method=request["method"]
    if method=="initialize":
        result={"protocolVersion":"2024-11-05","capabilities":{"tools":{}}}
    elif method=="tools/list":
        result={"tools":[{"name":"echo","description":"Echo","inputSchema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"],"additionalProperties":False}}]}
    else:
        text=request["params"]["arguments"].get("text", "")
        if text=="wait": time.sleep(5)
        if text=="malformed":
            print("garbage",flush=True); continue
        result={"content":[{"type":"text","text":text}]}
    print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result}),flush=True)
'''


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.script = Path(self.temp.name) / "fake.py"
        self.script.write_text(SERVER)
        self.client = StdioMCPClient(sys.executable, ("-u", str(self.script)), timeout=1)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_initialize_list_and_call(self):
        self.client.start()
        self.assertEqual(self.client.list_tools()[0]["name"], "echo")
        self.assertEqual(self.client.call_tool("echo", {"text": "hello"})["content"][0]["text"], "hello")

    def test_cancellation_retires_transport(self):
        self.client.start()
        cancel = threading.Event()
        timer = threading.Timer(.1, cancel.set)
        timer.start()
        try:
            with self.assertRaisesRegex(MCPError, "cancelled"):
                self.client.call_tool("echo", {"text": "wait"}, cancel)
            self.assertFalse(self.client.running)
        finally:
            timer.join()

    def test_timeout_retires_transport(self):
        self.client.start()
        with self.assertRaisesRegex(MCPError, "timed out"):
            self.client.call_tool("echo", {"text": "wait"})
        self.assertFalse(self.client.running)

    def test_malformed_response(self):
        self.client.start()
        with self.assertRaises(MCPError):
            self.client.call_tool("echo", {"text": "malformed"})

    def test_close_interrupts_request(self):
        self.client.start()
        timer = threading.Timer(.1, self.client.close)
        timer.start()
        try:
            with self.assertRaises(MCPError):
                self.client.call_tool("echo", {"text": "wait"})
        finally:
            timer.join()

    def test_relative_executable_rejected(self):
        with self.assertRaises(ValueError):
            StdioMCPClient("python")

    def test_server_request_is_rejected_without_host_execution(self):
        self.script.write_text('''import json,sys
r=json.loads(sys.stdin.readline())
print(json.dumps({"jsonrpc":"2.0","id":"host","method":"sampling/createMessage","params":{}}),flush=True)
answer=json.loads(sys.stdin.readline())
assert answer["error"]["code"]==-32601
print(json.dumps({"jsonrpc":"2.0","id":r["id"],"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}}}}),flush=True)
for line in sys.stdin: pass
''')
        self.client.start()

    def test_repeated_pagination_cursor_is_rejected(self):
        self.script.write_text(SERVER.replace('result={"tools":[', 'result={"nextCursor":"loop","tools":['))
        self.client.start()
        with self.assertRaisesRegex(MCPError, "pagination"):
            self.client.list_tools()

    def test_wrong_response_id_retires_transport(self):
        self.script.write_text(SERVER.replace('"id":request["id"]', '"id":999'))
        with self.assertRaisesRegex(MCPError, "response id"):
            self.client.start()
        self.assertFalse(self.client.running)

    def test_cancelled_start_does_not_spawn(self):
        from unittest.mock import patch
        cancel = threading.Event()
        cancel.set()
        with patch("chat_plugin_mcp.subprocess.Popen", side_effect=AssertionError("spawned")):
            with self.assertRaisesRegex(MCPError, "cancelled"):
                self.client.start(cancel)

    def test_cancelled_queued_request_does_not_interrupt_active_request(self):
        self.client.start()
        self.client._serial.acquire()
        cancel = threading.Event()
        cancel.set()
        try:
            with self.assertRaisesRegex(MCPError, "cancelled"):
                self.client.call_tool("echo", {"text": "hello"}, cancel)
            self.assertTrue(self.client.running)
        finally:
            self.client._serial.release()
        self.assertEqual(self.client.call_tool("echo", {"text": "next"})["content"][0]["text"], "next")

    def test_oversized_stdout_reaps_server(self):
        self.script.write_text('import sys,time\nsys.stdout.write("x"*2100000);sys.stdout.flush();time.sleep(10)\n')
        with self.assertRaises(MCPError):
            self.client.start()
        self.assertIsNotNone(self.client._process.poll())

    def test_large_screenshot_sized_tool_result_keeps_small_catalogue_limit(self):
        self.script.write_text(SERVER.replace('text=request["params"]["arguments"].get("text", "")',
                                             'text="x"*2100000'))
        self.client.start()
        result = self.client.call_tool("echo", {"text": "capture"})
        self.assertEqual(len(result["content"][0]["text"]), 2_100_000)
        self.assertEqual(self.client._response_limit, 2_000_000)

    def test_stderr_flood_is_drained_without_retention(self):
        self.script.write_text('import sys\nsys.stderr.write("x"*3000000);sys.stderr.flush()\n' + SERVER)
        self.client.start()
        self.assertEqual(self.client.call_tool("echo", {"text": "ok"})["content"][0]["text"], "ok")

    def test_cancelled_send_waiting_for_writer(self):
        self.client.start()
        self.client._write_lock.acquire()
        cancel = threading.Event()
        cancel.set()
        try:
            with self.assertRaisesRegex(MCPError, "cancelled"):
                self.client._send({"jsonrpc": "2.0", "method": "test"}, cancel)
        finally:
            self.client._write_lock.release()
