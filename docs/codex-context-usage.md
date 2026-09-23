# CLI routes and the Codex context indicator

The desktop setting enables the indicator; it does not supply its data.
Inspection of the installed ChatGPT desktop assets on 20 September 2026
confirmed that the composer requires a positive `modelContextWindow` and a
nonnegative `last.totalTokens` on its latest thread usage record. A missing
value hides the ring. The small grey circles beside the non-Codex models in
the reported screenshots are context indicators.

The host receives that record through `thread/tokenUsage/updated`, documented
in [Codex App Server events](https://learn.chatgpt.com/docs/app-server#turn-events).
Provider Hub supplies the host with model capacity through its projected
catalogue and request usage through the terminal Responses object.

## Capacity

Codex's `model/list` omits context metadata. The CLI route now joins its exact
model IDs to the native catalogue the nested runtime listed from: the one
selected on its command line, or, when none is (the real configuration selects
no `model_catalog_json` to neutralize), the runtime's own cache as that listing
refreshed it. It preserves the default `context_window`, the effective
percentage reserved by the runtime, and a numeric `model_context_window`
override reported by `config/read`. The resulting `runtime_context` survives
Hub normalization and becomes the host's effective context budget, including
the existing 85% automatic compaction threshold.

The larger `max_context_window` is not evidence that a request is using that
window. It is never substituted for the runtime default. For example, the
local cache inspected during this investigation reported a 272,000-token
default, an 872,000-token maximum, and 95% effective capacity for Astra and
Sol. With no override, that yields 258,400 usable tokens. These are observed
local catalogue values, not hard-coded model specifications.

Missing, malformed, unmatched, or incomplete catalogue metadata leaves the
capacity unknown. No model-name heuristic, cross-provider limit, or UI patch
is used. A refreshed native catalogue is picked up on the next provider
catalogue refresh.

## Usage

The nested Codex adapter forwards the latest request snapshot from
`thread/tokenUsage/updated`. It uses `last`, not cumulative `total`, and
filters notifications by thread and turn. Cached input is split from uncached
input for the Messages hop and recombined exactly once for Responses.
Repeated snapshots replace one another, so a lower count after compaction
is preserved. Malformed counters are ignored; a measured zero is retained.

The relay forwards those counts in both streaming and buffered responses.
The desktop host produces its own thread usage notification from the result;
the nested RPC notification is not sent directly to the desktop connection.

Adapters or tool handoffs without a usable snapshot retain the existing input
estimate and zero output fallback. Those counts are not measured usage.
Adding native usage to another CLI requires that provider's per-request
semantics; lifetime totals must not be used as current context occupancy.
The desktop's own layout and mode gates also still apply.

## Validation and activation

Offline regression tests cover native default versus maximum, configured
overrides, malformed or missing metadata, exact model matching, projection
through both catalogues, request versus lifetime counters, cache accounting,
foreign thread/turn events, repeated snapshots, and the terminal Responses
usage object. They do not make provider inference requests.

Read-only qualification on the installed runtime listed seven native models,
including Astra and Sol with the effective budgets above. A second isolated
runtime accepted both projected Hub rows. This verifies discovery and catalogue
compatibility without switching the user's profile or generating a response.

An installed bundle does not run edited repository files. To activate a source
fix, build/install the updated Provider Hub, refresh the Codex CLI provider
catalogue, and reapply the desktop configuration through the normal Hub flow.
After the desktop reloads the catalogue, a completed request with valid usage
provides the indicator's data. Existing task records may retain their previous
usage until the next update. Verify the tooltip and that its usage drops after
compaction in a separate live qualification; a source test is not proof that
the running desktop has loaded the change.
