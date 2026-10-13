"""Reported cache counts and privacy-safe API/CLI attribution."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import test_gateway_hub as fixtures


class CacheUsageHTTPTests(unittest.TestCase):
    setUp = fixtures.GatewayHubHTTPTests.setUp
    tearDown = fixtures.GatewayHubHTTPTests.tearDown
    start_gateway = fixtures.GatewayHubHTTPTests.start_gateway
    request = fixtures.GatewayHubHTTPTests.request

    def test_cli_estimate_is_tagged_in_log_and_excluded_from_reported_totals(self):
        from protocol import reported_input_tokens
        self.start_gateway("claude", "claude-opus-5-5", {})
        estimate = {"input_tokens": 425000, "output_tokens": 0, "provider_hub_usage_source": "estimate"}
        self.runtime.record("completed", "claude/claude-opus-5-5", 200, estimate, credential_mode="cli", surface="chat")
        row = json.loads((self.root / "activity.jsonl").read_text().splitlines()[-1])
        self.assertEqual(row["usage"], estimate)
        self.assertIsNone(reported_input_tokens(estimate))
        status = self.runtime.status()
        self.assertEqual(status["input_tokens"], 0)
        self.assertEqual(status["estimated_input_tokens"], 425000)
        self.assertEqual(status["providers"]["claude"]["total_input_tokens"], 0)
        self.assertEqual(status["providers"]["claude"]["estimated_input_tokens"], 425000)
        self.runtime.record("completed", "claude/claude-opus-5-5", 200,
                            {"input_tokens": 12, "output_tokens": 9, "cache_read_input_tokens": 100})
        self.assertEqual(self.runtime.status()["total_input_tokens"], 112)

    def test_messages_completion_records_credential_source_and_surface(self):
        self.start_gateway("claude", "claude-opus-5-5", {})
        body={"model":"claude-fable-5","max_tokens":256,"messages":[{"role":"user","content":"Question"}]}
        for surface in (None,"responses","chat"):
            with self.subTest(surface=surface):
                candidate=copy.deepcopy(body)
                if surface=="chat":
                    candidate.update(model="claude/claude-opus-5-5",_provider_hub_surface="chat",_provider_hub_account="")
                elif surface:candidate["_provider_hub_surface"]=surface
                status,raw,_=self.request(candidate)
                self.assertEqual(status,200,raw)
                rows=[json.loads(line) for line in (self.root/"activity.jsonl").read_text().splitlines()]
                completed=[row for row in rows if row["event"]=="completed"][-1]
                self.assertEqual(completed["credential_mode"],"keychain")
                self.assertEqual(completed["surface"],surface or "messages")
                self.assertNotIn("_provider_hub_surface",fixtures.MockProvider.requests[-1])
                self.assertNotIn(fixtures.PROVIDER_KEY,(self.root/"activity.jsonl").read_text())

    def test_record_accepts_only_known_source_labels_and_retains_reported_counts(self):
        self.start_gateway("claude","claude-opus-5-5",{})
        counts={"input_tokens":2,"cache_read_input_tokens":400000,"cache_creation_input_tokens":1000,
                "output_tokens":800,"output_tokens_details":{"thinking_tokens":300}}
        self.runtime.record("completed","claude/claude-opus-5-5",200,counts,
                            credential_mode="cli",surface="responses")
        row=json.loads((self.root/"activity.jsonl").read_text().splitlines()[-1])
        self.assertEqual(row["usage"],counts)
        self.assertEqual(row["credential_mode"],"cli")
        self.runtime.record("completed","claude/claude-opus-5-5",200,{"input_tokens":2,"output_tokens":8},
                            credential_mode="secret-like-value",surface="unknown-surface")
        row=json.loads((self.root/"activity.jsonl").read_text().splitlines()[-1])
        self.assertNotIn("credential_mode",row)
        self.assertNotIn("surface",row)
        self.assertNotIn("cache_read_input_tokens",row["usage"])


@unittest.skipUnless(sys.platform=="darwin" and shutil.which("xcrun"),"Swift usage test needs macOS")
class CacheUsageSwiftTests(unittest.TestCase):
    def test_missing_cache_counts_overflow_and_read_ratios(self):
        cases=r'''
import Foundation
@main struct Cases {
 static func main() {
  let cached=HubTokenUsage(["input_tokens":2,"cache_read_input_tokens":800,"cache_creation_input_tokens":198,
    "output_tokens":120,"output_tokens_details":["thinking_tokens":80],
    "cache_creation":["ephemeral_5m_input_tokens":198,"ephemeral_1h_input_tokens":0]])
  precondition(cached.totalInput==1000 && cached.cacheReadPercent==80)
  precondition(cached.summary.contains("80.0% of input") && cached.summary.contains("Thinking 80"))
  precondition(cached.summary.contains("5m write 198") && cached.summary.contains("1h write 0"))
  let unknown=HubTokenUsage(["input_tokens":1000,"output_tokens":100])
  let estimate=HubTokenUsage(["input_tokens":425000,"output_tokens":0,"provider_hub_usage_source":"estimate"])
  precondition(estimate.summary.contains("Estimated input") && estimate.output==nil && estimate.cacheReadPercent==nil)
  precondition(estimate.summary.contains("Provider usage unavailable"))
  let aggregate=HubTokenUsage(["input_tokens":12,"output_tokens":9,"estimated_input_tokens":425000])
  precondition(aggregate.summary.contains("excluded from reported totals"))
  precondition(unknown.cacheRead==nil && unknown.cacheReadPercent==nil)
  precondition(unknown.summary.contains("Cache telemetry unavailable") && !unknown.summary.contains("Cache read 0"))
  let zero=HubTokenUsage(["input_tokens":0,"cache_read_input_tokens":0,"cache_creation_input_tokens":0])
  precondition(zero.totalInput==0 && zero.cacheReadPercent==nil)
  let overflow=HubTokenUsage(["input_tokens":Int.max,"cache_read_input_tokens":1,"cache_creation_input_tokens":0])
  precondition(overflow.totalInput==nil && overflow.cacheReadPercent==nil)
  let bad=HubTokenUsage(["input_tokens":-1,"cache_read_input_tokens":true,"cache_creation_input_tokens":"12"])
  precondition(bad.input==nil && bad.cacheRead==nil && bad.cacheWrite==nil)
  print("Reported cache usage passed")
 }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);swift=root/"Cases.swift";swift.write_text(cases)
            binary=root/"cases"
            compiled=subprocess.run(["xcrun","swiftc","-parse-as-library",
                str(Path(__file__).with_name("HubTokenUsage.swift")),str(swift),"-o",str(binary)],
                capture_output=True,text=True,timeout=60)
            self.assertEqual(compiled.returncode,0,compiled.stderr)
            ran=subprocess.run([str(binary)],capture_output=True,text=True,timeout=10)
            self.assertEqual(ran.returncode,0,ran.stderr)
            self.assertIn("cache usage passed",ran.stdout)


if __name__=="__main__":unittest.main()
