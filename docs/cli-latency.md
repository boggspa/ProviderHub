# CLI response latency and continuity

The CLI routes deliver a validated response before process cleanup. A bounded
cleanup queue retains prompt, schema, image and diagnostics files until the child
exits. Cancellation is checked while waiting for stdio traffic and admission.
Each active request owns its CLI process; requests using the same provider/model
do not share an active reader or a native thread. The gateway's existing global
eight-request admission limit still applies.

## Provider behavior

| Route | Response boundary | Persistence |
| --- | --- | --- |
| Codex | Text streams immediately; native host calls remain validated | Initialized app-server processes are reused. A pending native call retains its thread and receives the real host result on the next matching request. |
| Muse Code | First complete, validated structured response on the Messages surface; terminal completion on other requests | Fresh exec process per request. The four-step allowance remains; later speculative structured output is not consumed. |
| Claude Code | Successful terminal result, without waiting for stdout EOF | Fresh process per request. |
| Grok | Successful terminal event or validated native host handoff | Fresh process per request. |
| AntiGravity | Successful result or confirmed pre-execution denial receipt | Fresh process per request. The denial hook and private files remain until child exit. |
| Mistral Vibe credentials | Existing streaming Mistral API route | This hub connection does not spawn Vibe CLI processes. |

Codex retains up to eight idle processes. Ordinary idle leases expire after two
minutes; pending host calls expire after ten minutes. Dead, cancelled, invalid or
expired sessions are retired. Changed instructions, tools, effort or history
cannot silently resume a stale native thread: the supplied host transcript is
used to create a fresh thread. The bridge generates unique host call IDs and
checks the full history prefix and offered call before answering a pending RPC.
New user text/images accompanying a host result are sent through `turn/steer`
before the result unblocks generation. Failed steering is reported rather than
dropped. Process reuse after a completed task starts a fresh native thread.

The installed Muse MSP schema exposes session/start and turn/steer, but the
published session configuration inspected here only exposes MCP configuration;
the exec structured-response contract has not been verified over persistent MSP.
Claude and AGY advertise stream-json input and session continuation; Grok
advertises resume/session controls. These are candidates for a later persistent
adapter, not a basis for sharing one active process among unrelated tasks.

## Images and app compaction

CLI planning keeps the newest contiguous image history within 20 images and
32 MiB. Older images become explicit placeholders while accompanying text,
tool IDs, tool results and user corrections stay in order. Image pixels are not
resized or described by the trimming code. Individual invalid/oversized images
still receive normal validation errors.

`POST /v1/responses/compact` summarizes completed older conversation items through
the selected route with no offered tools. It retains recent complete tool cycles,
pending calls and ordinary recent corrections. Oversized completed messages and
tool results can be summarized even in short histories. The opaque summary is
authenticated to the model/connection and accepted on the next `/v1/responses`
request. A failed summary produces no replacement history. This is a text summary
provided by the selected model, not OpenAI's native compressed reasoning state.
Histories larger than the summary input budget are processed in bounded segments
and then reduced into one summary. Every segment must succeed before replacement
history is returned. Cancelling compaction closes its active request and prevents
later segments from starting.

## AntiGravity context transport and finish responses

AGY 1.2.7 clips its initial model input near 192,000 UTF-8 bytes. A large
desktop tool manifest and harness preamble can exceed that before any user turn
is included. The adapter now checks the fully framed prompt, including image
references and response instructions, against a 160,000-byte budget.

Oversized system context is moved to private files first, followed by older
turns if necessary. Recent requests, steers and actual tool results remain
inline whenever they fit; an oversized individual turn is also preserved in
full. Nothing is summarized or discarded. Context parts contain at most 24,000
bytes and 600 lines, below AGY's native view_file limits. Only those exact files
and the existing image copies receive native read permission. Context files
have mode 0600 inside the private per-turn directory and use the same
process-exit cleanup as images.

Every context part must be successfully read before the adapter releases an
answer or a host action. Missing or failed reads produce an explicit context
error instead of silently answering from a truncated transcript. The extra
native reads add work only on oversized turns; AGY remains stateless.

The native finish instruction names its fields directly: answer/commentary in
text, host requests in tool_calls, and JSON-encoded inputs only in arguments.
Recovery of a double-wrapped response requires a witnessed failed finish call
with missing tool_calls, followed by a successful result with exactly the same
nested text and an empty outer call list. The inner reply passes the existing
host-call validation before release. Without that evidence, JSON answers stay
literal, even when they resemble the response schema.

Live checks on 2026-09-20 recovered the beginning and ending markers of a
252,178-byte preamble and the latest steer through 11 private reads (39.72 s
total). A separate deliberate schema-error/repair probe recovered the intended
answer. A second probe used the host protocol planner with a 247,597-byte
preamble, two prior tool results and a latest steer. It returned exactly one
validated host call containing all five expected markers (52.36 s). These are
correctness probes, not comparative latency measurements.

Claude Code's partial-message stream also emits one-block assistant snapshots
with message-local index 0, even when the streamed text is block 1 after
thinking. The adapter maps those snapshots to the active stream block, avoiding
the duplicated commentary at each host handoff while retaining missing suffixes
and genuinely separate text blocks.

## Timing and benchmark

Set `PROVIDER_HUB_CLI_TIMING_LOG` to a private JSONL destination to record admission,
startup, initialization where applicable, first model event, first visible text,
validated tool readiness, response completion and cleanup. Records contain timing
metadata and the CLI PID, not prompts, tool arguments/results or credentials.
`lease_release_complete_ms` describes returning a pooled Codex process; it must
not be interpreted as process shutdown. Fields absent on a warm request were not
performed for that request.

Run the synthetic echo benchmark with the same model, effort and call count:

```sh
uv run python scripts/benchmark_cli_handoffs.py --provider codex --model gpt-6-astra --effort low --calls 3 --repeats 2
uv run python scripts/benchmark_cli_handoffs.py --provider codex --model gpt-6-astra --effort low --calls 3 --repeats 2 --fresh
```

Measured locally on 2026-09-20 with those settings:

| Measurement | Persistent | Fresh process every leg |
| --- | --- | --- |
| Setup until model-ready, resumed legs | 1.3–3.0 ms | 501–896 ms |
| Process IDs across two four-leg trials | One PID | Eight PIDs |
| Tool/result/steer cycles | Both three-call trials completed | Both three-call trials completed |
| Response-to-lease-release or teardown | Under 0.1 ms lease release | About 9–18 ms shutdown |

Initial setup was 586 ms cold and 517 ms with an initialized process and new
thread. Model generation varied between requests; these two short trials establish
the setup reduction, not a general end-to-end speedup percentage. Both comparison
arms use the new streaming and cleanup behavior. An earlier unsteered trial ended
early, so the benchmark now supplies the next requested step alongside each real
echo result and checks every step. It never offers filesystem or network tools.

A live Muse Spark 1.3 / low trial also completed all three echo calls and the final
answer. Responses completed at 13.38 s, 3.66 s, 2.35 s and 22.79 s; process cleanup
finished another 3.03–3.05 s later on every leg. This directly verifies that cleanup
no longer delays the host handoff. It does not isolate how much model time the
early structured-object boundary saved. Muse still used a separate PID per leg.

References: [Codex app-server](https://learn.chatgpt.com/docs/app-server),
[active-turn steering](https://learn.chatgpt.com/docs/app-server#steer-an-active-turn),
[Responses compaction](https://developers.openai.com/api/docs/guides/compaction).
