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
`2026-09-13T17:00:25.830Z`. It is signed with Developer ID Application
`Christopher Izatt (8CZML8FK2D)` (certificate SHA-1
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
