**Provider Hub integration status — 12 September 2026**

The preview implements seven model API connections behind one native menu bar app: Mistral, Kimi Code subscription API keys, Xiaomi MiMo Token Plan, the existing Ollama daemon, DeepSeek API, Cerebras API, and Muse through the Meta Model API. Provider identity, credentials, metadata, and quota attribution remain separate from TaskWraith-derived display branding.

The current implementation includes model discovery/provenance, exact context where known, readable names, alias grouping, Claude slot mappings, native and translated Messages streaming/tool history, per-provider account settings, effort/Fast compatibility handling, profile launch/recovery, and metadata-only activity logs. See `README.md` for setup and `VERIFICATION.md` for actual evidence.

Grok Build's browser-authenticated subscription path is ACP (`grok agent stdio`). Muse Code's subscription path is MSP (`muse serve`). Neither is established as a reusable raw model API token. TaskWraith hosts their agent sessions with lifecycle, permission, and cancellation management. A future delegated-agent tool can reuse that architecture, but must not re-present already-executed native tools as pending Claude tool calls. The Meta Model API-key route is implemented as Muse in 0.3.2; a separate xAI API-key route remains a possible addition. The detailed first-party research and TaskWraith file references are in `NATIVE-AGENTS.md`.

Claude Auto works through the normal gateway mapping, but the current client exposes no supported independent classifier model selector or reliable classifier request marker. Preserve the existing opt-in and native permission handling. Do not route by prompt heuristics or claim that enabling the selector qualifies an arbitrary model as a reviewer.

Remaining qualification work is account-specific: broader live Kimi/MiMo/DeepSeek/Cerebras qualification, every selected model's tool behavior, advanced native reasoning compatibility, Claude's provider-specific UI affordances, and any future delegated ACP/MSP integrations. Those capabilities must be reported from evidence as they are tested; catalogue presence alone is not a passing test.

**Proposed native-agent slices: Muse first**

These are implementation boundaries for follow-up work; native subscription
adapters are not included in the current model gateway. The
supported experience is a Muse or Grok agent hosted by Provider Hub, optionally
called as a delegated tool from Claude. The outer Claude conversation still
uses one of the configured model APIs.

| Slice | User-visible result | Evidence required before completion |
| --- | --- | --- |
| 1. Muse connection and model discovery | Detect the installed Muse CLI, use its normal sign-in, and show the host's advertised models/capabilities. | Complete MSP initialization and model listing without a model turn; verify host shutdown and unavailable-SDK diagnostics. |
| 2. Muse session lifecycle | Start a task in a selected working directory, stream its progress/result, cancel, and resume sessions owned by the hub. | A disposable read-only task, cancellation/process-close checks, reconnect/resume, and retry tests proving a lost reply does not submit a second turn. |
| 3. Muse approvals and native task UI | Show permission requests and agent questions in the app with explicit allow/deny/cancel controls and the existing Muse branding. | Disposable read/edit/read, denied write, user-question response, cancellation during an approval, and host-exit handling. |
| 4. Claude delegation | Claude can invoke an explicit Muse task tool and receive its result and execution summary. | A complete Claude-to-Muse task, continuation/cancellation, clear provider attribution, and no replay of already-executed Muse tools as pending Claude tool calls. |
| 5. Grok ACP adapter | Add Grok's existing-login agent sessions to the same native task UI and delegation layer. | ACP initialize/authenticate/session/prompt/update/permission/cancel lifecycle, normal local login, and the same fixture and failure cases as Muse. |

Muse's [official SDK quickstart](https://meta-models.github.io/muse-code-sdk/guides/quickstart/)
explicitly uses a configured Muse login and `muse serve`; it demonstrates
streaming, permission decisions, cancellation, and reloading a session in a new
process. It also documents a host exit when that build's experimental SDK tier
is unavailable. The first slice must detect that condition and report it.
Fingerprint differences should be surfaced and assessed against required
protocol capabilities; the SDK documents them as warnings, not an automatic
reason to reject every newer host.

Grok's [official ACP example](https://docs.x.ai/build/cli/headless-scripting#acp)
uses `grok agent stdio` and the CLI's existing local authentication. This is a
credible subscription-preserving integration path. Exact entitlement still
needs qualification with the selected account; no API-billing equivalence is
assumed.

Using either subscription as a transparent replacement for every model request
inside Claude's own agent loop remains unproven. The native-agent slices above
can be pursued through the published session protocols without extracting
browser credentials or disguising completed agent activity as model tool calls.


The 0.3.1 follow-up adds automatic catalogue refresh and launch preparation,
Cerebras client-identity compatibility, and direct DeepSeek/Cerebras tool-cycle
qualification. Remaining context work includes account-specific Cerebras limits,
Kimi K3 plan entitlement, and Claude's native standard/1M variant and compaction
behavior. The gateway cannot publish a fixed-only 1M choice through the inspected
Desktop schema; its supported default preference is now used.


Muse credential clarification: its subscription does have an automatically
connected onboarding API key. Meta scopes that key to Muse Code, while additional
or manually supplied keys use PAYG. Keep **Muse Code subscription (login/MSP)**
and **Meta Model API PAYG (API key/raw inference)** as explicit access products.
Native subscription launch must detect API-key overrides so an inherited key
cannot silently change billing. See `NATIVE-AGENTS.md` for the source references.


Version 0.3.2 adds **Muse (Meta Model API)** as a normal provider-key connection:
Keychain storage, authenticated model discovery, and native Anthropic Messages
through `api.meta.ai`. The native Muse Code subscription/MSP slices above remain
separate follow-up work. A configured Meta Model API key is needed for live
account qualification; offline protocol tests do not prove that entitlement.
