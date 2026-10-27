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

The native catalogue's larger `max_context_window` is used only alongside
the turn-launch override that selects it. Current Codex turns apply that
override, and discovery reports the corresponding effective capacity after
the runtime reserve. The original qualification inspected a 272,000-token
default, an 872,000-token maximum and 95% effective capacity; the default
alone was 258,400 usable tokens. These are historical observations, not
hard-coded model specifications or a promise that future runtimes retain
the same limits.

Missing, malformed, unmatched, or incomplete catalogue metadata leaves the
capacity unknown. No model-name heuristic, cross-provider limit, or UI patch
is used. A refreshed native catalogue is picked up on the next provider
catalogue refresh.

### GPT-6.1 Terra

The Codex CLI provider discovers `gpt-6.1-terra` from the native runtime's
catalogue; Hub does not seed it as an always-available model. OpenAI requires
[Codex CLI 0.159.0 or newer](https://learn.chatgpt.com/docs/models). Context,
input modalities and reasoning efforts come from the installed runtime's
model card. Hub supports its documented
[Fast control](https://learn.chatgpt.com/docs/agent-configuration/speed),
alongside Standard; it does not assume an Ultrafast tier.

A native cache predating the rollout can omit Terra. When Hub neutralizes
a desktop catalogue override or pins code-mode cards to direct tools, it
lists from that cache, so Hub's Refresh alone cannot fetch newer native
cards. Refresh the account's native Codex catalogue first, then refresh
that account in Hub and enable Terra for the desired picker. Account
availability is established by a completed inference, not by a listing.
Existing model selections and chats keep their current model IDs.

### Runtime updates and overflow recovery

The desktop loads Provider Hub's projected catalogue separately from the
nested Codex runtime. A runtime update can shrink its model window while
the Hub still advertises a previously discovered larger capacity. At each
Codex CLI request, the gateway now reads that account's current native
`models_cache.json` before estimating, compacting and checking the input.
The exact model card's maximum and effective percentage can lower the saved
planning limit, never enlarge it. Smaller configured input limits remain
in force. Missing, malformed or unmatched metadata leaves the saved limits
unchanged. This read neither launches a CLI nor opens credentials, and it
does not overwrite catalogues or conversations.

Codex `contextWindowExceeded` errors and recognized context-overflow text
now retain `context_length_exceeded` through the CLI adapter, Messages relay
and Responses translator. Buffered failures return HTTP 400 with that code;
streaming failures end in `response.failed` with that code, including when
text or an idle ping has already started. They no longer become a generic
`502/provider_error` that invites retries of the same oversized input.
Network errors, quota failures and output limits remain distinct.

The desktop can use this signal for context recovery. Compaction reduces
the active model input; it does not delete the saved transcript or provide
unlimited exact recall. A single oversized newest input, huge fixed tool
definitions, or a failed compaction can still require a smaller attachment,
shorter input or explicit handoff. Do not raise context settings above the
selected runtime's advertised capacity to suppress the error.

After a desktop/CLI update, refresh the Codex provider catalogue and reapply
Provider Hub's desktop configuration through the normal UI so the visible
meter and per-model compaction threshold agree with the new runtime. For
an already full thread, use the client's compaction command where available
before another large turn. The per-request guard protects planning while
the UI may still be displaying old catalogue values.

References: [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference)
and [OpenAI compaction](https://developers.openai.com/api/docs/guides/compaction).

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
