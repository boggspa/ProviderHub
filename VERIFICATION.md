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

Two pre-inference harness attempts exposed the native Effort/adaptive normalization issue and were rejected locally before any model inference. Those failures were fixed and covered by regression tests before the successful run.

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
truthful 1M capability always expands to standard plus 1M rows, including static
profiles. A suffixed discovery ID creates incorrect double expansion. The
supported `modelPrefer1mContext` preference changes defaults only and retains
saved selections. No duplicate-suppression workaround or false capability flag
was introduced; exact-context and generic Kimi plan limitations remain visible.


**0.3.2 Muse API and version labels**

Muse is added as a Meta Model API-key connection through native Anthropic
Messages. Model discovery is authenticated and account-scoped; only IDs actually
returned by the account list are published. Meta's first-party documentation
enriches the exact `muse-spark-1.3` ID with 1,048,576 context and 131,072 maximum
output. Other model IDs retain separate metadata and unknown values remain
unknown.

Ten provider tests and two complete HTTP JSON/SSE tool-cycle tests cover the
new route, including bearer authentication, local-token separation, exact model
IDs, native thinking/signatures, adaptive thinking, and Claude Max mapping to
Meta High. Forced tool choices, unsupported reasoning disable, and Fast fail
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
