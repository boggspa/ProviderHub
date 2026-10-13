#!/usr/bin/env python3
"""Summarize content-free CLI timing JSONL; token counts are not plan billing."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics

TOKENS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def summarize(rows, provider="claude"):
    rows = [row for row in rows if row.get("provider") == provider]
    counts = Counter(row.get("reuse", "unknown") for row in rows)
    result = {"provider": provider, "requests": len(rows), "reuse": dict(counts),
              "process_starts": sum(row.get("process_starts", 0) for row in rows),
              "usage_requests": sum(row.get("usage_reported") is True for row in rows),
              "token_totals": {}, "token_reporting_requests": {}, "latency_ms": {}}
    for name in TOKENS:
        values = [row[name] for row in rows if type(row.get(name)) is int and row[name] >= 0]
        result["token_totals"][name] = sum(values) if values else None
        result["token_reporting_requests"][name] = len(values)
    cache_rows = [row for row in rows if all(type(row.get(k)) is int and row[k] >= 0
                                           for k in TOKENS if k != "output_tokens")]
    reads = sum(row["cache_read_input_tokens"] for row in cache_rows)
    total = sum(sum(row[k] for k in TOKENS if k != "output_tokens") for row in cache_rows)
    result["cache_read_fraction"] = round(reads / total, 4) if total else None
    result["cache_fraction_requests"] = len(cache_rows)
    for name in ("gateway_queue_ms", "first_model_event_ms", "first_visible_text_ms", "tool_ready_ms", "response_complete_ms"):
        values = sorted(row[name] for row in rows if type(row.get(name)) in (int, float)
                        and math.isfinite(row[name]) and row[name] >= 0)
        if values:
            result["latency_ms"][name] = {"median": statistics.median(values),
                "p95": values[max(0, math.ceil(len(values) * .95) - 1)], "samples": len(values)}
    result["note"] = "Reported tokens and cache reuse; not subscription usage, billing, or tokens per second."
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--provider", default="claude")
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.path.read_text().splitlines() if line.strip()]
    print(json.dumps(summarize(rows, args.provider), indent=2))
