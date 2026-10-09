# Claude prompt caching and usage

Provider Hub has two Claude transports. They share the local Messages endpoint,
but caching is controlled at different boundaries.

## Anthropic API mode

For the `claude` provider using an Anthropic API key, the outbound request gets
`"cache_control": {"type": "ephemeral"}` when the caller supplied no cache policy.
This is Anthropic's automatic caching mode: its breakpoint follows the last
eligible content block as the conversation grows. It uses the default five-minute
TTL. A caller's top-level policy or tool/system/message breakpoints are preserved
instead, including its TTL and breakpoint count. The bridge does not modify
signed thinking or add cache controls to other providers' Messages-compatible
endpoints. [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

Caching is a request, not a guaranteed hit. Reuse depends on an identical prefix,
supported model, minimum length and unexpired cache. Cache writes carry a premium;
short one-off requests may not benefit. The bridge does not opt users into the
more expensive one-hour TTL. [Caching costs and constraints](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

## Claude Code CLI mode

Subscription-backed routes invoke Claude Code. That process constructs the
Anthropic requests and already manages prompt caching. Adding `cache_control`
to a gateway payload cannot configure a subprocess's hidden HTTP requests.
Provider Hub leaves the CLI's cache settings alone. Replayed context is not
evidence of uncached billing: Claude Code still sends the conversation while
eligible prefixes can be read from cache. [Claude Code caching](https://code.claude.com/docs/en/prompt-caching)

The Claude adapter forwards the native usage reported in message streams,
assistant snapshots and final results. Repeated snapshots of one message ID
replace that message's counters; they are not extra requests. A result's aggregate
replaces the per-message sum. Live host-tool handoffs report only usage since the
previous handoff, preventing earlier legs from being counted again. Provider
fields are validated as nonnegative integers; text, reasoning, account metadata
and monetary estimates are not copied into usage. This follows the SDK's
distinction between per-step messages and cumulative main-loop results.
[SDK usage accounting](https://code.claude.com/docs/en/agent-sdk/cost-tracking)

Older CLI output without usable usage retains the existing gateway estimate.
If the CLI reports input/output without cache telemetry, the Messages response
and activity record keep cache fields absent. An absent cache counter is
**unknown**, not proof of a cache miss; prefer those records over a Responses
client's zero-default `cached_tokens` display when diagnosing missing telemetry.
Native CLI auxiliary calls outside the reported main-loop usage are not inferred.
This instrumentation does not promise an API-price-to-subscription-quota mapping.

## Reading the measurement

Successful Messages replies, their final SSE usage, and the private local
`activity.jsonl` records retain these provider fields:

| Field | Meaning for Claude |
| --- | --- |
| `input_tokens` | Uncached input |
| `cache_creation_input_tokens` | Input written into cache |
| `cache_read_input_tokens` | Input read from cache |
| `output_tokens` | Output reported for the request |

Total input is the sum of the first three fields. Responses clients receive
that total as `input_tokens`, with cache reads in
`input_tokens_details.cached_tokens`; cache creation stays included in the
total. The gateway's authenticated status endpoint exposes separate cache-read
and cache-write counters and `total_input_tokens`, globally and per provider.
Its existing `input_tokens` counter remains the sum of the Messages input field.
Status totals include the estimate fallback for routes that cannot report usage.
The Hub's existing tokens-used display uses total input plus output, so cached
tokens do not vanish from that total when the detailed counters become available.

For a controlled comparison, keep the model/account, tools, system prompt and
conversation prefix fixed. Read the first request's counters and a follow-up
within the cache lifetime. A write followed by a substantial read directly
demonstrates caching. Record compaction, model changes and idle time alongside
any misses. Use provider-reported counters before attributing a subscription
meter change to full-rate input; meter movement alone does not distinguish cache
writes, cache reads, output or unrelated usage. The journal holds usage metadata,
not prompts or credentials. [Claude Code usage diagnostics](https://code.claude.com/docs/en/costs)

Tests use fake CLI streams and loopback HTTP providers. No billed request,
account setting or live subscription measurement is needed for these tests.
