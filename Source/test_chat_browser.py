"""Executable production WKWebView qualification using a loopback HTTP fixture."""
import http.server
from pathlib import Path
import platform
import subprocess
import tempfile
import threading
import time
import unittest
import chat_browser as cb

SWIFT = r'''
import AppKit
import WebKit
import Combine
@main struct Test {
 @MainActor static func main() async {
  NSApplication.shared.setActivationPolicy(.prohibited)
  let store = ChatBrowserStore(storageRoot:URL(fileURLWithPath:CommandLine.arguments[2]))
  let url = CommandLine.arguments[1]
  func check(_ ok:Bool,_ name:String) {
   let message = (ok ? "PASS " : "FAIL ")+name+"\n"
   FileHandle.standardError.write(Data(message.utf8))
   if !ok { exit(1) }
  }
  func call(_ id:String,_ tool:String,_ args:[String:Any]=[:]) async -> [String:Any] {
   await store.perform(chatID:"fixture",requestID:id,tool:tool,arguments:args)
  }
  _ = await call("list","browser_list")
  check(store.sessions.isEmpty,"list is inert")
  let cancelled = Task { @MainActor in await call("cancel","browser_open",["url":url]) }
  cancelled.cancel(); _ = await cancelled.value
  check(store.sessions.isEmpty,"pre-cancel inert")
  let opened = await call("open","browser_open",["url":url])
  check(opened["is_error"] as? Bool == false,"open loopback")
  var forwarded = 0
  let subscription = store.objectWillChange.sink { forwarded += 1 }
  guard let web = store.webView(forChatID:"fixture") else { check(false,"opened page exists"); return }
  // The macOS 14 async overlay returns nonoptional Any and traps on JavaScript
  // undefined. The callback API preserves nil, just like the production driver.
  func evaluatePage(_ script:String) async throws -> Any? {
   try await withCheckedThrowingContinuation { continuation in
    web.evaluateJavaScript(script) { value,error in
     if let error { continuation.resume(throwing:error) }
     else { continuation.resume(returning:value) }
    }
   }
  }
  let isolated = try? await evaluatePage("typeof window.__cbRefs")
  check(isolated as? String == "undefined","isolated world")
  let observed = await call("observe","browser_observe",["depth":8,"max_bytes":200000])
  if observed["is_error"] as? Bool == true { print(observed) }
  check(observed["is_error"] as? Bool == false,"observe")
  guard let snapshot = store.sessions["fixture"]?.page.snapshotID else { check(false,"observation includes snapshot identity"); return }
  do {
   let mutation = try await evaluatePage("document.getElementById('evil').setAttribute('data-cb-ref','r1')")
   check(mutation == nil,"undefined JavaScript result remains optional")
  } catch { check(false,"fixture mutation failed: \(error)") }
  let acted = await call("act","browser_act",["snapshot_id":snapshot,"actions":[["kind":"click","ref":"r1"]]])
  check(acted["is_error"] as? Bool == false,"act")
  let clicked = try? await evaluatePage("document.body.dataset.clicked")
  check(clicked as? String == "good","ref forgery rejected")
  let first = Task { @MainActor in await call("slow","browser_navigate",["url":url+"slow"]) }
  try? await Task.sleep(nanoseconds:150_000_000)
  let second = Task { @MainActor in await call("queued","browser_navigate",["url":url+"queued"]) }
  try? await Task.sleep(nanoseconds:100_000_000)
  second.cancel()
  let one = await first.value; let two = await second.value
  check(one["is_error"] as? Bool == false,"active unaffected by queued cancel")
  check(two["is_error"] as? Bool == true,"queued cancelled")
  check(store.sessions["fixture"]?.page.url.hasSuffix("slow") == true,"per chat serial")
  let newer = Task { @MainActor in await call("new","browser_navigate",["url":url+"slow2"]) }
  try? await Task.sleep(nanoseconds:150_000_000)
  store.cancel(chatID:"fixture",requestID:"slow")
  let newerResult = await newer.value
  check(newerResult["is_error"] as? Bool == false,"late cancel inert")
  let stale = await call("stale","browser_act",["snapshot_id":snapshot,"actions":[["kind":"click","ref":"r1"]]])
  check(stale["is_error"] as? Bool == true,"navigation invalidates old snapshot")
  let active = Task { @MainActor in await call("active","browser_navigate",["url":url+"slow3"]) }
  try? await Task.sleep(nanoseconds:150_000_000)
  store.cancel(chatID:"fixture",requestID:"active")
  let stopped = await active.value
  check(stopped["is_error"] as? Bool == true,"active cancellation")
  let after = await call("after","browser_open",["url":url])
  check(after["is_error"] as? Bool == false,"navigation after cancellation")
  check(forwarded > 0,"session metadata forwarded to store")
  let serialA = Task { @MainActor in await call("serialA","browser_navigate",["url":url+"slow4"]) }
  try? await Task.sleep(nanoseconds:100_000_000)
  let serialB = Task { @MainActor in await call("serialB","browser_navigate",["url":url+"final"]) }
  let resultA = await serialA.value; let resultB = await serialB.value
  check(resultA["is_error"] as? Bool == false && resultB["is_error"] as? Bool == false,"different request IDs serialized successfully")
  check(store.sessions["fixture"]?.page.url.hasSuffix("final") == true,"queued navigation finishes last")
  subscription.cancel()
  store.close(chatID:"fixture")
  print("WEBKIT_INTEGRATION_OK"); exit(0)
 }
}
'''

class WireTests(unittest.TestCase):
    def test_reject_non_http(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)", "data:text/plain,hi"):
            with self.assertRaises(ValueError):
                cb.describe("browser_open", {"url":url})

@unittest.skipUnless(platform.system() == "Darwin", "requires macOS WebKit")
class WebKitTests(unittest.TestCase):
    def test_production_webkit(self):
        class Fixture(http.server.BaseHTTPRequestHandler):
            def handle(self):
                try:
                    super().handle()
                except ConnectionResetError:
                    pass  # Expected when WebKit cancels an active request.
            def do_GET(self):
                if self.path.startswith("/slow"):
                    time.sleep(.6)
                body = b"<html><body><button id='good' onclick=\"document.body.dataset.clicked='good'\">Good</button><button id='evil' onclick=\"document.body.dataset.clicked='evil'\">Evil</button></body></html>"
                self.send_response(200)
                self.send_header("Content-Type","text/html")
                self.send_header("Content-Length",str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass
            def log_message(self,*_):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1",0),Fixture)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                harness = root/"Test.swift"
                harness.write_text(SWIFT)
                binary = root/"browser-test"
                result = subprocess.run(["xcrun","swiftc","-parse-as-library",str(Path(__file__).with_name("ChatBrowser.swift")),str(harness),"-o",str(binary)],capture_output=True,text=True,timeout=120)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                result = subprocess.run([str(binary),f"http://127.0.0.1:{server.server_port}/",str(root/"storage")],capture_output=True,text=True,timeout=45)
                print(result.stdout)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertIn("WEBKIT_INTEGRATION_OK",result.stdout)
        finally:
            server.shutdown(); server.server_close(); thread.join()
