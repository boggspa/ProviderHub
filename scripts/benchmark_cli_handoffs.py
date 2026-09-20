#!/usr/bin/env python3
"""Measure real CLI handoffs; uses vendor login and synthetic echo tools only.

Run from the repository with uv's Python 3.13. No file/network tools are offered
to the model. Measurements contain timings and process IDs, never conversation
content. --fresh disables Codex persistence for a controlled comparison.
"""
from __future__ import annotations

import argparse
from functools import partial
import json
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Source'))
import cli_routes


def exercise(provider, model, effort, calls, timeout, repeat, *, fresh=False):
    tools = [{"name": "benchmark_echo", "description": "Echo one step number from the host.",
              "input_schema": {"type": "object", "properties": {"step": {"type": "integer"}},
                               "required": ["step"], "additionalProperties": False}}]
    history = [{"role": "user", "content":
        f"Call benchmark_echo with step 1, then wait for its real result. Repeat with steps 2 through {calls}, "
        "waiting for each result before the next call. After the final result reply DONE. "
        "These calls are a synthetic latency benchmark. Do not use any other tool."}]
    completed = 0
    for leg in range(calls + 2):
        plan = cli_routes.plan_turn(provider, model, {"messages": history,
            "system": "Use only the offered host echo tool, one call per reply.",
            "tools": tools, "stream": True, "output_config": {"effort": effort}}, {}, wanted_output=128)
        wire, records = [], []
        measured = threading.Event()
        stream = cli_routes.run_turn(provider, plan['body'], parse_tool_calls=True, timeout=timeout)
        stream.timing.label(repeat=repeat, leg=leg, effort=effort, requested_mode='fresh' if fresh else 'default')
        def record(value):
            records.append(value)
            measured.set()
        stream.timing.sink = record
        result = cli_routes.relay_cli_turn(stream, wire.append, model=model)
        if not measured.wait(20):
            raise RuntimeError('Cleanup measurement did not complete within 20 seconds')
        print(json.dumps(records[-1], sort_keys=True), flush=True)
        if result['error']:
            raise RuntimeError(result['error'])
        blocks = {}
        for event in wire:
            kind, index = event['type'], event.get('index')
            if kind == 'content_block_start':
                blocks[index] = dict(event['content_block'])
                if blocks[index]['type'] == 'tool_use':
                    blocks[index]['_arguments'] = ''
            elif kind == 'content_block_delta':
                delta = event['delta']
                if delta['type'] == 'text_delta':
                    blocks[index]['text'] += delta['text']
                elif delta['type'] == 'thinking_delta':
                    blocks[index]['thinking'] += delta['thinking']
                elif delta['type'] == 'input_json_delta':
                    blocks[index]['_arguments'] += delta['partial_json']
        content = list(blocks.values())
        host_calls = [block for block in content if block['type'] == 'tool_use']
        if not host_calls:
            if completed != calls:
                raise RuntimeError(f'Model stopped after {completed} of {calls} echo calls')
            return
        if len(host_calls) != 1 or host_calls[0]['name'] != 'benchmark_echo':
            raise RuntimeError('Model did not make exactly one offered echo call')
        call = host_calls[0]
        call['input'] = json.loads(call.pop('_arguments'))
        completed += 1
        if completed > calls or call['input'] != {'step': completed}:
            raise RuntimeError('Unexpected echo step; refusing to continue')
        followup = (f'Now call benchmark_echo with step {completed + 1}.' if completed < calls
                    else 'All requested echo calls have completed. Reply DONE.')
        history.extend([{'role': 'assistant', 'content': content}, {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': call['id'],
             'content': json.dumps({'step': completed, 'verified': True})},
            {'type': 'text', 'text': followup}]}])
    raise RuntimeError('Model exceeded the benchmark turn budget')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=tuple(cli_routes.ADAPTERS), default='codex')
    parser.add_argument('--model', required=True)
    parser.add_argument('--effort', default='low')
    parser.add_argument('--calls', type=int, default=3)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--fresh', action='store_true', help='Codex only: start a new app-server for every leg')
    args = parser.parse_args()
    if not 1 <= args.calls <= 8 or not 1 <= args.repeats <= 10:
        parser.error('calls must be 1..8 and repeats must be 1..10')
    if args.fresh and args.provider != 'codex':
        parser.error('--fresh applies only to Codex; the other adapters already use fresh processes')
    adapter = cli_routes.adapter_for(args.provider)
    try:
        if args.fresh:
            with patch.object(adapter, 'run_turn', partial(adapter.run_turn, spawner=subprocess.Popen)):
                for repeat in range(args.repeats):
                    exercise(args.provider, args.model, args.effort, args.calls, args.timeout, repeat, fresh=True)
        else:
            for repeat in range(args.repeats):
                exercise(args.provider, args.model, args.effort, args.calls, args.timeout, repeat)
    finally:
        pool = getattr(adapter, '_POOL', None)
        if pool is not None:
            pool.close()


if __name__ == '__main__':
    main()
