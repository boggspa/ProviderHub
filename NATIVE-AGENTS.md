# Native agent and Auto-mode integration boundaries

> **Historical record.** Research dated 12–13 September 2026. Version numbers,
> test counts and provider lists below are as they were then and are not
> maintained. For current state see [`README.md`](README.md).

Research date: 12 September 2026. The TaskWraith source notes below
refer to the read-only TaskWraith checkout at
`b6eba91e9216eefdd7a75572585e79b48ed4e1b0`. That research did not run an
ACP/MSP agent, read a provider credential, or live-test xAI. The separate 0.4.0
Codex qualification used an existing Ollama daemon and is recorded in
`HARNESS-OPTIONS.md`. The 13 September implementation follow-up added Codex
model-API access for the other six provider connections; it did not add an
ACP/MSP native-agent host.

## Product boundary

There are two distinct integrations:

1. A **model API provider** accepts inference requests owned by this gateway.
   Claude uses Messages across all eleven connections. Codex uses Responses
   across all eleven: native forwarding for Grok, Ollama and OpenRouter, and a local
   Responses-to-Messages bridge for Mistral, Kimi, MiMo, DeepSeek, Cerebras,
   Muse, Qwen Token Plan and Gemini. The selected desktop harness retains its
   own tool loop.
2. A **native agent provider** owns a stateful coding-agent session. It may
   inspect files, request permission, execute tools, and return a final answer.
   ACP and MSP expose that session; they do not turn it into a raw completion
   endpoint.

The Codex Messages bridge remains a model-API integration. Its encrypted
reasoning envelope preserves opaque provider state across a full-history tool
turn. `cryptography` 50.0.0 authenticates the envelope with the separate local
`responses-encryption-key`, scoped to the model and account connection. Only
that persistent key is stored by Provider Hub; the bridge does not host a
provider agent, resume an ACP/MSP session, or add a prompt/session store.
Reasoning history cannot cross to a different provider, model, or account, so
that change requires a new Codex task.

The current API connections are Mistral, Kimi, MiMo, Ollama, DeepSeek,
Cerebras, Muse through the Meta Model API, and Grok through xAI. Muse's ordinary
API-key route was implemented in 0.3.2, Grok's xAI API-key route in 0.3.3, and
native Grok/Ollama plus translated access to the other six connections for
Codex in 0.4.0. Reusing a flat-rate coding-agent subscription remains a
separate native-agent feature.

| Product and credential | Genuine model API? | Subscription-preserving integration | Current conclusion |
| --- | --- | --- | --- |
| Grok Build browser login / SuperGrok allowance | A separate xAI model API exists, but the cached Build login is not documented as a public API credential. | `grok agent stdio` over ACP | Native agent only unless the user separately configures an xAI API key. |
| xAI API key | Yes: Responses and Chat Completions, including `grok-4.6` and `grok-build-0.1`. | Not needed | Implemented for PAYG model inference: Chat Completions serves Claude, and native Responses serves Codex in 0.4.0. |
| Muse Code login / flat-rate Muse Code plan | A separate Meta Model API exists, but no public source inspected establishes that the Muse Code login is a reusable Model API key. | `muse serve` over MSP | Native agent only unless the user separately configures a Model API key. |
| Meta Model API key | Yes: OpenAI-compatible API at `https://api.meta.ai/v1`, including `muse-spark-1.3`. | Not needed | Implemented as Muse (Meta Model API) in 0.3.2, with Messages serving Claude directly and Codex through the 0.4.0 Responses bridge. |

## Why the Vibe connection uses a saved API key

Vibe 2.25.0 browser sign-in exchanges the completed browser flow for an API key,
then persists it as `MISTRAL_API_KEY`. Its Mistral backend uses that credential
with the ordinary `https://api.mistral.ai/v1` Chat API. The relevant installed
source modules are `setup/auth/http_browser_sign_in_gateway.py`,
`setup/auth/api_key_persistence.py`, and `core/llm/backend/mistral.py`.

Mistral's [API-key and profile documentation](https://docs.mistral.ai/vibe/code/cli/api-keys-profiles)
explicitly shares included monthly usage across Studio, its API, and Vibe Code.
Its [key-scope documentation](https://docs.mistral.ai/admin/identity-access/api-keys#api-key-scope)
associates API usage with the key's workspace. Provider Hub therefore keeps the
server-side billing identity of whichever configured key it resolves; it does
not prove or select a subscription tier itself.

The current resolver can select an environment key or a Vibe `.env` key before
the Keychain credential. Storage location is not enough to prove a particular
billing plan. The app now calls this option **Vibe saved API key** rather than
implying that it performs its own sign-in or verifies a subscription.

Muse also uses an API-key-backed account flow internally. The relevant
integration difference is the documented credential scope and billing product,
not a blanket technical distinction between API keys and OAuth. Meta scopes its
special subscription key to Muse Code; Grok's raw-model use of CLI OAuth remains
unverified. The supported native session integrations remain MSP and ACP.

## Grok Build

### Supported paths

xAI documents Grok Build as an interactive, headless, and ACP coding agent. The
current integration command is `grok agent stdio`; it exchanges ACP JSON-RPC
over stdin/stdout and can use the local browser-authenticated session. An
`XAI_API_KEY` is an alternate authentication path for non-browser automation.
See [Grok Build](https://docs.x.ai/build/overview),
[Headless and Scripting](https://docs.x.ai/build/cli/headless-scripting), and
the [CLI reference](https://docs.x.ai/build/cli/reference).

xAI also exposes genuine model APIs. Its current Build overview shows
`grok-4.6` on `POST https://api.x.ai/v1/responses`, and the model catalogue
lists `grok-build-0.1` as a token-billed API model. That establishes a direct
model-API contract for an xAI API key. Provider Hub uses Chat Completions for
Claude and native Responses for Codex; both require a separately supplied xAI
API key and API billing. This does not establish that an app may
extract or replay the browser session token as an API key. xAI's account FAQ
also says the Grok account is shared while Grok and xAI API billing are
separate. See [Grok 4.6 on the API](https://docs.x.ai/build/overview#use-grok-46-on-the-api),
[Grok Build 0.1](https://docs.x.ai/developers/models/grok-build-0.1), and
[xAI account and billing FAQ](https://docs.x.ai/console/faq/accounts#if-i-already-have-an-account-for-grok-can-i-use-the-same-account-for-api-access).

### TaskWraith evidence to reuse

TaskWraith's production Grok lane is a joined ACP child, not a Messages
translator:

- `src/main/grok/GrokCliArgs.ts` builds `grok --no-auto-update ... agent stdio`
  and forwards only genuine Grok model IDs and supported effort values.
- `src/main/grok/GrokAcpClient.ts` drives `initialize -> session/new ->
  session/prompt`, streams `session/update`, answers
  `session/request_permission`, sends cancellation through ACP, and waits for
  the real process-close event.
- `src/main/grok/GrokAcpProtocol.ts` validates the permission request and maps
  allow, deny, and cancellation to an ACP response. The client defaults to deny
  if no approval handler exists.
- `src/main/grokGate.ts` makes the joined ACP path mandatory for managed Grok
  runs. Persistent per-seat processes remain disabled there because their
  durable ownership and close receipts are not yet proven.

A future Provider Hub subscription integration should therefore expose Grok as
a delegated native-agent session. The host owns the child process, binds the
working directory, streams assistant and progress events, forwards each
supported permission request to the user, and closes or cancels the ACP session
explicitly. It may return the agent's final text and a typed activity summary
to an outer desktop conversation.

Already-executed Grok tool activity must stay an executed activity event. It
must not be converted to a pending outer-harness tool call, because the desktop
would then treat the call as still needing execution and could run it a second
time.

### Unknown or unsupported

- No inspected first-party document authorizes using a cached Grok Build OIDC
  token directly against `api.x.ai`.
- A SuperGrok allowance may cover Build usage in Grok's own products, but that
  does not make it an xAI API key or prove API billing equivalence.
- ACP session persistence beyond the documented start/prompt flow needs a
  version-pinned qualification before the menu app promises resume.
- The direct xAI API connection must use credentials supplied for that purpose;
  it must not search Grok's profile for secrets.

## Muse Code

### Supported paths

Meta now publishes a genuine model API. Its first-party cookbook uses the
OpenAI client with `base_url="https://api.meta.ai/v1"`, a `MODEL_API_KEY`, and
`muse-spark-1.3`; it documents streaming, tool calls, structured output,
reasoning, vision, and long context. See the
[Meta Model API cookbook](https://github.com/meta-models/meta-model-cookbook)
and its [API fundamentals](https://github.com/meta-models/meta-model-cookbook/tree/main/01_api_fundamentals).

Muse Code has a separate native session surface. The official SDK starts
`muse serve`, performs an MSP handshake, starts or resumes a session, sends a
turn, receives item and turn notifications, answers `approval/requested` with
`approval/decide`, and cancels through `turn/cancel`. The quickstart requires a
configured `muse` binary and says to run `muse` once and log in, or deliberately
point the process at a profile that already has credentials. See the
[Muse Code SDK](https://github.com/meta-models/muse-code-sdk) and
[MSP quickstart](https://meta-models.github.io/muse-code-sdk/guides/quickstart/).

The SDK is explicitly a developer preview and pre-1.0. Its schema fingerprint,
supported methods, and host version must therefore be checked at connection
time rather than assumed from the installed command name.

Muse Code subscriptions do issue an API key: Meta describes a special key
automatically connected during CLI onboarding. The subscription documentation
scopes that credential to Muse Code; additional API keys created under the same
account use pay-as-you-go billing. Key existence therefore does not establish a
supported third-party subscription endpoint. See
[Muse Code subscriptions](https://dev.meta.ai/docs/muse-code/subscriptions/).

The authentication documentation and installed Muse CLI distinguish account
login from manually supplied keys. `META_API_KEY` and manually stored API keys
take precedence over account login and use the API-key billing lane. A native
subscription adapter must verify the effective authentication source instead of
accidentally injecting an inherited PAYG key. See
[Muse authentication and billing](https://dev.meta.ai/docs/muse-code/auth/).
These documentation pages require login in their ordinary web view.

The products remain explicit: **Muse Code subscription** uses the supported
Muse login/MSP path as proposed follow-up work; **Muse (Meta Model API)** is the
implemented user-created API-key connection for raw inference in 0.3.2 and is
available to both desktop harnesses in 0.4.0. The automatically connected
subscription key is not a supported general-purpose BYOK option unless Meta
publishes an external-client contract for it. This corrects the earlier
uncertainty about whether the subscription has a key, while retaining the
documented client and billing distinction.

### TaskWraith evidence to reuse

TaskWraith's Muse implementation shows the required session semantics:

- `src/main/muse/MuseMspRun.ts` launches a `muse serve` process in an isolated
  seat, sends the exact provider and model to `session/start`, forwards
  `turn/start`, consumes live token/context usage, and waits for a terminal
  result.
- `src/main/muse/MuseMspClient.ts` implements handshake, session start/resume,
  turn lifecycle, permission decisions, cancellation, and gap handling.
- `src/main/muse/MuseIpcBridge.ts` projects native Muse events into the app and
  only falls back to `muse exec --json` when an old CLI fails before a session
  or event exists. It never silently re-runs a turn that may have started.
- `src/main/museGate.ts` records that write/shell sandbox posture belongs to the
  `muse serve` host process, while approval mode is per session. Read-only and
  write-capable seats must therefore use separate hosts.

For this app, the safest subscription-preserving design is to let the installed
Muse CLI use its supported login state, or let the user log in to a dedicated
Muse home. Do not parse a personal `auth.json`, copy tokens into app settings,
or claim that a CLI login is a Model API key. MSP results and native tool
activity remain delegated-agent results, never pending Claude tool calls.

### Unknown or unsupported

- No inspected public source proves that a Muse Code subscription credential
  can authenticate `https://api.meta.ai/v1` outside the Muse host.
- Exact subscription quotas still require the selected account's current plan.
  Additional/manual API keys are documented as PAYG; do not infer subscription
  coverage from key format or account ownership.
- The MSP SDK is a developer preview. Pin a supported version/fingerprint and
  surface protocol mismatches instead of guessing compatibility.
- TaskWraith's credential projection into an isolated home is useful protocol
  evidence, but it is not authority for this app to extract a user's credential.

## Claude Desktop Auto-mode classifier

### Result

There is no supported profile or user-facing setting in the inspected build
that elects an independent adapter-native reviewer model for Auto mode.
`autoModeEnabled` only makes Auto mode available in the permission selector.
It does not choose the classifier, its endpoint, or its model.

This conclusion is based on the installed, signed builds:

- Claude Desktop `1.52386.3`
- Claude Desktop's managed Claude Code `2.1.266`, at
  `~/Library/Application Support/Claude-3p/claude-code/2.1.266/claude.app/Contents/MacOS/claude`
- the separately installed Claude Code `2.1.268`, at
  `~/.local/share/claude/versions/2.1.268`

The two Claude Code binaries were inspected separately. They have the same
Auto request-role and dispatch-header behavior described below; the newer
standalone binary was not treated as if it were the executable Desktop
launches.

Claude Desktop's third-party profile schema describes `autoModeEnabled` as
offering Auto/Automatically approve. On session launch the app passes
`permissionMode: "auto"` and a `settings.autoMode` object containing the
classifier's `environment`, `soft_deny`, and `allow` prose rules. The latter is
rule configuration, not model routing. The app only sends those rules to a CLI
new enough to accept them and keeps approval cards if the push fails.

The Claude Code binary chooses its classifier from server-controlled
`tengu_auto_mode_config.modelByMainModel` or `.model`, after applying the
organization’s model policy, then from internal provider/model fallbacks. Its
public documentation likewise says the classifier is server-configured and
independent of the session's `/model` selection. See
[permission modes](https://code.claude.com/docs/en/permission-modes#eliminate-prompts-with-auto-mode)
and [Auto-mode configuration](https://code.claude.com/docs/en/auto-mode-config).

The current Auto-mode configuration reference says Auto is available to all
users on every provider, including Bedrock, Claude Platform on AWS, Google
Cloud's Agent Platform, Foundry, and signed-in Claude apps gateway sessions.
It also says the historical `CLAUDE_CODE_ENABLE_AUTO_MODE=1` requirement for
those providers was removed in Claude Code 2.1.207. Provider availability and
reviewer-model election are separate questions: broad provider availability
does not expose a classifier routing control.

Both binaries contain an environment-schema name
`CLAUDE_CODE_AUTO_MODE_MODEL`, but the external 2.1.268 classifier selector
does not read it, and the Desktop-managed 2.1.266 selector has the same
behavior. Desktop also strips that name from host-managed provider environment
overlays. It is therefore not a supported routing hook in either inspected
build and should not be written to a profile.

### Request path

The classifier uses Claude Code's normal model request machinery and the
session's resolved provider credentials. For a Gateway profile, that means the
same gateway base URL and authentication scheme as the main conversation. The
current profile shape has no second classifier URL or classifier credential.
The gateway protocol is Anthropic Messages at `/v1/messages` and must preserve
`anthropic-version` and `anthropic-beta`; Claude Code also sends its documented
session attribution header. See the
[LLM gateway requirements](https://code.claude.com/docs/en/llm-gateway#gateway-requirements).

In Desktop-managed 2.1.266 and standalone 2.1.268, the classifier call is
built locally with
`querySource: "auto_mode"`, a model field, and the same credential object used
by the normal request client. The default two-stage implementation uses a short
first verdict request and, when needed, a larger reasoning request.

`querySource` is **not a reliable wire marker**. The final Messages body does
not contain `querySource`, `query_source`, or another role field derived from
it. The generic request path can add an `anthropic-dispatch-id` header, but only
for a direct first-party Anthropic endpoint; a Gateway profile fails that
first-party condition. Auto-mode calls are also classified locally as
`auxiliary`, for which the normal dispatch-header selector returns no value.
An experimental generic `v2d` dispatch value can override that behavior, but
it is neither Auto-specific nor available as a stable gateway contract. For a
non-first-party gateway the inspected code also adds no Auto-specific beta
header. `querySource` remains useful for local retries, tracing, telemetry, and
cache policy, but it does not let this gateway distinguish an Auto classifier
request from another Messages request.

### What Ollama actually propagates

The Ollama launcher source in the supplied research snapshot writes this
third-party profile:

- `inferenceProvider: "gateway"`
- `inferenceGatewayBaseUrl: "http://127.0.0.1:11435"` (its configured local
  gateway address)
- bearer gateway credential
- `autoModeEnabled: true|false`

It also publishes fixed Claude-facing model route IDs such as
`claude-sonnet-5` and maps them to Ollama models. It does not set a classifier
model or classifier URL. When Auto mode causes Claude Code to request one of
those fixed classifier IDs, the ordinary gateway mapping handles it. This is
route propagation, not an independently elected reviewer contract.

### Consequence for Provider Hub

A gateway can mechanically reserve a Claude-facing model ID that Claude Code
happens to select for the classifier and map that ID to a different upstream
provider/model. That works only while the classifier ID is distinct from the
main model ID. If the primary and reviewer share an ID, the wire carries no
stable classifier marker and independent election is impossible without
guessing from prompt or request shape. Claude Code owns classifier-model
selection and may change the chosen ID through server configuration or an app
update.

The current classifier expects a strict verdict protocol and has fail-closed
parsing and retry behavior. Auto mode's documented provider availability does
not prove that an arbitrary elected non-Claude model satisfies that protocol.
No live qualification in this review establishes Grok, Muse, Ollama, or
another non-Claude model as a separately selectable reviewer.

Provider Hub should therefore:

1. Preserve the existing `autoModeEnabled` opt-in for every configured
   provider profile; do not remove or gate Auto solely by provider identity.
2. Keep `autoModeEnabled` separate from model routing in settings and status.
3. Avoid an "Auto reviewer provider" picker until Anthropic exposes a supported
   classifier-model hook or a specific adapter/model passes a version-pinned,
   end-to-end qualification with known request and verdict semantics.
4. Never infer classifier role from prompt text, token limits, system content,
   or other request-shape heuristics. Those are not a routing contract.
5. Never infer classifier support from a provider's ability to answer ordinary
   Messages requests, from a Haiku/cheap-model mapping, or from the mere
   presence of `autoModeEnabled`.
6. If Claude Code chooses a distinct classifier model route, record the exact
   Claude-facing classifier route and the upstream model that actually served
   it as observed metadata, while retaining normal approval behavior when the
   classifier cannot return a verdict.

## Implementation recommendation

Keep the native-agent layer beside, not inside, the model gateway:

```text
Desktop model harness -> selected model API provider -> model response/tool call
                     \-> explicit native-agent tool -> ACP/MSP session -> executed activity/result
```

Each native adapter should own process discovery, supported login handoff,
protocol handshake, model catalogue, session resume, streaming, permission
requests, cancellation, and close evidence. The shared layer should own only
working-directory selection, lifecycle state, user-visible approvals, and an
honest result envelope. Credentials remain with the provider's supported login
or with a separately supplied API key for the model-API route.


## Follow-up commit boundaries

`PROVIDER-ROADMAP.md` now records a Muse-first implementation sequence with
separate connection, lifecycle, approvals/UI, desktop delegation, and Grok-ACP
slices. Those native adapters are proposed follow-up work, not part of the
current model gateway or the 0.4.0 all-provider Codex Responses harness.

The official Muse quickstart explicitly supports normal configured Muse login
and `muse serve`. It documents SDK-tier-unavailable host exit code 5 and states
that a differing schema fingerprint is a warning. An adapter should therefore
check the required host capabilities, report version/fingerprint differences,
and verify compatibility rather than treating every fingerprint difference as
an automatic failure. See the [SDK quickstart](https://meta-models.github.io/muse-code-sdk/guides/quickstart/).


Provider Hub 0.5.0 also adds Qwen Token Plan and Gemini API as model-API connections and OpenRouter with native Messages/Responses. These do not introduce CLI delegation or alter the native-agent boundary above.
