"""Fail closed when an installed runtime rejects or misreads a catalogue."""
from pathlib import Path
import shlex
import sys
import tempfile
import textwrap
import unittest

from bridge_core import BridgeError
from codex_runtime import qualify_runtime
from test_codex_profile import fixture


class CodexRuntimeQualificationTests(unittest.TestCase):
    def run_fixture(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "fake-codex"
            code = textwrap.dedent('''
                import json, os, sys, tomllib
                from pathlib import Path
                mode = MODE_VALUE
                config = tomllib.loads((Path(os.environ['CODEX_HOME']) / 'config.toml').read_text())
                models = json.loads(Path(config['model_catalog_json']).read_text())['models']
                for line in sys.stdin:
                    request = json.loads(line)
                    with Path(__file__).with_name('methods.txt').open('a') as log:
                        log.write(request['method'] + '\\n')
                    if 'id' not in request:
                        continue
                    result = {}
                    if request['method'] == 'model/list':
                        rows = [{'model':m['slug'], 'displayName':m['display_name'], 'hidden':False,
                                 'supportedReasoningEfforts':[{'reasoningEffort':v['effort']} for v in m['supported_reasoning_levels']],
                                 'serviceTiers':m['service_tiers']} for m in models]
                        if mode == 'fallback': rows = [{'model':'gpt-fallback'}]
                        if mode == 'wrong-controls': rows[0]['supportedReasoningEfforts'] = []
                        result = {'data':rows, 'nextCursor':None}
                    print(json.dumps({'id':request['id'],'result':result}), flush=True)
            ''').replace("MODE_VALUE", repr(mode))
            script = root / "fake-codex.py"
            script.write_text(code)
            binary.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(script)) + ' "$@"\n')
            binary.chmod(0o700)
            settings, inventory = fixture()
            result = qualify_runtime(settings, inventory, binary=binary, timeout=3)
            methods = (root / "methods.txt").read_text().splitlines()
            self.assertEqual(methods, ["initialize", "initialized", "model/list"])
            self.assertTrue(result["accepted"])
            self.assertEqual(result["model_count"], 2)

    def test_matching_catalogue_needs_no_turn_or_inference(self):
        self.run_fixture("valid")

    def test_fallback_catalogue_is_rejected(self):
        with self.assertRaises(BridgeError):
            self.run_fixture("fallback")

    def test_silently_ignored_effort_controls_are_rejected(self):
        with self.assertRaises(BridgeError):
            self.run_fixture("wrong-controls")


if __name__ == "__main__":
    unittest.main()
