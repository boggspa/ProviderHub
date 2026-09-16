"""Codex power-slider accent helper: offline, against a fake browser only."""
import io
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from bridge_core import SLOTS
import codex_accent
from codex_accent import (ACCENT_PROPERTY, PROPERTY, THEME_ATTRIBUTE, ULTRA_ACCENT_PROPERTY, ULTRA_MARK, ULTRA_PROPERTY, AccentBridge, usage_banner_css, usage_banner_selector, HUE_PROPERTY, hue_map,
                          DevToolsPipe, accent_map, already_running, bridge_command, child_environment, executable_path, launch, run,
                          shimmer_css, ultra_accents, ultra_css, ultra_map, watcher_script)
from hub_config import defaults


def fixture():
    settings = defaults(SLOTS, "mistral-medium-latest", 11438)
    settings["codex_model"] = "kimi/kimi-for-coding"
    inventory = {"models": [
        {"id": "kimi/kimi-for-coding", "display_name": "Kimi for Coding", "provider_id": "kimi",
         "context": 256000, "tools": True, "vision": False, "reasoning": True, "effort_modes": ["low", "high"],
         "presentation": {"displayProvider": "Kimi", "hueKey": "kimi", "accent": "#0073e6"}},
        {"id": "ollama/qwen3:cloud", "display_name": "Qwen 3", "provider_id": "ollama",
         "context": 131072, "tools": True, "vision": False,
         "presentation": {"displayProvider": "Alibaba", "hueKey": "alibaba", "accent": "#8C52EF"}},
        {"id": "grok/grok-4.6", "display_name": "Grok 4.6", "provider_id": "grok",
         "context": 500000, "tools": True, "vision": True,
         "presentation": {"displayProvider": "Grok", "accent": "not-a-colour"}},
    ]}
    return settings, inventory


def demo_bundle(directory: Path, body: str = "#!/bin/sh\nexit 0\n", environment: dict | None = None) -> Path:
    app = directory / "Demo.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    info = {"CFBundleExecutable": "Demo"}
    if environment is not None:
        info["LSEnvironment"] = environment
    with (app / "Contents" / "Info.plist").open("wb") as stream:
        plistlib.dump(info, stream)
    binary = app / "Contents" / "MacOS" / "Demo"
    binary.write_text(body)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return app


class AccentMapTests(unittest.TestCase):
    def test_labels_map_to_presentation_accents_and_bad_colours_are_skipped(self):
        settings, inventory = fixture()
        accents = accent_map(settings, inventory)
        self.assertEqual(accents, {"Kimi for Coding": "#0073E6", "Qwen 3": "#8C52EF"})

    def test_watcher_embeds_lowercased_labels_the_property_and_the_origin_guard(self):
        script = watcher_script({"Kimi for Coding": "#0073E6", "Qwen 3": "#8C52EF"})
        self.assertIn('"kimi for coding": "#0073E6"', script)
        self.assertIn('"qwen 3": "#8C52EF"', script)
        self.assertIn(PROPERTY, script)
        self.assertIn("window.__providerHubAccent", script)
        self.assertIn('[data-explicit-model="true"]', script)
        self.assertIn("window !== window.top", script)
        self.assertIn(r"/^app:\/\/-\//", script)
        self.assertIn("observer.observe(document, ", script)  # never the root element: absent at document start
        self.assertIn("[data-codex-intelligence-trigger]", script)  # the composer pill
        self.assertIn("[data-reasoning-effort]", script)  # its stacked effort layers
        self.assertIn('effort === "ultra"', script)  # Ultra takes the model's Ultra hue and the sweep
        self.assertIn('"kimi for coding": {"dark": "#0A82FF", "light": "#0065CC"}', script)
        self.assertIn('const ULTRA_PROPERTY = "--color-chart-purple";', script)  # the app's own token for Ultra
        self.assertIn(f'const ULTRA_ACCENT_PROPERTY = "{ULTRA_ACCENT_PROPERTY}";', script)
        self.assertIn(f'const ULTRA_MARK = "{ULTRA_MARK}";', script)
        self.assertIn('[data-reasoning-effort="ultra"]', script)  # the pill's Ultra layer
        self.assertIn('[data-maximum="true"]', script)  # the popover's title at Ultra (and Max)
        self.assertIn("[data-model-picker-power-slider]", script)  # the slider's fill reads the same token
        self.assertNotIn("__HUB_", script)

    def test_usage_banner_rule_is_opt_in_and_keys_on_the_gauge_icon(self):
        selector = usage_banner_selector()
        self.assertEqual(selector, 'aside:has(svg path[d^="M10.8343 12.0693"])')
        self.assertEqual(usage_banner_css(), selector + "{display:none}")
        plain = watcher_script({"Kimi for Coding": "#0073E6"})
        self.assertNotIn("aside:has(", plain)
        self.assertIn('const USAGE_SELECTOR = "";', plain)
        hiding = watcher_script({"Kimi for Coding": "#0073E6"}, hide_usage_banner=True)
        self.assertIn(json.dumps(usage_banner_css())[1:-1], hiding)  # inside the adopted stylesheet
        self.assertIn(f"const USAGE_SELECTOR = {json.dumps(selector)};", hiding)
        self.assertIn("document.querySelectorAll(USAGE_SELECTOR).length", hiding)  # the status reports matches
        self.assertNotIn("__HUB_", hiding)

    def test_shimmer_gray_takes_the_accent_hue_and_the_sweep_stays_the_apps(self):
        css = shimmer_css()
        self.assertEqual(css, f':where([{THEME_ATTRIBUTE}] :is(.loading-shimmer-pure-text,.loading-shimmer))'
                              f'{{--loading-shimmer-foreground:oklch(from var(--color-codex-description) l 0.07 var({HUE_PROPERTY}))}}')
        self.assertNotIn("--loading-shimmer-highlight", css)  # the sweep is the app's own again
        hues = hue_map({"kimi for coding": "#0073E6", "antigravity": "#308713", "gray": "#808080", "white": "#FFFFFF"})
        self.assertEqual(set(hues), {"kimi for coding", "antigravity"})  # grays have no hue to lend
        self.assertAlmostEqual(hues["kimi for coding"], 256.0, delta=0.2)
        self.assertAlmostEqual(hues["antigravity"], 139.7, delta=0.2)
        script = watcher_script({"Kimi for Coding": "#0073E6", "Plain": "#808080"})
        self.assertIn(f'const HUES = {json.dumps(hues["kimi for coding"] and {"kimi for coding": hues["kimi for coding"]})};', script)
        self.assertIn(f'const HUE_PROPERTY = "{HUE_PROPERTY}";', script)
        self.assertIn("--loading-shimmer-foreground", script)
        self.assertNotIn("--loading-shimmer-highlight", script)
        self.assertIn("adoptedStyleSheets", script)
        self.assertIn(f'const THEME = "{THEME_ATTRIBUTE}";', script)
        self.assertIn(f'const ACCENT_PROPERTY = "{ACCENT_PROPERTY}";', script)
        self.assertIn("applyShimmer(found.accent, found.theme, found.hue)", script)
        self.assertNotIn("__HUB_", script)


class UltraTests(unittest.TestCase):
    def test_ultra_hue_moves_lightness_away_from_the_surface_and_takes_all_the_chroma_the_gamut_allows(self):
        for colour in ("#0073E6", "#D44404", "#308713", "#976C52", "#4E6AEE", "#5E7C6F", "#EA0C2D"):
            base_l, base_c, base_h = codex_accent._srgb_to_oklch(colour)
            variants = ultra_accents(colour)
            self.assertEqual(set(variants), {"dark", "light"})
            for theme, sign in (("dark", 1), ("light", -1)):
                self.assertRegex(variants[theme], r"^#[0-9A-F]{6}$")
                level, chroma, hue = codex_accent._srgb_to_oklch(variants[theme])
                self.assertGreater(sign * (level - base_l), 0.03, (colour, theme, variants))
                self.assertAlmostEqual(hue, base_h, delta=0.05, msg=(colour, theme, variants))
                # Chroma reaches the target or the gamut edge at the new lightness, whichever comes first;
                # an accent already at the edge may end a little under its base chroma once moved.
                self.assertGreaterEqual(chroma, 0.9 * base_c, (colour, theme, variants))
                at_target = chroma >= 1.5 * base_c - 0.005
                at_edge = not codex_accent._in_gamut(codex_accent._oklch_to_srgb(level, chroma * 1.03, hue))
                self.assertTrue(at_target or at_edge, (colour, theme, variants, chroma / base_c))
        self.assertEqual(ultra_accents("#0073E6"), {"dark": "#0A82FF", "light": "#0065CC"})
        self.assertEqual(ultra_accents("#D44404"), {"dark": "#ED4C00", "light": "#BD3B00"})
        # A grey has no chroma to raise: it only moves away from the surface.
        self.assertEqual(ultra_accents("#757575"), {"dark": "#848484", "light": "#676767"})
        self.assertEqual(ultra_map({"kimi for coding": "#0073E6"}), {"kimi for coding": {"dark": "#0A82FF", "light": "#0065CC"}})

    def test_ultra_css_sweeps_the_marked_word_and_never_loses_it(self):
        css = ultra_css()
        self.assertIn("@keyframes provider-hub-ultra-sweep{from{background-position:100% 0}to{background-position:0% 0}}", css)
        self.assertIn(f':where([{ULTRA_MARK}="1"]){{background-image:linear-gradient(100deg,var({ULTRA_ACCENT_PROPERTY},currentColor) 0%', css)
        self.assertIn(f"color-mix(in srgb,var({ULTRA_ACCENT_PROPERTY},currentColor) 55%,#fff) 50%", css)
        self.assertIn("background-size:240% 100%;-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;", css)
        self.assertIn("animation:provider-hub-ultra-sweep 3.2s linear infinite}", css)
        self.assertNotRegex(css, r"[;{]color:transparent")  # only the fill: currentColor keeps drawing whatever uses it
        self.assertIn(f':where([{THEME_ATTRIBUTE}="light"] [{ULTRA_MARK}="1"])', css)
        self.assertIn(f"@media (prefers-reduced-motion:reduce){{:where([{ULTRA_MARK}=\"1\"]){{animation:none;background-image:none;-webkit-text-fill-color:var({ULTRA_ACCENT_PROPERTY},currentColor)}}}}", css)
        self.assertEqual(ULTRA_PROPERTY, "--color-chart-purple")
        script = watcher_script({"Kimi for Coding": "#0073E6"})
        self.assertIn("provider-hub-ultra-sweep", script)
        self.assertIn(json.dumps(shimmer_css() + ultra_css()), script)  # both stylesheets travel together


class EnvironmentTests(unittest.TestCase):
    def test_child_environment_is_an_allowlist_plus_the_bundle_lsenvironment(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
                "HOME": "/Users/demo", "USER": "demo", "PATH": "/opt/homebrew/bin:/usr/bin",
                "OPENAI_API_KEY": "sk-secret", "NODE_OPTIONS": "--require evil", "ELECTRON_RUN_AS_NODE": "1",
                "MISTRAL_BRIDGE_TOKEN": "t", "CODEX_HOME": "/elsewhere", "PYTHONPATH": "/x", "TMPDIR": "/tmp/demo/"}):
            plain = child_environment()
            self.assertEqual(plain["HOME"], "/Users/demo")
            self.assertEqual(plain["TMPDIR"], "/tmp/demo/")
            self.assertEqual(plain["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
            for key in ("OPENAI_API_KEY", "NODE_OPTIONS", "ELECTRON_RUN_AS_NODE", "MISTRAL_BRIDGE_TOKEN", "CODEX_HOME", "PYTHONPATH"):
                self.assertNotIn(key, plain)
            app = demo_bundle(Path(directory), environment={"MallocNanoZone": "0"})
            self.assertEqual(child_environment(app)["MallocNanoZone"], "0")
            self.assertNotIn("MallocNanoZone", child_environment(Path(directory) / "Missing.app"))


class FakeTransport(DevToolsPipe):
    """Records sends and lets a test feed replies without descriptors."""

    def __init__(self):
        super().__init__(-1, -1)
        self.sent = []

    def send(self, method, params=None, session_id=None):
        self.next_id += 1
        self.sent.append({"id": self.next_id, "method": method, "params": params or {}, "sessionId": session_id})
        return self.next_id


class BridgeTests(unittest.TestCase):
    def test_pipe_frames_nul_delimited_json_and_reports_eof(self):
        command_read, command_write = os.pipe()
        event_read, event_write = os.pipe()
        try:
            pipe = DevToolsPipe(event_read, command_write)
            identifier = pipe.send("Target.getTargets", session_id="abc")
            raw = os.read(command_read, 4096)
            self.assertTrue(raw.endswith(b"\0"))
            self.assertEqual(json.loads(raw[:-1]), {"id": identifier, "method": "Target.getTargets", "params": {}, "sessionId": "abc"})
            os.write(event_write, b'{"id": 1, "result": {"a": 1}}\0{"method": "Target.targetCreated", "params": {}}\0{"partial":')
            self.assertEqual([m.get("id", m.get("method")) for m in pipe.poll(1.0)], [1, "Target.targetCreated"])
            os.write(event_write, b' 1}\0not json\0')
            self.assertEqual(pipe.poll(1.0), [{"partial": 1}])
            self.assertEqual(pipe.poll(0.05), [])
            os.close(event_write)
            with self.assertRaises(EOFError):
                pipe.poll(1.0)
        finally:
            for fd in (command_read, command_write, event_read):
                os.close(fd)

    def test_bridge_auto_attaches_to_app_pages_only_and_injects_once_per_session(self):
        transport = FakeTransport()
        events = []
        bridge = AccentBridge(transport, "SCRIPT", events.append)
        bridge.start()
        self.assertEqual([m["method"] for m in transport.sent], ["Target.setAutoAttach"])
        self.assertEqual(transport.sent[0]["params"], {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True, "filter": [{"type": "page"}]})
        bridge.handle({"id": transport.sent[0]["id"], "result": {}})
        # The app's own window gets the watcher.
        bridge.handle({"method": "Target.attachedToTarget", "params": {"sessionId": "S1", "targetInfo": {"targetId": "page-1", "type": "page", "url": "app://-/index.html?initialRoute=/"}}})
        injected = [m for m in transport.sent if m["sessionId"] == "S1"]
        self.assertEqual([m["method"] for m in injected], ["Page.enable", "Page.addScriptToEvaluateOnNewDocument", "Runtime.evaluate"])
        self.assertEqual(injected[1]["params"], {"source": "SCRIPT", "runImmediately": True})
        bridge.handle({"id": injected[2]["id"], "result": {"result": {"type": "object", "value": {"skipped": "origin"}}}})
        self.assertEqual(events[-1], {"event": "injected", "session": "S1", "result": {"skipped": "origin"}})
        # A repeated attach notification for the same session is a no-op.
        bridge.handle({"method": "Target.attachedToTarget", "params": {"sessionId": "S1", "targetInfo": {"targetId": "page-1", "type": "page", "url": "app://-/index.html"}}})
        self.assertEqual(sum(1 for m in transport.sent if m["method"] == "Page.addScriptToEvaluateOnNewDocument"), 1)
        # Once the document is there the script is evaluated again (it is idempotent).
        bridge.handle({"method": "Page.loadEventFired", "sessionId": "S1", "params": {"timestamp": 1.0}})
        again = [m for m in transport.sent if m["sessionId"] == "S1" and m["method"] == "Runtime.evaluate"]
        self.assertEqual(len(again), 2)
        bridge.handle({"id": again[1]["id"], "result": {"result": {"type": "object", "value": {"installed": True, "accents": 2, "ready": "complete"}}}})
        self.assertEqual(events[-1]["result"], {"installed": True, "accents": 2, "ready": "complete"})
        bridge.handle({"method": "Page.loadEventFired", "sessionId": "unknown", "params": {}})  # not ours: ignored
        self.assertEqual(len([m for m in transport.sent if m["method"] == "Runtime.evaluate"]), 2)
        # A browser-panel window on an outside site and a worker are detached again.
        bridge.handle({"method": "Target.attachedToTarget", "params": {"sessionId": "S2", "targetInfo": {"targetId": "page-2", "type": "page", "url": "https://example.com/"}}})
        bridge.handle({"method": "Target.attachedToTarget", "params": {"sessionId": "S3", "targetInfo": {"targetId": "w", "type": "service_worker", "url": "app://-/sw.js"}}})
        detached = [m for m in transport.sent if m["method"] == "Target.detachFromTarget"]
        self.assertEqual([m["params"]["sessionId"] for m in detached], ["S2", "S3"])
        self.assertEqual(bridge.injected, {"S1"})
        # A fresh blank window is accepted (the document arrives later) and an evaluate error is reported.
        bridge.handle({"method": "Target.attachedToTarget", "params": {"sessionId": "S4", "targetInfo": {"targetId": "page-4", "type": "page", "url": "about:blank"}}})
        evaluate = [m for m in transport.sent if m["sessionId"] == "S4" and m["method"] == "Runtime.evaluate"][0]
        bridge.handle({"id": evaluate["id"], "result": {"exceptionDetails": {
            "text": "Uncaught", "lineNumber": 60, "columnNumber": 14,
            "exception": {"type": "object", "description": "TypeError: Failed to execute 'observe' on 'MutationObserver': parameter 1 is not of type 'Node'.\n    at <anonymous>:61:14"}}}})
        self.assertEqual(events[-1]["stage"], "evaluate")
        self.assertTrue(events[-1]["message"].startswith("TypeError: Failed to execute 'observe'"))
        self.assertEqual((events[-1]["line"], events[-1]["column"]), (60, 14))
        bridge.handle({"method": "Target.detachedFromTarget", "params": {"sessionId": "S4"}})
        self.assertEqual(bridge.injected, {"S1"})
        bridge.handle({"id": 999, "result": {}})  # unknown ids are ignored

    def test_bridge_falls_back_to_an_unfiltered_auto_attach(self):
        transport = FakeTransport()
        events = []
        bridge = AccentBridge(transport, "SCRIPT", events.append)
        bridge.start()
        bridge.handle({"id": transport.sent[0]["id"], "error": {"code": -32602, "message": "Invalid parameters"}})
        self.assertEqual(transport.sent[1]["method"], "Target.setAutoAttach")
        self.assertNotIn("filter", transport.sent[1]["params"])
        bridge.handle({"id": transport.sent[1]["id"], "error": {"code": -32601, "message": "no"}})
        self.assertEqual(events, [{"event": "error", "stage": "autoattach-unfiltered", "message": "no"}])


FAKE_BROWSER = r'''
import json, os, sys
buffer = b""
def send(message):
    os.write(4, json.dumps(message).encode() + b"\0")
seen = []
while True:
    chunk = os.read(3, 65536)
    if not chunk:
        sys.exit(3)
    buffer += chunk
    while b"\0" in buffer:
        raw, buffer = buffer.split(b"\0", 1)
        message = json.loads(raw)
        seen.append(message["method"])
        if message["method"] == "Target.setAutoAttach":
            send({"id": message["id"], "result": {}})
            send({"method": "Target.attachedToTarget", "params": {"sessionId": "S1", "targetInfo": {"targetId": "T1", "type": "page", "url": "app://-/index.html"}}})
        elif message["method"] == "Runtime.evaluate":
            with open(sys.argv[1], "w") as stream:
                json.dump({"argv": sys.argv[2:], "script": message["params"]["expression"], "seen": seen,
                           "env": {k: v for k, v in os.environ.items() if k in ("OPENAI_API_KEY", "HOME", "DEMO_LS")},
                           "stdout_is_null": os.fstat(1).st_rdev == os.stat(os.devnull).st_rdev}, stream)
            send({"id": message["id"], "result": {"result": {"type": "undefined"}}})
            sys.exit(7)
        else:
            send({"id": message["id"], "result": {}})
'''


class LaunchTests(unittest.TestCase):
    def test_run_launches_over_a_pipe_injects_and_returns_the_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "fake_browser.py").write_text(FAKE_BROWSER)
            marker = base / "seen.json"
            wrapper = base / "browser.sh"
            wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{base / "fake_browser.py"}" "{marker}" "$@"\n')
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
            events = []
            environment = dict(child_environment(), OPENAI_API_KEY="leak-check", DEMO_LS="1")
            status = run(wrapper, watcher_script({"Kimi for Coding": "#0073E6"}), emit=events.append,
                         environment=environment, poll_interval=0.05)
            self.assertEqual(status, 7)
            self.assertEqual([event["event"] for event in events], ["launched", "injected", "exited"])
            record = json.loads(marker.read_text())
            self.assertEqual(record["argv"], ["--remote-debugging-pipe"])
            self.assertIn('"kimi for coding": "#0073E6"', record["script"])
            self.assertEqual(record["seen"][:2], ["Target.setAutoAttach", "Page.enable"])
            self.assertEqual(record["env"], {"OPENAI_API_KEY": "leak-check", "HOME": os.environ["HOME"], "DEMO_LS": "1"})
            self.assertTrue(record["stdout_is_null"])

    def test_run_survives_a_failing_status_sink(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "exit.sh"
            script.write_text("#!/bin/sh\nexit 5\n")
            script.chmod(script.stat().st_mode | stat.S_IXUSR)

            def broken(event):
                raise BrokenPipeError("stdout gone")

            self.assertEqual(run(script, "SCRIPT", emit=broken, environment=child_environment(), poll_interval=0.05), 5)

    def test_launch_closes_its_copies_of_the_child_ends(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "exit.sh"
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            pid, pipe = launch(script, environment=child_environment())
            self.assertEqual(os.waitpid(pid, 0)[0], pid)
            with self.assertRaises(EOFError):
                pipe.poll(2.0)
            pipe.close()

    def test_launch_hands_the_child_fds_3_and_4_even_when_the_pipes_land_there(self):
        # A fresh process gets 3 and 4 from os.pipe(); dup2(3, 3) would keep
        # close-on-exec set and the child would see them closed.
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fds.sh"
            marker = Path(directory) / "fds.txt"
            script.write_text(f'#!/bin/sh\nprintf "%s" "$1" >&4\nread line <&3\nprintf "%s|%s" "$line" "$1" > "{marker}"\n')
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            pid, pipe = launch(script, environment=child_environment())
            self.assertGreaterEqual(pipe.read_fd, 10)
            self.assertGreaterEqual(pipe.write_fd, 10)
            os.write(pipe.write_fd, b"hello\n")
            self.assertEqual(os.waitpid(pid, 0)[0], pid)
            self.assertEqual(marker.read_text(), "hello|--remote-debugging-pipe")
            self.assertEqual(os.read(pipe.read_fd, 100), b"--remote-debugging-pipe")
            pipe.close()

    def test_launch_puts_the_app_in_its_own_session(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "sid.sh"
            marker = Path(directory) / "sid.txt"
            script.write_text(f'#!/bin/sh\n/bin/ps -o pgid= -p $$ > "{marker}"\n')
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            pid, pipe = launch(script, environment=child_environment())
            self.assertEqual(os.waitpid(pid, 0)[0], pid)
            pipe.close()
            self.assertEqual(int(marker.read_text().strip()), pid)
            self.assertNotEqual(os.getpgrp(), pid)

    def test_already_running_matches_the_binary_command_line(self):
        child = subprocess.Popen(["/bin/sleep", "30"])
        try:
            self.assertTrue(already_running(Path("/bin/sleep")))
        finally:
            child.terminate()
            child.wait()
        self.assertFalse(already_running(Path("/bin/slee")))  # a prefix of a running command is no match
        self.assertFalse(already_running(Path("/nonexistent/Provider Hub Test.app/Contents/MacOS/Nope")))

    def test_bridge_command_refuses_a_running_app_logs_and_keeps_going_without_stdout(self):
        settings, inventory = fixture()
        with tempfile.TemporaryDirectory() as directory:
            app = demo_bundle(Path(directory))
            events = []
            log = Path(directory) / "codex-accent.log"
            with mock.patch.object(codex_accent, "already_running", lambda path: True):
                self.assertEqual(bridge_command(str(app), settings, inventory, emit=events.append, log_path=log), 2)
                self.assertIn("already running", log.read_text())
                self.assertEqual(events, [{"event": "error", "stage": "launch", "message": "The app is already running; quit it first."}])
                closed = io.StringIO()
                closed.close()
                with mock.patch.object(sys, "stdout", closed):
                    self.assertEqual(bridge_command(str(app), settings, inventory, log_path=log), 2)
            events.clear()
            with mock.patch.object(codex_accent, "already_running", lambda path: False):
                self.assertEqual(bridge_command(str(app), settings, inventory, emit=events.append, log_path=log, poll_interval=0.05), 0)
            self.assertEqual(events[0], {"event": "accents", "count": 2, "usage_banner": "shown"})
            self.assertEqual([event["event"] for event in events[1:]], ["launched", "exited"])
            self.assertEqual(len(log.read_text().splitlines()), 3)
            events.clear()
            scripts = []
            with mock.patch.object(codex_accent, "already_running", lambda path: False), \
                    mock.patch.object(codex_accent, "run", lambda binary, script, **options: scripts.append(script) or 0):
                hiding = dict(settings, codex_hide_usage_banner=True)
                self.assertEqual(bridge_command(str(app), hiding, inventory, emit=events.append, log_path=log), 0)
            self.assertEqual(events, [{"event": "accents", "count": 2, "usage_banner": "hidden"}])
            self.assertIn("aside:has(", scripts[0])

    def test_executable_path_reads_the_bundle_plist(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Demo.app"
            (app / "Contents" / "MacOS").mkdir(parents=True)
            with self.assertRaises(ValueError):
                executable_path(app)
            with (app / "Contents" / "Info.plist").open("wb") as stream:
                plistlib.dump({"CFBundleExecutable": "Demo"}, stream)
            with self.assertRaises(ValueError):
                executable_path(app)
            binary = app / "Contents" / "MacOS" / "Demo"
            binary.write_text("#!/bin/sh\n")
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            self.assertEqual(executable_path(app), binary)


if __name__ == "__main__":
    unittest.main()
