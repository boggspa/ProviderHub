**Provider Hub integration status — 13 September 2026**

Provider Hub Preview 0.5.0 implements eleven model API connections behind one
native menu bar app: Mistral, Kimi Code subscription API keys, Xiaomi MiMo
Token Plan, the existing Ollama daemon, DeepSeek API, Cerebras API, Muse through
the Meta Model API, Grok through the xAI PAYG API, Qwen Token Plan, curated OpenRouter, and Gemini API. Provider identity,
credentials, model metadata, and quota attribution remain separate from
TaskWraith-derived display branding.

The app now launches two desktop harnesses with separate model selection and
restoration state:

| Harness | Local protocol | Provider connections in 0.5.0 | Tool owner |
| --- | --- | --- | --- |
| Claude Desktop | Anthropic Messages at `/v1/messages` | All eleven | Claude executes returned tool calls |
| Codex / ChatGPT Desktop | Responses at `/v1/responses` | All eleven: native Grok/Ollama/OpenRouter; local Messages translation for the other eight connections | Codex executes returned function/shell calls |

The current implementation includes model discovery and provenance, exact
context where known, readable names, alias grouping, Claude slot mappings,
native and translated Messages streaming/tool history, native Responses
streaming/function history, Responses-to-Messages translation, authenticated
encrypted reasoning replay, per-provider account settings, provider-qualified
Codex catalogue IDs, documented effort/Fast controls, profile and configuration
launch/recovery, installed-runtime catalogue validation, cancellation, and
metadata-only activity logs. See `README.md` for setup, `HARNESS-OPTIONS.md` for
the Codex implementation and qualification, and `VERIFICATION.md` for the
broader evidence matrix.

Grok, Ollama and OpenRouter expose native Responses endpoints, so their protocol objects and
opaque reasoning pass through without a Messages translation. Mistral, Kimi,
MiMo, DeepSeek, Cerebras, Muse, Qwen Token Plan and Gemini translate Codex Responses through the
authenticated local Messages endpoint and their existing provider adapters.
The bridge requires full history and `store:false`; native xAI remains the only
route with scoped `previous_response_id` continuation. The inner Messages hop
owns the existing provider concurrency slot and activity record, avoiding
double accounting at the outer Responses layer.

Opaque provider thinking crosses the translated boundary in authenticated
encrypted Fernet envelopes. A separate persistent `responses-encryption-key`
is used through `cryptography` 50.0.0; no prompt or session store is added.
Envelope scope binds the model and account connection, and cross-provider or
cross-account reasoning history requires a new task. Mock JSON and streaming
function-tool cycles pass for all eight translated providers, including
Cerebras's signed thinking replay.

The custom Codex catalogue includes every compatible model published by the
configured account catalogues. OpenRouter publishes a curated shortlist with
separate context choices; there is no additional user inclusion checklist.
The selected default sets the starting model. Models explicitly marked as
tool-incompatible are omitted. Known numeric
contexts remain 100 percent with an 85 percent automatic-compaction threshold;
unknown contexts use `null` with no threshold. The installed engine accepted
that null contract and reported `model_context_window:null` rather than
guessing 200,000 tokens. The temporary Provider Hub catalogue replaces the
ordinary picker while active; it does not merge an OpenAI list with Hub models.

Each provider retains only its known effort controls. Grok also has an
explicitly labelled xAI Priority tier, while Ollama controls are not invented.
Installed app-server and CLI tests accepted real provider-qualified, non-GPT
slugs. Launch preparation requires the installed app-server to accept the
expected model IDs, names, effort levels, and service tiers in a disposable
home before any user configuration switch. A live Codex engine completed
read/edit/read through Provider Hub and `ollama/deepseek-v4-flash:cloud`; no
xAI key was configured, so Grok is not live account-qualified. The user's
normal Codex GUI/configuration was not switched, leaving the full GUI
restart/restore cycle as a user-operated final check.

Claude Auto mode still works through the ordinary Messages mapping. The
inspected Claude client exposes no supported independent classifier selector or
reliable classifier request marker. Preserve the existing opt-in and native
permission handling. Do not route by prompt heuristics or claim that enabling
Auto qualifies an arbitrary model as a reviewer.

Remaining model-route qualification is account-specific: broader live
Kimi/MiMo/DeepSeek/Cerebras coverage, every selected model's tool behavior,
advanced native reasoning compatibility, Claude's provider-specific UI
affordances, additional live Codex provider/model coverage, live xAI Responses, the Codex GUI
launch/restore path, and any future delegated ACP/MSP integrations. Catalogue
presence and mock protocol coverage are not live account tests.

**Shareable build status**

The prepared shareable bundle includes a clean ARM64 CPython 3.13.13 runtime
downloaded with `uv` in an isolated work directory and the pinned
`cryptography` 50.0.0 dependency. Python lookup prefers the bundle, and no Vibe
or other user `site-packages` are copied. Lightweight source builds can omit the
runtime; `PROVIDER_HUB_PYTHON_RUNTIME` supplies a clean relocatable runtime to
`Source/build.sh` when an embedded build is wanted.

`Source/package_macos.py` signs the app and embedded native components using a
supplied Developer ID Application identity and can optionally submit the zip
with a user-provided `notarytool` Keychain profile. The final 0.5.0 build 11 is
Developer ID signed and Apple-notarized. Its ticket is stapled, and Gatekeeper
accepts the app as `Notarized Developer ID`. This qualification applies to the
packaged artifact; a future build needs a new submission.

**Proposed native-agent slices: Muse first**

Grok Build's browser-authenticated subscription path is ACP
(`grok agent stdio`). Muse Code's subscription path is MSP (`muse serve`).
Neither login is established as a reusable raw model API token. TaskWraith
hosts these stateful agents with lifecycle, permission, and cancellation
management. A future delegated-agent tool can reuse that architecture, but it
must not re-present native tools that already executed as pending desktop tool
calls. The intended experience is a Muse or Grok agent hosted by Provider Hub,
optionally invoked as an explicit delegated tool from a desktop harness; the
outer conversation still uses one of the configured model API routes.

| Slice | User-visible result | Evidence required before completion |
| --- | --- | --- |
| 1. Muse connection and model discovery | Detect the installed Muse CLI, use its normal sign-in, and show the host's advertised models/capabilities. | Complete MSP initialization and model listing without a model turn; verify host shutdown and unavailable-SDK diagnostics. |
| 2. Muse session lifecycle | Start a task in a selected working directory, stream its progress/result, cancel, and resume sessions owned by the hub. | A disposable read-only task, cancellation/process-close checks, reconnect/resume, and retry tests proving a lost reply does not submit a second turn. |
| 3. Muse approvals and native task UI | Show permission requests and agent questions in the app with explicit allow/deny/cancel controls and the existing Muse branding. | Disposable read/edit/read, denied write, user-question response, cancellation during an approval, and host-exit handling. |
| 4. Desktop delegation | A desktop model can invoke an explicit Muse task tool and receive its result and execution summary. | A complete delegated Muse task, continuation/cancellation, clear provider attribution, and no replay of already-executed Muse tools as pending model tool calls. |
| 5. Grok ACP adapter | Add Grok's existing-login agent sessions to the same native task UI and delegation layer. | ACP initialize/authenticate/session/prompt/update/permission/cancel lifecycle, normal local login, and the same fixture and failure cases as Muse. |

Muse's [official SDK quickstart](https://meta-models.github.io/muse-code-sdk/guides/quickstart/)
uses a configured Muse login and `muse serve`; it demonstrates streaming,
permission decisions, cancellation, and reloading a session in a new process.
It also documents a host exit when that build's experimental SDK tier is
unavailable. The first slice must detect that condition and report it.
Fingerprint differences should be surfaced and assessed against required
protocol capabilities; the SDK documents them as warnings rather than an
automatic reason to reject every newer host.

Grok's [official ACP example](https://docs.x.ai/build/cli/headless-scripting#acp)
uses `grok agent stdio` and the CLI's existing local authentication. This is a
credible subscription-preserving integration path. Exact entitlement still
needs qualification with the selected account; API-billing equivalence is not
assumed.

Using either subscription as a transparent replacement for every request in a
desktop model's own agent loop remains unproven. The published session
protocols support an explicit native-agent boundary without extracting browser
credentials or disguising completed agent activity as model tool calls.

**Release history and retained boundaries**

Version 0.3.1 added automatic catalogue refresh and launch preparation,
Cerebras client-identity compatibility, and direct DeepSeek/Cerebras tool-cycle
qualification. Remaining context work includes account-specific Cerebras
limits, Kimi K3 plan entitlement, and Claude's standard/1M variant and
compaction behavior. The inspected Claude Desktop schema cannot publish a
fixed-only 1M choice, so Provider Hub uses its supported default preference.

Version 0.3.2 added **Muse (Meta Model API)** as a normal provider-key
connection with Keychain storage, authenticated model discovery, and native
Anthropic Messages through `api.meta.ai`. A Muse Code subscription does have an
automatically connected onboarding key, but Meta scopes that key to Muse Code;
additional or manually supplied Model API keys use PAYG. Keep **Muse Code
subscription (login/MSP)** and **Meta Model API PAYG (API key/raw inference)**
as explicit access products. Native subscription launch must detect API-key
overrides so an inherited key cannot silently change billing.

Version 0.3.3 added the Grok xAI API-key connection for PAYG inference through
Claude's Messages harness. Version 0.4.0 adds native Grok and Ollama Responses,
the six-provider Responses-to-Messages bridge, encrypted provider-reasoning
continuity, the full provider-qualified Codex catalogue, and the reversible
Codex / ChatGPT Desktop launcher. ACP/MSP subscription-agent hosting remains
future work.


**0.5.0 implemented additions**

Qwen Token Plan, curated OpenRouter and Gemini API are implemented in separate commits. See `PROVIDER-ADDITIONS.md` for endpoint isolation, context provenance, OpenRouter route binding and Gemini signature handling. Further additions should preserve the same per-provider qualification boundary.
