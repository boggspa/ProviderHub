**Grok CLI route: the MCP compat race and the argv cap — 24 September 2026**

> **Historical record.** Dated 24 September 2026. Version numbers, build
> numbers and test counts below are as they were then and are not maintained.
> For current state see [`README.md`](README.md).

Two failures made the Grok Build CLI route unusable from the Codex desktop app
on `grok-4.7-build-fast`, and the hub's activity log for the day shows both:
one 502 at 21:58 with "grok did not disable its native CLI tools; this runtime
cannot safely forward host calls", and sixty 502s between 19:06 and 19:12,
which the Codex rollouts record as "prompt is too large for the grok CLI
positional argument (272167 bytes; maximum 262144 bytes)" retried by the app.

The first was diagnosed from the CLI's own session records rather than from
the hub, which did not say which tools it had seen. grok 1.0.41 imports MCP
servers from `~/.cursor/mcp.json` and `~/.claude.json` by default (its
`[compat.cursor] mcps` and `[compat.claude] mcps` cells), so every hub turn
started the user's three Cursor MCP servers inside the throwaway workspace,
and the taskwraith server registers `read_file`, `write_file`, `web_search`,
`web_fetch`, `run_shell_command` and `list_directory`. Whether those names
are in the `system/init` line's `tools` list depends on a race between the
server handshake and the turn start: in the failing session the server
connected 1 ms before `turn_started`; in every other session of the day, and
in four direct replays of the hub's argv, the turn started first and the
registry read `[]`. `--disallowed-tools` removes built-ins only, and the
guard, which requires an exactly empty registry before it will forward a
tool call to the host, was doing its job: the tools really were live.

Hub turns now spawn the CLI with `GROK_CURSOR_MCPS_ENABLED=0` and
`GROK_CLAUDE_MCPS_ENABLED=0`, the per-process env cells the CLI documents
for exactly this; `grok inspect --json` under that environment reports the
three servers `disabled` with `source: env`, and two live turns (one via
`--single`, one via `--prompt-file`) reported `mcp_servers: []`, `tools: []`,
and no MCP events in the session record. The user's `~/.grok/config.toml`
is not touched. The guard now compares the registry as a set, names the
tools it found, and when the registry is not empty before any output has
reached the client the turn is spawned a second time with those names
appended to the removal list, which covers a built-in a newer CLI adds
(1.0.41 added `sports_search`, now also on the static list) and a native
`[mcp_servers]` entry when the second spawn wins the race; names whose
removal worked are kept for the process lifetime, and a second failure
reports the names. A missing registry is still refused without a retry, and
only names that can be a plain argv value are ever appended.

The second failure was the route's own 256 KB cap on the `--single`
positional argument, refused before spawn once a Codex conversation grew
past it, so long conversations died permanently. The prompt now moves into
the workspace prompt file when prompt and system override together exceed
the cap, the same ACP JSON transport the screenshot path already uses,
verified live on 1.0.41 with a text-only file (`result: "OK"`, same token
count as the positional form). Small prompts keep the positional argument.

Offline coverage: registry naming and order independence, the single
respawn with names removed and remembered, refusal after a second failure,
unsafe names never reaching argv, the child environment carrying only the
two cells plus the allowlist, and the prompt-file switch-over including the
system override's share of the budget.

**Muse and Grok host tool handoffs on the Messages surface — 19 September 2026**

> **Historical record.** Dated 19 September 2026. Version numbers, build
> numbers and test counts below are as they were then and are not maintained.
> For current state see [`README.md`](README.md).

Claude Desktop drove the CLI-backed Muse route into narration with no host tool
action. The Messages surface now renders host tools as the nested CLI's own
enforced output schema rather than the prompt-only sentinel envelope, because
Claude's native tool names (`Read`, `Edit`) collide with the nested CLI's own
inventory and a prompt-only handoff could finish as prose or turn into a native
search loop. The Responses/Codex surface keeps the sentinel protocol, selected
by an explicit marker set by the Responses bridge.

That schema path then failed for a second, separate reason, reproduced live on
Muse Code 1.3.0 (1.3.0-R3401.1): `muse exec --output-schema` can concatenate
more than one schema-shaped answer onto one output stream with no separator
between the values. Replaying one captured request four times per setting, this
appeared in 1 of 4 runs at `--max-model-steps` 2, 4, and unset alike. Decoding
the buffer as a single JSON value rejected the whole turn, which spent the
one-shot formatting correction; that correction asked the model to "reissue the
intended call", and the model re-ran an `Edit` the host had already applied,
which then failed against the file it had itself changed. Two live end-to-end
runs reached no result inside the harness's 180s budget.

The later values are written as though the host had already executed the first
one, so they describe work that never happened: one observed tail "verified" an
edit the host was never asked to make, another simply repeated the same `Read`.
Only the first value was generated from the real host transcript, so the first
complete JSON value in the stream is now the authoritative reply and the
remainder is discarded unparsed. A tail can neither be executed nor rescue a
first value that fails validation. The correction message now says that only the
rejected reply was undone, that every host result already in the transcript is
real and complete, and asks for the call that comes next instead of a reissue of
the last one.

A one-step cap was tested as the alternative and rejected. `--max-model-steps 1`
never concatenated, but made muse report "model did not reach a terminal state
within 1 step(s)" and fail a run whose handoff had already been generated
correctly (1 of 4 replays). The budget stays at 4 as headroom for muse to
settle; it is not what bounds host work, and no budget prevents the extra
answers. The first-value rule, not the budget, is the correctness boundary.

Live qualification used `Source/verify_claude_cli_tools.py` against Desktop's
Claude runtime (claude 2.1.276, `claude-fable-5`, `--tools Read,Edit`), which
spends real inference on the selected vendor CLI's own login. `muse-spark-1.3`
and `grok-4.6` each passed 3 of 3 runs, completing the full
Read → Edit → read-back cycle against a disposable fixture with the edit
verified on disk and `VALUE = 7` preserved. Three of the 11 muse model turns
across those runs arrived as concatenated replies and were absorbed by the
first-value rule: the defect still occurs at roughly the measured rate, and no
longer reaches the harness. Before the repair the same harness ended two runs
with no result at all.

One `grok` run, before these repeats, tripped the pre-existing native-tool
guard ("grok did not disable its native CLI tools") on its first request, with
no output. It did not recur in 3 live runs or in 14 direct replays of that same
request, Claude Desktop's own retry absorbed it, and it is not diagnosed here.

These are observed CLI behaviours on one installed version each, not documented
contracts. The first-value rule is inferred from those traces: it is the only
value a vendor can generate from the real host transcript, but no vendor
promises that the extra answers exist, or that they will keep arriving in this
order. The offline suite (1184 tests) covers the parsing, limit, tool_choice and
cleanup boundaries without inference.

**CLI harness correctness — 19 September 2026 (follow-up audit)**

The Claude Desktop → Codex CLI startup failure was reproduced on codex-cli
0.153.0 with `mcp__ccd_directory__change_directory`: `thread/start` returned
JSON-RPC -32600 because that dynamic tool name is reserved. No model turn was
started in the reproduction. Namespacing alone did not protect MCP-prefixed
host names.

Codex now registers every host tool under a deterministic `bridge_` alias,
identifies the original name in the tool description, aliases historical
function calls identically, and translates requested calls back to the exact
host name before allowlist validation and dispatch. Tool arguments and call IDs
are preserved. Aliases are independent of tool ordering and do not collide with
host names that happen to resemble generated aliases. A real two-turn
`gpt-5.6-sol` check changed a disposable host working directory through the exact
previously rejected tool name, then correctly read that directory from the host
result. This used two model turns; registration checks and the remaining audit
used mocks, schemas, or offline echo instead of repeated inference.

Invalid-request/method/parameter JSON-RPC rejections now propagate as HTTP 400
(or an invalid-request SSE error after streaming begins), rather than a
retryable 502. Genuine transport failures remain errors. CLI errors no longer
also create false `completed`/200 activity records.

Two additional Astra Max agents audited native output handling and implemented
bounded adapter repairs, integrated with the parent changes:

| Area | Reproduced defect and repair |
| --- | --- |
| Shared formatting retry | First-attempt prose could be shown and then repeated by the correction attempt. Assistant text is now held until a valid reply/handoff; rejected-attempt prose is discarded. Pings preserve connection liveness, and genuine thinking stays on its own channel. |
| Grok | Repeated snapshots could duplicate text; one streamed block could suppress another unstreamed block. Reconciliation is now by message ID, block index, and type; distinct messages are retained even when their prose matches. |
| Claude | Early progress could hide a final message or snapshot-only thinking. Per-message/block reconciliation restores missing material and avoids replaying the CLI's last-block result projection. Missing terminal results and nonzero exits are failures. |
| Muse | An earlier delta could suppress terminal text containing the actual host tool call. Missing cumulative suffixes and separate final text are recovered without replaying tool envelopes. Output/completion is correlated to the foreground `payload.run_stream`; empty success is an error. |
| AntiGravity | Clean EOF without SUCCESS, interrupted/cancelled statuses, and nonzero exits could look complete. Successful results are now required and a missing final response suffix is retained. Scoped image reading is preserved. |
| Codex | Turn-wide fallback flags could drop a distinct final item or published reasoning summary. Reconciliation is per item/section, with separate wire blocks for distinct items and reasoning sections. |

The Muse event identity contract was checked with installed Muse Code 1.3.0
(1.3.0-R3401.1) using the offline echo provider. `payload.run_stream` identifies
the run; the enclosing stream identifies the session. The observed echo delta
has text but no phase field. This is not proof that the real Meta provider always
uses the same final-text convention. Final-only prose is preserved conservatively;
conflicting already-streamed tool envelopes fail rather than execute alternatives.

Host-facing `thinking.type: disabled` and `thinking.display: omitted` now suppress
readable thinking without turning it into assistant text. Display suppression is
not a promise that the CLI model performs no internal reasoning or consumes no
reasoning budget. Legitimate assistant commentary is not heuristically relabelled
as hidden thinking.

Codex CLI published summaries are exposed through Responses only when explicitly
requested with `reasoning.summary` = auto, concise, or detailed. The preference
reaches `turn/start.summary`; raw reasoning text is excluded from that summary
path. The Responses adapter emits summary part/text events and a final summary
array, preserving encrypted replay. Redacted blocks and signatures remain hidden.
The Codex model catalogue advertises this opt-in capability only for reasoning
models on the Codex CLI route. Other providers' thinking is not automatically
relabelled as a published summary. These boundaries follow the
[Codex event contract](https://learn.chatgpt.com/docs/app-server#item-deltas),
[Responses summary event schema](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.reasoning_summary_part.added),
and [Anthropic thinking/display contract](https://platform.claude.com/docs/en/build-with-claude/thinking).

Evidence limits: the user's stopped Grok/Muse runs have no retained native event
trace here. The failures above were independently reproduced, but cannot be
assigned to every sentence in those screenshots. A genuinely completed intro-only
model reply can still occur; stronger host-action instructions are not a guarantee
of model behavior. No stopped user run was resumed.

Final validation: 1,142 tests passed on uv CPython 3.13, including
adapter, host-tool, image, HTTP error-status, catalogue, and Responses regressions.
The GPT-5.6-Sol reserved-name call/result smoke passed. The patched Muse adapter
also passed an installed-runtime offline echo smoke. Source changes require a
new app build/relaunch; summary capability metadata also requires a catalogue refresh.

---

**CLI screenshot forwarding — 19 September 2026**

The bridge now preserves embedded screenshot images from Messages and Responses
tool results. Codex receives native `input_image` content in call-correlated
history; Claude receives a structured stdin message; Grok receives ACP image
blocks in a private `.json` prompt file; Muse receives private image attachments.
AntiGravity receives temporary image paths and a scoped `--add-dir` workspace;
its native `view_file` reader decodes only those declared image copies.
Text transports number images in transcript order and retain the originating
tool-result id. PNG/JPEG screenshot dimensions are included for coordinate work.
The bridge does not resize screenshots or put their base64 bytes on argv.

| CLI route tested | Installed runtime | Visual evidence | Qualification |
| --- | --- | --- | --- |
| Codex `gpt-6-astra` | codex-cli 0.153.0 | Selected the purple triangle at (650, 300); after a real host browser click, read `K9P7` from the returned screenshot | Screenshot → click → screenshot verified |
| Claude `sonnet` alias | Claude Code 2.1.276 | Selected (649, 290); read the verification code after the host click | Screenshot → click → screenshot verified |
| Grok `grok-4.6` | Grok 1.0.34 | Selected (640, 250); read `K9P7` after the host click | Screenshot → click → screenshot verified; one earlier run stopped at the existing native-tool isolation check |
| Muse `muse-spark-1.3` | Muse Code 1.3.0 (1.3.0-R3401.1) | Read `K9P7` from an image-only screenshot result | Image recognition verified; coordinate-control trial missed the target, and a later trial timed out. Do not claim a verified click loop for Muse |
| AntiGravity `gemini-3.1-pro` | agy 1.2.7 | Native `view_file` read the image through a scoped temporary directory; selected the triangle at (650, 285) after coordinate-unit guidance | Screenshot → click → screenshot verified; read `K9P7` from the returned image |

The browser check used a disposable local canvas with three coloured shapes.
The model was given only the screenshot, host tool definitions, and the task;
it did not receive DOM text, page source, target coordinates, or the verification
code. Model-selected coordinates were executed through the actual Computer Use
browser tool, and a second screenshot was returned to the model. This qualifies
the tested browser task, not arbitrary applications, permissions, or models.

The catalogue advertises image input for runtime-advertised Codex models and
the specifically verified Claude, Grok, Muse, and Gemini identifiers above. Unknown
models are not assumed to support vision. AntiGravity still rejects structured
image blocks as documented in its [official headless input reference](https://www.antigravity.google/docs/cli/headless/#send-a-prompt). A filepath
is a distinct route: plain paths and `@path` both invoked `view_file`, but reads
were denied until the disposable directory was added explicitly with `--add-dir`.
No global settings or approval-bypass flags were changed. The adapter accepts
only `view_file` events for its declared image paths and reports other native
actions as errors during image turns. The scoped files are removed on exit.
Codex content shapes were checked against its installed experimental JSON schema
and the [app-server protocol](https://learn.chatgpt.com/docs/app-server).
Grok's `.json` prompt-file handling was checked against its
[official implementation](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/src/headless/cli.rs)
and the installed runtime.

Input limits are explicit: embedded PNG, JPEG, WebP, or GIF; at most 8 MiB per
image, 20 images and 32 MiB total per request. Remote image URLs are not fetched.
Invalid, oversized, and unsupported inputs return errors instead of being
silently flattened. Private media files are removed on completion, error, or
consumer cancellation. The old Codex `exec` fallback rejects image requests.

Validation: 1,089 tests passed with `uv run --python 3.13 python -m unittest
discover -s Source -p 'test_*.py'`; the new image suite covers byte preservation,
Responses ingress, native tool-result identity, image transport framing,
catalogue projection, size/format failures, and cancellation cleanup.
`bash -n Source/build.sh` passed; the build list includes `cli_images.py`.

GUI rollout requires rebuilding Provider Hub Preview, refreshing the enabled CLI
provider catalogues, and relaunching the desktop harness so its model catalogue
includes the new image capabilities. No notarized distribution was replaced in
this source change.

---

**Provider Hub Preview 0.3.2 — verification record**

Verified locally on 12 September 2026 on Apple Silicon macOS.

**Live checks completed**

| Check | Result | Scope |
| --- | --- | --- |
| Native preview build and code signature | Passed | Swift/AppKit/SwiftUI arm64 target, macOS 14+, ad-hoc signature |
| Preview launch and provider UI | Passed | Six provider cards, distinct preview identity/port/state, TaskWraith assets |
| Existing Mistral metadata import | Passed | Ten model/context groups; metadata only; no new Mistral inference |
| Kimi catalogue UI | Passed | Four documented routes; no API key configured, no claim of account access |
| MiMo catalogue UI | Passed | Two documented Token Plan routes and region selection; no inference |
| Ollama catalogue discovery | Passed | 49 available models from the existing daemon, enriched with `/api/show`; no model downloads |
| Shared gateway → Ollama → model tool cycle | Passed | `deepseek-v4-flash:cloud`: Read → Edit → Read → final confirmation |
| Desktop-managed Claude Code → gateway → Ollama → model | Passed | Real Read/Edit tools and adaptive thinking/High effort, isolated configuration and fixture |

The direct gateway tool cycle made four successful requests and used 2,012 input tokens and 222 output tokens. The model read a disposable `settings.txt`, changed `colour=blue` to `colour=green`, read it again, and confirmed the change.

The managed harness used:

`~/Library/Application Support/Claude-3p/claude-code/2.1.266/claude.app/Contents/MacOS/claude`

It ran in print/restricted/bare mode with only Read and Edit, an isolated `CLAUDE_CONFIG_DIR`, no session persistence, no loaded user/project settings or MCP servers, and a temporary working directory. The real requests sent `thinking.type=adaptive`, `thinking.display=omitted`, and `output_config.effort=high`. The adapter normalized these to the provider's supported control. All four requests succeeded, with 2,558 input tokens and 218 output tokens. The final response was “The verified colour is **green**.” The open Claude Desktop window and its profile were not restarted or changed for this test.

Two pre-inference harness attempts exposed the native Effort/adaptive normalization issue and were rejected locally before any model inference. These failures were fixed and covered by regression tests before the successful run.

**Offline coverage**

The complete unittest suite covers:

- Mistral Chat translation, tool names/IDs, continuation, images, completion signals, errors and cancellation.
- Provider-qualified routes, account-specific credentials, exact upstream IDs, official endpoint and MiMo region validation.
- Provider metadata provenance, deprecation, context variants, deterministic alias grouping and ambiguity handling.
- Strict settings types, credential revisions, cache invalidation, display-only branding and model label overrides.
- Native Messages JSON/SSE reasoning/signature/tool preservation over complete multi-turn HTTP cycles.
- Native protocol header forwarding without local bearer token, local API key or arbitrary identity header forwarding.
- HTTP 429/503, credential redaction, stream errors and cancellation cleanup without false success.
- Cerebras JSON/SSE reasoning authentication over text/tool envelopes, signature tamper detection, scope/model binding, multi-tool replay and streamed completion.
- A separate persistent signing key that is never supplied to Claude or forwarded upstream.
- Claude profile activation/recovery/restoration in temporary application-support directories, including external changes.

The final suite passed **102/102 tests** with ResourceWarnings promoted to errors on Python 3.13. A Python 3.11 run passed the HTTP provider/replay suite before the final native-control additions. All tests are offline, use temporary directories and mocked provider credentials, and avoid the user's real Claude profiles.

**What has not been live-qualified**

Kimi and MiMo do not have a root-run live qualification record. DeepSeek-direct and Cerebras now have saved-key discovery and a small live tool-cycle check; their other advertised models remain untested. Ollama-hosted DeepSeek is a distinct provider route from DeepSeek's direct API. Mistral's live GUI baseline predates this worktree; no additional Mistral inference was spent during the expansion.

No arbitrary non-Claude model has been qualified as an independently elected Auto reviewer. The existing Auto opt-in is preserved; an independent reviewer picker is not implemented because the inspected clients expose no supported routing control or stable request-role marker. The Auto trace was checked against Desktop-managed Claude Code 2.1.266 and standalone CLI 2.1.268; these are different binaries. See `NATIVE-AGENTS.md`.

PDF/base64 uploads on Chat-translated routes, hosted web search, Cowork VM behavior, audio, advanced hosted tools, and Codex Responses are outside this preview's validated scope. Cerebras streams are buffered to authenticate complete reasoning/tool envelopes. Claude's own context meter and picker affordances retain client-side assumptions that a gateway cannot fully override.

**Stable Mistral baseline**

Before this worktree, Mistral Bridge 0.2.0 completed a live Claude Desktop Code read → edit → read workflow through `mistral-vibe-cli-latest`. The original profile was restored afterwards. Of 201 preexisting session files, 200 remained byte-identical and the usage ledger retained its original bytes with new usage appended. The profile manager never reads or changes transcripts. The baseline commits remain in history. The provider extension was split into four coherent commits and merged into main after the full 102-test suite passed again; its source tree matches the live-tested preview exactly.


**0.3.1 refresh and launch follow-up**

The Cerebras 403 was reproduced on the authenticated model-list request with
Python urllib's default User-Agent. Sending the truthful `ProviderHub/0.3`
identity fixed discovery; the same header is used for inference. TLS and
provider authentication were preserved.

Saved-key discovery populated DeepSeek `deepseek-flash` and `deepseek-v4-pro`,
and Cerebras `gemma-4-31b`, `gpt-oss-120b`, and `qwen-3.8-27b`.

Two direct API tool-cycle checks passed using disposable fixture content:

- DeepSeek `deepseek-flash`: tool request, tool result, final green confirmation;
  two requests, 470 input tokens and 106 output tokens.
- Cerebras `gpt-oss-120b`: signed reasoning/tool request, verified reasoning
  replay with tool result, final green confirmation; two requests, 332 input
  tokens and 47 output tokens.

These used isolated local gateway state and did not modify Claude's open
conversation, profile, or the user's files. No Mistral inference was used.

Catalogue lifecycle tests cover app-open refresh, independent provider errors,
key revisions, selected-route refresh, transient cache fallback, removed models,
missing credentials, bounded timeouts, runtime metadata fingerprints, and
profile-write ordering. The full suite passes 117 tests. Runtime selection and
metadata are prepared before the gateway starts, and activation refuses a
stale gateway snapshot.

The installed Desktop's pure discovery/picker functions were also evaluated:
its discovery layer folds a `supports_1m: true` (or `max_input_tokens >= 1e6`)
entry into a bare plus `[1m]` pair, and its engines meter any id ending in `[1m]`
at 1,000,000 tokens. The gateway therefore advertises each 1M-capable route under
a single `[1m]`-suffixed slot id with `supports_1m: false`, yielding one picker
row at the 1M preset instead of a standard-plus-1M pair. Routes below 1M are
advertised bare as before. Plan-dependent routes (Kimi K3) qualify when
`max(context_options) >= 1M`, so K3 gets the 1M meter without asserting a single
fixed window; the explicit `k3-256k` entry stays bare at 262144.


**0.3.2 Muse API and version labels**

Muse is added as a Meta Model API-key connection through native Anthropic
Messages. Model discovery is authenticated and account-scoped; only IDs actually
returned by the account list are published. Meta's first-party documentation
enriches the exact `muse-spark-1.3` ID with 1,048,576 context and 131,072 maximum
output. Other model IDs retain separate metadata and unknown values remain
unknown.

Ten provider tests and two complete HTTP JSON/SSE tool-cycle tests cover the
new route, including bearer authentication, local-token separation, exact model
IDs, native thinking/signatures, adaptive thinking, and the original cookbook-era
Claude Max mapping to Meta High. Forced tool choices, unsupported reasoning disable, and Fast fail
with explicit compatibility messages. No Meta Model API key was read and no
paid Meta inference was run during implementation; account qualification follows
user key entry in the app.

Four regression tests restore Mistral Medium 3.5, Small 4, Large 3 and other known
version labels after a fresh catalogue projection. Medium's version comes from
its returned billing-model identifier; unknown latest aliases are not assigned
a guessed version. Distinct reported version identities remain separate and
user display overrides continue to win. All API routing IDs remain unchanged.


The Vibe credential control is labelled **Vibe saved API key** to match the
implemented key reuse. Public Mistral docs confirm shared included usage across
the API and Vibe, but no claim is made about the selected account's active plan.
No private plan lookup, credential change, or paid inference was performed for
this clarification.

The combined 0.3.2 suite passed **133/133 tests** with ResourceWarnings treated as errors. The native Swift build and ad-hoc signature verification passed.


**User-run live Muse qualification — 12 September, 20:22–20:23 BST**

The user configured the dedicated Provider Hub key and ran a Claude Desktop
repository-inspection conversation on `muse-spark-1.3`. Private gateway activity
confirms four HTTP 200 completions between 20:22:55 and 20:23:11 BST, with native
tool results carried across the turns. Provider-reported totals were 156,290
input tokens, 1,421 output tokens, and 0 cache-read input tokens. These are summed
request totals, including context resent during the tool loop.

This confirms live authentication, inference, and multi-turn tool operation for
Spark 1.3 in this account. The user's immediate screenshots initially showed
unchanged subscription percentages and zero PAYG counters. A later screenshot
at 20:48 BST reports **156.3k input tokens, 1.4k output tokens and GBP 0.14 under
Pay as you go**. Those rounded token counts match the gateway receipt, confirming
PAYG attribution for this test with the newly created Provider Hub API key.
The dashboard was filtered to all keys and models; the evidence is the matching
test totals and newly reported spend, not a provider-issued per-request invoice.
No additional inference was run to investigate billing. This result does not
qualify Muse Code's separate subscription login/MSP path.


**0.3.3 Grok PAYG API — deterministic qualification**

Grok is registered with an independent `XAI_API_KEY` credential entry and the
official xAI Chat Completions endpoint. No Grok CLI credentials were accessed,
and no paid xAI inference was run. Model availability remains account-specific.

Twelve new tests cover official endpoint/auth boundaries, account language-model
membership, context enrichment, exact-ID fallback limits, empty/malformed lists,
branding projection, effort normalization, explicit unsupported controls,
Priority requests, image/tool history, cache routing, full HTTP JSON and SSE
tool cycles, interleaved parallel arguments, final usage-only stream chunks,
actual tier reporting, HTTP 429 handling, and credential redaction.

All **145 tests pass** with ResourceWarnings treated as errors. The native Swift
build and ad-hoc signature verification pass. Fast requests are tested against
a mock returning the default tier, and the activity record correctly records
default. No assumption about charged Priority use is derived from the request.

Codex/ChatGPT Desktop and Ollama were inspected read-only. No OpenAI config,
authentication, sessions, or running tasks were changed for the harness research.


**0.4.0 Codex harness and all-provider Responses — 13 September 2026**

The combined suite passes **181/181 tests**, including a complete run under
the clean CPython 3.13.13 runtime embedded in the distribution app, with
ResourceWarnings treated as errors. The native Swift build and development
signature verification also pass.

The new tests cover the Responses endpoint, native Grok/Ollama forwarding,
namespaced function tools, streaming and JSON completion/error handling,
cancellation, request capacity, provider/account ownership of response IDs,
catalogue capabilities, and reversible TOML configuration. Restoration tests
exercise byte-exact unchanged restores, unrelated later edits, external provider
switches, provider-entry collisions, selection changes, and ambiguous external
model edits that must preserve the recovery journal.

All six translated providers—Mistral, Kimi, MiMo, DeepSeek, Muse, and
Cerebras—pass both JSON and streaming function-tool cycles through the existing
Messages adapters. Reasoning envelopes survive restart with the same local key;
tampered envelopes and cross-account/model replay fail explicitly. Provider
reasoning is carried as authenticated encrypted Responses history. No prompt
or reasoning transcript is stored in Provider Hub's response-ID journal.
Usage conversion includes input cache reads/writes so Codex receives the whole
input-token count.

The installed Codex app-server accepted the generated **74-model catalogue**
from seven configured accounts, including provider-qualified IDs, friendly
names, reasoning levels, and service-tier controls. No OpenAI model aliases were
required. The launcher repeats this installed-runtime compatibility check before
switching configuration and refuses a silent fallback to the built-in catalogue.
The provider list is account-dependent; the missing eighth account is Grok.
The UI review caught Meta's image-generation and transcription models in the
general model list. Discovery now excludes those exact non-chat IDs, explicit
non-text output models, and models marked deprecated/archived; text models
with vision input remain eligible.

Installed-engine probes in disposable Codex homes verified:

- A native Responses read/edit/read cycle using function/shell tools.
- The exact 500,000-token context from a mock model, plus local compaction under
  simulated context pressure through ordinary Responses requests.
- A harmless namespaced function dispatch, without creating another agent.
- An unreported context limit represented as `null`; Codex reported an unknown
  context instead of an invented 200,000-token limit.
- A translated Cerebras tool cycle with thinking preserved and authenticated
  across the Responses → Messages → Chat Completions round trip.

**Live installed-engine qualification**

| Route | Successful provider requests | Summed input / output tokens | Reported context | Result |
| --- | --- | --- | --- | --- |
| `ollama/deepseek-v4-flash:cloud` | 3 × HTTP 200 | 16,486 / 135 | 1,048,576 | Read/edit/read verified, 5.3 seconds |
| `cerebras/gpt-oss-120b` | 3 × HTTP 200 | 13,250 / 262 | 131,072 | Read/edit/read verified, 2.0 seconds |

The Ollama test ran on 12 September; the Cerebras test ran at 00:18 BST on
13 September. Each used a disposable workspace and the already configured
provider account. Counts are summed request usage, including history resent
during the tool loop. These results qualify those routes only. Other providers
have deterministic Codex bridge coverage; no new paid Mistral inference was
run, and no xAI key was configured for live Grok testing.

The active Codex GUI has not been restarted or switched to Provider Hub while
this development task runs in it. Installed-engine behaviour is verified, but
the final real GUI picker, context meter, restart, quit, and restoration cycle
remain a user-operated check. Claude's native third-party profile support is
independent of this Codex configuration switch.

**Distribution runtime**

The Apple Silicon distribution embeds a fresh relocatable CPython 3.13.13
runtime, `cryptography` 50.0.0, `cffi` 2.1.1, and `pycparser` 3.0. It includes
their upstream licence files and the vendored TOMLKit 0.13.3 MIT notice.
It does not include Vibe's Python installation, provider keys, app settings,
Claude or Codex sessions, or user account data. Python lookup prefers the
bundled interpreter and ignores ambient PYTHONHOME/PYTHONPATH.

The final 0.4.0 build 9 was signed with Developer ID, including all 12 embedded
native components. Apple accepted submission
`c792bff2-9c05-4441-a5be-9b682183353a`, created at
2026-09-12T23:39:28.286Z (13 September locally). Stapling and ticket validation
pass, `codesign --verify --deep --strict` passes, and Gatekeeper reports
`accepted` with `source=Notarized Developer ID`. The distribution zip is created
after stapling. The signed runtime passes SSL certificate loading and a Fernet
encryption/decryption round trip. A source rebuild does not inherit this
artifact's notarization.

The installed app's UI now shows all seven configured provider groups in the
Codex default-model menu. Muse shows Spark models only; its image-generation
and transcription entries are absent. The Codex sidebar label fits, and the
page states that the default sets the starting model while Codex receives the
whole compatible catalogue. No default was selected on the user's behalf.
During the update, idle Claude was closed and the app visibly confirmed that
its previous profile had been restored. Claude then reopened through the final
signed hub with the saved session list and provider profile visible. No new
inference was requested during that UI check. The active Codex app remained
running.


**0.5.0 Qwen Token Plan, OpenRouter and Gemini API — 13 September 2026**

The new provider work was committed in slices: Qwen (`c9df461`), OpenRouter
(`6869b61`), and Gemini (`ca0ae18`). The combined suite passes **226/226 tests**
under the bundled CPython 3.13.13 runtime with ResourceWarnings treated as
errors. The native Swift build passes. `PROVIDER-ADDITIONS.md` records each
contract, metadata source, implementation detail and qualification boundary.

Qwen uses only the dedicated Token Plan endpoint. Five exact context/output
limits are imported from TaskWraith/Pi's matching Token Plan catalogue with
source version and hash recorded. No number is guessed for the newer Qwen 3.8
Flash entry. OpenRouter's current metadata defines the curated membership,
context variants, routing endpoints and reasoning controls. Gemini uses the
official Models API and OpenAI-compatible Chat Completions, preserving exact
input/output limits and positional opaque thought signatures. Different Gemini
snapshot versions cannot collapse into aliases, and encrypted signature bytes
do not count as ordinary text in the gateway's input estimate.

The user authorized reuse of Qwen, OpenRouter and Gemini keys already saved by
TaskWraith. The selected three keys were copied into Provider Hub's own Keychain
entries. Their values were not printed or placed in source/build artifacts.
TaskWraith's source and encrypted credential files were not modified. Account
discovery returned six documented Qwen entries, eleven Gemini entries, and
between fifteen and seventeen OpenRouter context choices as endpoint status
changed. The installed Codex parser accepted the seventeen-choice OpenRouter
catalogue. The installed app also displayed all three new provider groups in
its Codex selector, without selecting a default or changing Codex configuration.

**Live gateway qualification**

Every successful cycle below used only a disposable `fixture.txt`, initially
`colour=blue`, and the supplied read/write tools. The model read the file,
changed it to green, read it back, and completed its final reply. No user
repository or private document was included in these requests.

| Route | Client interface | Provider requests | Summed input / output tokens | Result |
| --- | --- | --- | --- | --- |
| `openrouter/cohere/north-mini-code:free` | Messages JSON | 4 × HTTP 200 | 490 / 30 | Complete read/edit/read cycle |
| `openrouter/cohere/north-mini-code:free` | Streaming Responses | 4 × HTTP 200 | 490 / 30 | Complete read/edit/read cycle |
| `gemini/gemini-3.8-flash` | Messages JSON | 4 × HTTP 200 | 882 / 64 | Complete read/edit/read cycle |
| `gemini/gemini-3.8-flash` | Streaming Responses, paced retry | 4 × HTTP 200 | 882 / 64 | Complete read/edit/read cycle |
| `qwen-token-plan/qwen3.8-max` | Messages JSON | HTTP 429 | No usage reported | Weekly Token Plan quota exhausted |
| `qwen-token-plan/qwen3.8-max` | Streaming Responses | HTTP 429 | No usage reported | Same subscription quota response |

Gemini's initial Responses cycle completed the file operations but encountered
HTTP 429 before its final reply. That attempt remains recorded as incomplete.
A later retry with fifteen seconds between turns completed. Qwen's response
named the Token Plan weekly quota and a reset at **16 September, 02:03 UTC**.
No alternate billing endpoint was attempted. The user will verify its live
tool cycle when quota returns.

These live tests qualify the gateway protocols and the listed accounts/models.
They do not qualify every model in any provider catalogue or replace the
remaining actual Codex GUI restart/picker/context-meter/quit/restore check.

**Release review**

The final Sol Max review found that incomplete Gemini defaults fell back to
High in the Codex catalogue. Build 11 adds every established model default,
including Medium for 3.5 Flash, Minimal for 3.1 Flash-Lite and thinking off for
2.5 Flash-Lite. Gemini 2.5 Pro and Flash keep a null default because Google
documents dynamic thinking without a fixed default level. The expanded
regression covers all eleven currently listed Gemini models. The combined
226-test suite passed again in 39.365 seconds after that correction.

The installed `codex-cli 0.154.0-alpha.6.2` completed four entirely local mock
turns in disposable Codex homes. A Gemini 2.5 Pro-shaped row with a null default
sent `reasoning: {}` with no effort. Exact None, Minimal and Medium defaults
sent those exact effort values. The app-server's model list renders a null
default as the string `none` in its required suggestion field, even when None
is not a supported option; that display projection did not add an outbound
effort. Actual GUI presentation of this case remains part of the GUI check.

The installed app-server also accepted the complete build-11 catalogue: 108
choices across ten configured providers, including six Qwen, seventeen
OpenRouter context choices and eleven Gemini models. This was a model-list
check in a disposable Codex home, with no provider inference or change to the
user's normal Codex configuration. A final check using the installed app's
freshly refreshed cache accepted 107 choices, with sixteen OpenRouter routes
after another endpoint-status change; all eleven Gemini defaults were present.

**Muse Spark 1.3 effort ranks**

Spark 1.3 advertised ranks are Meta's current first-party values `minimal`,
`low`, `medium`, `high`, `xhigh`, and `max`. Claude Desktop Effort `xhigh` and
Max are forwarded as those Meta values when advertised; Ultra uses the highest
advertised Meta rank. Codex/ChatGPT catalogue rows publish the same ranks as
`supported_reasoning_levels`. A live `/v1/models` `effort_modes` list still
wins, so Contributor-tier or other narrower sets are not given Standard-tier
`max`. Muse Fast is not invented. `none` remains unsupported. Meta's cookbook
still treats `xhigh` as `high` and never mentions `max`; Hub follows the current
[reasoning](https://dev.meta.ai/docs/reasoning.md) page plus live list fields.

**Signed distribution**

Apple accepted **0.5.0 build 13** submission
`67958106-b904-473c-b5cb-be84743be4ae`, created at
`2026-09-13T17:00:25.830Z`. It is signed with a Developer ID Application
identity (signer name and team ID withheld; certificate SHA-1
`A5D4019DBFEDE7727487D49BD08257C46A72E7E0`). All twelve embedded native
components and the app use hardened runtime. The notarization ticket was
stapled successfully. The recipient archive was created after stapling and
extracted into a fresh directory. Strict deep signature verification, ticket
validation and Gatekeeper assessment all passed; Gatekeeper reported
`Notarized Developer ID`. The extracted ARM64 CPython 3.13.13 runtime loaded
its SSL trust store and passed an encryption/decryption check with
`cryptography` 50.0.0. All seventy-two packaged worker files match their source
files. The Swift `omitSystem`/`omitTools` bindings required explicit `self`
captures so `Source/build.sh` compiles with Swift 5.

The notarized build was copied to `/Applications/Provider Hub Preview.app` and
launched twice from that path. Gatekeeper accepted the installed copy as
`Notarized Developer ID`. The process is `MistralBridge` from the Applications
bundle, version 0.5.0 build 13. Claude Desktop and Codex were not restarted as
part of this packaging pass.

The earlier 0.5.0 build 12 submission `428429c2-e200-4cdd-b871-71b4837077de`
(`2026-09-13T13:18:07.679Z`) remains accepted; a later source rebuild does not
inherit that ticket.

The earlier 0.5.0 build 11 submission `76a51b1d-8569-4bf4-b64d-0233fb931319`
(`2026-09-13T03:05:34.268Z`) remains accepted; a later source rebuild does not
inherit that ticket.
