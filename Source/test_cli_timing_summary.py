import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("timing_summary", Path(__file__).parents[1] / "scripts/summarize_cli_timings.py")
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)


class SummaryTests(unittest.TestCase):
    def test_cache_reads_remain_distinct_and_missing_usage_is_not_zero(self):
        rows = [{"provider": "claude", "reuse": "cold", "process_starts": 1,
                 "input_tokens": 100, "cache_read_input_tokens": 800, "cache_creation_input_tokens": 100,
                 "output_tokens": 30, "usage_reported": True, "first_model_event_ms": 1000,
                 "prompt": "MUST NOT APPEAR"},
                {"provider": "claude", "reuse": "resumed", "first_model_event_ms": 200},
                {"provider": "codex", "input_tokens": 900000}]
        result = summary.summarize(rows)
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["usage_requests"], 1)
        self.assertEqual(result["token_totals"]["input_tokens"], 100)
        self.assertEqual(result["cache_read_fraction"], .8)
        self.assertEqual(result["latency_ms"]["first_model_event_ms"]["median"], 600)
        self.assertNotIn("MUST NOT APPEAR", str(result))
        self.assertIsNone(summary.summarize([])["token_totals"]["output_tokens"])
