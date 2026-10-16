# CLI response latency and continuity

The CLI routes deliver a validated response before process cleanup. A bounded
cleanup queue retains prompt, schema, image and diagnostics files until the child
exits. Cancellation is checked while waiting for stdio traffic and admission.
Each active request owns its CLI process; requests using the same provider/model
do not share an active reader or a native thread. The gateway's existing global
sixteen-request admission limit applies. Provider backoff and adaptive admission
still respond to upstream rate limits.

## Provider behavior

| Route | Response boundary | Persistence |
| --- | --- | --- |
| Codex | Text streams immediately; native host calls remain validated | Initialized app-server processes are reused. A pending native call retains its thread and receives the real host result on the next matching request. |
| Muse Code | First complete, validated structured response on the Messages surface; terminal completion on other requests | Fresh exec process per request. The four-step allowance remains; later speculative structured output is not consumed. |
| Claude Code | Native host handoff or successful terminal result | Lean Chat keeps a scoped streaming conversation across matching tools, user steering and completed contributions on CLI 2.1.290+. Other surfaces retain tool-handoff continuity. Changed history reconstructs portable context. |
| Grok | Successful terminal event or validated native host handoff | Matching tool calls keep the process. Scoped lean Chat follow-ups after a completed turn use that lease's explicit native session ID and send only new input. |
| AntiGravity | Successful result or confirmed pre-execution denial receipt | Scoped lean Chat retains a stream-json process after a successful result when the installed CLI advertises that protocol. Early denied native handoffs still retire the process. |
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
Persistent transports keep one exclusive native conversation per host
chat/member; they never share an active reader between unrelated tasks.

## Stateful CLI context in the lean harness

Shared Team transcript updates are classified from their host-owned source
entries before conversion to portable user-role messages. For a CLI-backed
Claude, Codex or Grok member, a peer-only preview update is appended to a fresh, complete tool-result
boundary. Source IDs, attribution, ordering and error flags are retained, and
the cursor and messages are persisted together through the existing journal.
This closes the separate peer-update replay path left after notebook and
Blackboard projections were made append-only. Six-member regressions exercise
durable publications, deferred rows, explicit peer mentions and three real MCP
bridge handoffs on one scripted CLI process.

Actual user entries (even ones imitating a peer-reference label), mixed user
input, incomplete tool results and non-text references keep ordinary message
boundaries. The native continuation matchers are unchanged. Codex still accepts
real user steering with `turn/steer`; Claude also has scoped native user-frame
continuation. Grok still reconstructs history for new user input interleaved
with an unresolved native tool handoff. API-backed and stateless routes receive the same small previews as
ordinary messages rather than having peer references attached to tool results.
No historical chat is rewritten, and pending processes from an earlier build
are not adopted or reused by the fix.

Claude retains up to sixteen sessions, matching gateway admission and
accommodating an eight-member Team. Tool handoffs and completed lean Chat
conversations expire after fifteen idle minutes. Other surfaces still close
completed queries. Retention does not keep Anthropic's server cache warm by
itself, and no paid keepalive requests are sent. Native workspace tools remain
disabled; host approval and tool execution are unchanged.

### Claude user steering and contribution boundaries

Lean Chat sends a stable chat/member scope to the Claude adapter. With Claude
Code 2.1.290 or later, the adapter keeps `--input-format stream-json` stdin
open and sends only new user content on subsequent turns. It retains the
CLI's native assistant messages and real MCP results instead of flattening
the entire history into a new user prompt. A Team child also reuses its
member's in-memory reference projection across contributions. Each member has
an independent process; model/account/system/tools/effort and output-limit
changes still prevent reuse. Older or unidentified CLI versions use the
previous single-query behavior.

The scope is not sufficient on its own: reuse requires an unchanged history
prefix and the exact host-visible assistant reply. All pending tool results
must match before new user text can follow them. Real user instructions are
sent as native user frames, never inserted into MCP tool results. UUID
acknowledgments and the terminal result's `user_message_uuids` identify the
instruction Claude took. If an earlier query finishes while a steer remains
queued, the adapter keeps reading until the steer is answered, preserves
both replies, and sums each query's usage without subtracting a previous
query's lifetime counters. Unknown usage stays unknown. Missing
acknowledgments, cancellation, or partial delivery retire the session;
duplicate host transitions are rejected rather than replaying their tools.

Compaction, edited history, process exit/expiry and gateway restart still
require reconstruction. Server-side web-search identities assigned by the
outer relay cannot currently be verified by the adapter: those legs are not
retained for scoped conversation reuse. This favors a safe reconstruction
over silently skipping changed history. The ordinary tool-only continuation
path on other CLI surfaces is unchanged.

When supplied by Claude, `cache_creation_5m_tokens` and
`cache_creation_1h_tokens` are retained alongside cache reads/writes in
activity and timing telemetry. Timing also distinguishes tool-result reuse,
steering, new user turns and missing matching conversations. No prompt text,
task identity or tool output is added to those diagnostics. Token counts are
provider telemetry, not a calculation of subscription allowance consumption.

`test_claude_conversation.py` exercises eight-member isolation, steering at
real socket-backed MCP handoffs, completed-turn follow-ups, delayed queued
turns, per-query usage, exact reply matching, images and cancellation.
An additional local qualification used the installed Claude 2.1.293 binary
against a synthetic loopback Messages server: one native session handled a
tool call, a user steer and a subsequent completed-turn follow-up, preserving
typed history. It used a temporary config home, no live chat content and no
paid inference. This verifies transport behavior, not server cache savings.

Protocol references: [streaming input](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode),
[CLI stream flags](https://code.claude.com/docs/en/cli-reference), and
[SDK transport changes](https://github.com/anthropics/claude-agent-sdk-typescript/blob/main/CHANGELOG.md).

### Grok completed-turn continuity

Lean Chat supplies a chat/member scope and connection identity. Grok starts
with a fresh UUID via `--session-id`; an unchanged prefix and exact returned
assistant reply permit a later user turn to use `--resume <that UUID>` in the
same private MCP workspace. Only the appended user content is sent. It never
uses the user's most-recent session, never restores code, and verifies the
session ID returned by the CLI before accepting output. A previous query must
exit cleanly before resume. Tool registries and permission checks remain the
same as fresh requests. The pool retains at most sixteen idle leases for up
to fifteen minutes, covering an eight-member Team; cached replay identifiers
expire with the existing pool policy. Duplicate host requests are refused.

Completed-turn resume starts a new Grok process; matching pending host calls
continue to reuse their existing process. User steering at an unresolved
handoff and newly appended images still use portable reconstruction rather
than pretending Grok supports Claude's input protocol. Changed model, effort,
connection, tools, system text, CLI binary or history also prevents reuse.
The CLI's own session storage follows its existing retention policy; Provider
Hub does not read credentials or delete vendor transcripts.

Installed Grok 1.0.60 was qualified against an isolated loopback fake API:
explicit session resume preserved native history, and the actual adapter sent
only the new prompt on its second invocation. This used a temporary home and
synthetic data, with no paid inference. Fixtures also test exact reply matching,
scope isolation, foreign session IDs and duplicate requests.
See [Grok CLI reference](https://docs.x.ai/build/cli/reference).

### AntiGravity streaming continuation

The installed `--help` must advertise stream-json input before the lean Chat
adapter uses it. A single process then receives sequential
`{"event":"user","message":{"content":"..."}}` frames. Only matching
completed replies can continue: host history, configuration and connection
identity are checked first. Structured replies are parsed into the same
host-visible text/calls for matching, ignoring only host-generated call IDs;
each real tool result must still match the saved call exactly. The native
conversation ID must remain unchanged. There is no concurrent prompt queue.

The sandbox, plan mode and pre-tool denial hook remain active. A confirmed
early native-tool denial is returned to the host and retires the process,
because that native turn has not reached a successful terminal boundary.
Images, malformed history and oversized appended content use the existing
lossless fresh context transport. Files from prior context reads remain scoped
until the process is reaped. No tool result or user instruction is invented.

AGY's documented cumulative session counters are differenced per completed
query. Missing or decreasing counters remain unknown; they are not replaced
with invented cache hits. This path is qualified with protocol fixtures and
the installed 1.3.6 help contract; live subscription cache savings have not been
measured. See [AntiGravity headless protocol](https://www.antigravity.google/docs/cli/headless/).

### API prefix and cache audit

Kimi, Mistral (including Vibe credentials) and Ollama use API transports here.
They already receive the same bounded Team peer digest. Their lean Chat
notebook/Blackboard/work-record updates now use the append-only in-memory
projection rather than replacing the first history message every round.
Historical budget-reminder text is left unchanged on these audited lean Chat
routes; current context/output limits are still enforced. Compaction and
projection resets intentionally rebuild a shorter prefix.

Mistral lean Chat sends an opaque `prompt_cache_key` derived from model,
conversation/member and connection scope, never prompt text or credentials.
Reported `prompt_tokens_details.cached_tokens` is split from total input once
in both streaming and buffered Chat-Completions translation. Missing or invalid
cache counts remain absent. Mistral documents that the key improves routing
but does not guarantee hits: [prompt caching](https://docs.mistral.ai/studio/conversations/advanced/prompt-caching).

No unverified Kimi cache flags or TTLs are added. The existing Kimi Messages
cache counters pass through when the provider supplies them. Ollama model
residency (`keep_alive`) remains distinct from cached prompt evaluation; its
native API documents `prompt_eval_cached_count` and `prompt_eval_duration`, but
this route uses compatibility endpoints and does not infer missing native
metrics. See [Ollama chat metrics](https://docs.ollama.com/api/chat).
Synthetic fixtures verify request prefixes and telemetry arithmetic; they do
not establish provider-side hit rates or subscription savings.

### Prompt delivery and visible activity

True Team user messages are delivered in a separate user-instruction block,
with exact text, attachments, mention targeting and source identity. They are
not shortened by the peer-preview budget and are no longer wrapped as portable
peer history. Every member also receives an exact source locator for its latest
addressed request through the bounded reference projection. The locator does
not repeat the instruction text: it provides a direct paginated `read_history`
source to use only if the original is no longer in active context. Saved records
remain complete. Fresh-member Grok and MiniMax plans are tested for exact
beginning-to-end delivery, without asserting that a model will always use it.

Transcript paging includes live segments and their speaker headers even when
those segments belong to an older member section outside the normal tail.
It does not expand unrelated old sections or alter worker scheduling. A
`needs_input` outcome still blocks new contributions while current work
settles. Hidden display activity, prompt reconstruction and deliberate input
pauses are separate conditions.

For Claude, Codex and Grok, notebook, Blackboard and work-record snapshots use an append-only in-memory
projection. A changed snapshot travels as reference text in a fresh tool
result; it does not rewrite the beginning of the conversation. Budget reminders
use that same result boundary. Saved provider history stays portable and
unchanged by reference projections. Old snapshots count toward the context
budget; projection resets discard superseded snapshots. Compaction, history
edits, route/account switches and unmatched tool results still start a fresh
native query. User steering never masquerades as a tool result. History that
reaches its 85% input watermark is trimmed to a 60% target (minus output and
notes) rather than shaving a few messages from a full prefix on every round.

Routine Team peer content is now a source-linked preview, with full text
available through recall tools. It does not auto-drain old overflow into later
requests. See [Team context delivery](chat-team.md) for preview and mandatory
input rules. Muse CLI and AntiGravity are still stateless; smaller inputs help
them without claiming native continuation support.

CLI usage without a native snapshot carries `provider_hub_usage_source: estimate`.
The gateway retains that tag in activity logs, tracks estimated inputs separately
and excludes them from provider-reported totals and token calibration. The lean
harness also excludes them from measured Team consumption and marks its usage
incomplete. A later native snapshot replaces the initial estimate and label.
Historical logs cannot be retroactively disambiguated by this change.

Responses-format clients receive `usage: null` when native usage is unavailable.
A valid native snapshot replaces the estimate and restores reported usage in
both streaming and buffered Responses replies.

## Claude-specific execution settings

The lean route uses `--system-prompt` with the host's complete instructions
and MCP bridge guidance. Desktop routes retain `--append-system-prompt`.
Offered MCP definitions remain loaded eagerly: the host owns the tool set,
and this bridge cannot safely enable an unprovided native tool-search loop.
Keep optional capabilities disabled when unused to reduce that schema cost.

`max_tokens` becomes the documented per-child `CLAUDE_CODE_MAX_OUTPUT_TOKENS`
setting and is part of the session key. Claude may clamp it further for its
model; it is a per-request output ceiling, not a subscription budget or a
guarantee about internal model work. Team requests also set
`CLAUDE_CODE_MAX_RETRIES=2` to bound the inner retry loop beneath the Team host's
existing retries. Parent, Side Chat and helper lanes retain Claude's default
retry policy because those roles have no outer retry loop. Account settings,
selected effort and fast mode are unchanged.
See [Claude environment variables](https://code.claude.com/docs/en/env-vars)
and [system prompt flags](https://code.claude.com/docs/en/cli-reference).

The opt-in `PROVIDER_HUB_CLI_TIMING_LOG` adds Claude process-start counts,
reuse, prompt/system/schema byte counts, offered tool count and separately
reported input/output/cache-read/cache-write tokens. It never records prompts,
tool arguments/results or credentials. Missing token fields remain unknown.
Cache reads contribute to context size and must not be treated as fresh input
or as a subscription spend meter.

For a bounded, real-account comparison (these commands consume model usage):

```sh
uv run python scripts/benchmark_cli_handoffs.py --provider claude --model claude-sonnet-5-5 --effort low --calls 3 --repeats 1 > .local-only/claude-live.jsonl
uv run python scripts/benchmark_cli_handoffs.py --provider claude --model claude-sonnet-5-5 --effort low --calls 3 --repeats 1 --fresh > .local-only/claude-fresh.jsonl
uv run python scripts/summarize_cli_timings.py .local-only/claude-live.jsonl
uv run python scripts/summarize_cli_timings.py .local-only/claude-fresh.jsonl
```

Compare like-for-like model, effort and task. First-event latency, cache-read
fraction and output tokens describe different effects; this summary does not
claim a subscription saving or a generation tokens-per-second measurement.

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
