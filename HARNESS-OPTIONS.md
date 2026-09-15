**Provider Hub: Codex / ChatGPT Desktop harness — 13 September 2026**

Provider Hub Preview 0.5.0 extends the second desktop harness from 0.4.0 with Qwen Token Plan, curated OpenRouter and Gemini API. Every configured provider connection is now
available to Codex. Grok, Ollama and OpenRouter retain native Responses forwarding;
Mistral, Kimi, MiMo, DeepSeek, Cerebras, Muse, Qwen Token Plan and Gemini use a local
Responses-to-Messages translation over their existing provider adapters. This
document records the shipped design and the evidence gathered so far, including
the checks that remain user-operated.

**Installed app and configuration contract**

The local desktop bundle is `/Applications/ChatGPT.app`, version
`26.908.40834`, with display name ChatGPT and bundle identifier
`com.openai.codex`. The bundle is ChatGPT-branded while its local task harness,
configuration, and process identity remain Codex. Provider Hub therefore uses
**Codex / ChatGPT Desktop** in user-facing labels.

Installed Ollama `0.33.3` advertises `ollama launch chatgpt` and accepts
`codex-app`, `codex-desktop`, and `codex-gui` as aliases. Its distinct `codex`
integration launches the CLI. The public desktop guide still uses
`ollama launch codex-app` and documents a restore command. This confirmed that
the installed desktop accepts a custom Responses provider and model catalogue;
the app-server qualification below additionally proved that model IDs do not
need GPT-shaped aliases.

Sources: [Ollama desktop guide](https://docs.ollama.com/integrations/codex-app),
[matching Ollama 0.33.3 implementation](https://github.com/ollama/ollama/blob/v0.33.3/cmd/launch/codex_app.go),
[OpenAI custom providers and profiles](https://learn.chatgpt.com/docs/config-file/config-advanced),
and [OpenAI configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

The initial installed-app investigation used version/help commands only. It did
not run Ollama's launcher or read or change the user's OpenAI configuration or
credentials. Later Provider Hub qualification used disposable configuration;
the user's normal Codex GUI/configuration still has not been switched.

**Implemented protocol boundary**

```text
Claude Desktop -> /v1/messages -> Provider Hub -> all eleven provider connections

Codex / ChatGPT Desktop -> /v1/responses -> Provider Hub -+-> native Responses -> Grok, Ollama or OpenRouter
                                                           \-> local /v1/messages -> other eight connections
```

The native Responses path does not translate through the existing Messages or
Chat Completions adapters. It validates the Codex request, replaces the
provider-qualified catalogue slug with the provider's exact model ID, relays
the native request, and restores the qualified slug on returned response
objects. Streaming and non-streaming responses preserve function calls and
outputs, full-history items, opaque provider reasoning items, terminal status,
final usage, provider errors, and client/gateway cancellation. The local
`client_metadata` field is consumed at the hub boundary and is not sent to the
provider.

The translated path converts Responses input and function history to the
existing local Messages contract, then converts JSON or streaming Messages
results back into Responses events and terminal objects. The loopback hop uses
the hub's local bearer credential. The inner Messages request owns the existing
provider concurrency slot and activity record, so a translated turn is bounded
and counted once. Closing the outer Responses request propagates cancellation
through the local Messages request to the provider connection.

This route enables only function tools and function namespaces.
Provider Hub deterministically flattens a namespaced Codex function into a
provider-safe name and reverses that mapping on returned calls. A collision is
rejected. Hosted tools, including `web_search`, are disabled in the generated
profile. The dedicated free-form `apply_patch` catalogue metadata is `null`
by default, so the installed Codex engine's function and shell path inspects
and edits files and the desktop shows no close-out diff card. The Codex tab's
**Offer apply_patch to catalogue models** switch (`codex_apply_patch_all`)
advertises `freeform` for every catalogue route, minus any
`codex_apply_patch_exclude` entries; Provider Hub projects the tool as a JSON
`apply_patch(patch)` function and converts the calls back (see
`CLOSEOUT_CARDS.md`). The per-route `codex_apply_patch` list still qualifies
individual routes while the switch is off.

Provider-specific continuation rules remain explicit:

- Ollama, OpenRouter and all eight translated providers require full input history and
  `store:false`. Provider Hub rejects `previous_response_id` and storage for
  these routes. It does not advertise unproven Ollama effort or service-tier
  controls.
- Grok also defaults to `store:false`. If a client explicitly sends
  `store:true`, Provider Hub can accept a later `previous_response_id` only
  after that ID was returned for the same provider model, connection, and
  credential scope. The local record contains ID/ownership/time metadata, not
  prompts or responses, and is capped at 10,000 IDs.
- Grok exposes only documented effort levels for the selected model. The
  additional speed choice is labelled **Fast · xAI Priority**, including its
  premium-rate meaning. Ollama receives no guessed effort or Fast controls.

Native xAI is the only route that supports response-ID continuation. Changing
provider, model, or account on a full-history route requires a new task when the
history contains provider reasoning state.

Messages providers can return opaque `thinking` or `redacted_thinking` blocks
that must survive a function-tool round trip. Provider Hub carries them in the
Responses `encrypted_content` field as authenticated Fernet envelopes. The
separate persistent `responses-encryption-key` is consumed by `cryptography`
50.0.0; only the key is stored in Provider Hub state, not prompts, response
content, reasoning, or session history. Each envelope is scoped to the exact
model and account connection and fails closed if edited or replayed across a
different scope. Cerebras mock fixtures cover both JSON and streaming signed
thinking replay through this envelope.

**Model catalogue**

The generated catalogue uses exact provider-qualified slugs, for example
`grok/grok-4.6`, `mistral/mistral-vibe-cli-latest`, and
`ollama/small-model:latest`, with friendly provider-aware labels. The upstream
request uses the provider's exact model ID, while the Codex-facing response
retains the catalogue slug. No OpenAI model aliases are introduced.

By default, every compatible model published by the configured account
catalogues appears. OpenRouter publishes a curated shortlist with separate
context choices. The selected default sets the starting model.

**Codex catalogue curation**

Provider Hub 0.5.0 adds an optional Codex catalogue selection on the Codex page.
Switch the **Catalogue** mode from **All compatible models** (the default)
to **Custom selection** and choose which of your configured provider models
should appear in the Codex picker. The curated list:

- Contains only the exact provider-qualified routes you select
- Excludes models without coding-tool support (they are listed under
  exclusions with an honest reason)
- Shows friendly labels and known context limits in the setup card
- Maintains the same default-model picker and starting behaviour
- Can be saved while Claude is live on the gateway; launching Codex
  briefly restarts the gateway so both desktop harnesses share the new
  snapshot, and the live app reconnects automatically

Missing curated routes (e.g., after a provider refresh that removes a model)
are reported on the Codex page and block `codex-prepare` until you refresh
or remove them. While the Provider Hub configuration is active, the catalogue
replaces the ordinary picker list rather than merging with OpenAI models.

For a known numeric context, `context_window` and `max_context_window` contain
the full value, `effective_context_window_percent` is 100, and automatic
compaction is set to 85 percent. When the provider does not establish a numeric
limit, the context values and automatic-compaction threshold are `null` rather
than an invented 200,000 tokens; the picker tells the user to compact manually
when needed. Each provider publishes only its known effort levels. Muse Spark 1.3
publishes Meta's current first-party ranks including distinct `xhigh` and `max`,
without inventing Fast. Grok also
has an explicitly labelled Priority service tier when that model advertises
same-model Fast. Thinking-capable Ollama models publish their think ranks;
speed/Fast is still omitted unless a same-model Fast control exists. ChatGPT's compact Power slider is not given fake
GPT-only ranks. After a custom catalogue model is selected, that control and
the advanced Effort menu use `supported_reasoning_levels`; Fast uses the
model's `service_tiers` when a same-model Fast control exists.

An **Ultra** slider position is synthesized for every reasoning-capable model
as an alias for that model's top advertised rank, so the slider is available
even when a provider does not name an ultra level natively. The gateway maps
ultra onto the provider's highest advertised reasoning rank at request time;
above-range ranks that a model does not support natively (xhigh, max, ultra)
are capped to its top advertised rank with a `normalized_to_` compatibility
note rather than rejected. Below-range requests still fail closed.

For reasoning-capable models, the catalogue also advertises
`multi_agent_version: "v2"` and `multi_agent_reasoning_effort: "xhigh"`. On
activation, Provider Hub writes `default_subagent_model` and
`default_subagent_reasoning_effort` to point the Codex multi-agent runtime at
the selected hub catalogue route, and enables `features.multi_agent_v2` in
the Codex `config.toml`. This lets the Ultra slider position opt into
autonomous sub-agent orchestration using the configured provider connection.
The `multi_agent_mode` hint is left at its default so selecting Ultra enables
the capability without forcing proactive delegation. Non-reasoning routes
receive no subagent keys or feature flag. `model_reasoning_effort` is cleared
on activation so the catalogue's `default_reasoning_level` governs the
starting slider position; the user's prior value is saved in the journal and
restored on quit. The installed app-server verified that `multiAgentVersion`
is echoed back by the runtime in `model/list`.

The installed app-server loaded real non-GPT IDs `grok/grok-4.6` and
`ollama/small-model:latest`, returned their intended friendly labels, and
exposed the expected context, effort, and Priority metadata. This directly
answers the compatibility question: provider-qualified, non-GPT model slugs are
accepted by the installed Codex harness. A separate installed-engine check
loaded a model with `context_window:null` and reported
`model_context_window:null`; it did not substitute a 200,000-token default.

**Launch, ownership, and restoration**

`Source/CodexHarness.swift` adds a **Codex** page beside **Claude** and owns the
desktop launch/recovery flow. It prepares the selected route and generated
catalogue, then launches the installed app bundle's
`Contents/Resources/codex` runtime in `app-server` mode with a disposable Codex
home. Its `model/list` result must contain the expected provider-qualified IDs,
friendly names, effort levels, and service tiers. This validation submits no
turn or inference request and does not edit the user's Codex configuration.
Provider Hub writes a prepared runtime signature only after the installed
runtime accepts the catalogue.

The launcher then starts and verifies the shared gateway before asking whether
to restart an already-running Codex / ChatGPT Desktop. Activation rechecks the
installed-runtime signature and gateway catalogue digest before configuration
switching. An incompatible catalogue, including one that silently falls back
to OpenAI defaults, fails before any user configuration is edited. A cancelled
restart leaves the desktop configuration unchanged; the gateway can stop when
no other owned harness or active request is using it.

**Simultaneous use**

Claude and Codex can now both run on the same gateway. While one desktop
harness is live, you can still change the *other* harness's selection:

- Changing the Codex catalogue or default while Claude is live: Save writes
the new selection without stopping the gateway. When you launch Codex, the
app briefly restarts the gateway so both harnesses share the new snapshot;
Claude reconnects automatically.
- Changing Claude mappings while Codex is live: Save writes the new mappings,
and launching Claude briefly restarts the gateway; Codex reconnects.
- Changing shared provider settings (keys, regions, port, branding) while
*either* harness is live still requires quitting both desktop apps first.

Activation edits the user's selected Codex `config.toml` and owns only the
selected model, catalogue, context, reasoning, verbosity, service-tier, and web
search root keys plus a `provider_hub` provider stanza. Vendored `tomlkit`
0.13.3 preserves TOML structure and unrelated content. Provider Hub writes an
atomic verified backup, generated catalogue, and recovery journal before
switching. Restore returns owned settings to their prior values and preserves
later unrelated edits or external provider/model changes. It refuses to
overwrite a pre-existing external `provider_hub` stanza.

The custom provider uses `wire_api = "responses"` and a command-backed bearer
helper for the loopback gateway. Provider API keys stay in Provider Hub's
existing Keychain entries. Codex conversation, authentication, project, and
skill files are not read or modified by the launcher.

Claude and Codex share one prepared gateway. It remains running while either
owned desktop harness is open and is eligible to stop only after both have
closed and active requests are finished. Each harness has its own restoration
journal. Codex launch manages the installed app through a restart; it does not
claim a separately isolated simultaneous GUI instance.

**Qualification evidence and limits**

The installed Codex CLI completed a disposable read/edit/read cycle against a
mocked native Responses provider. That path preserved the exact
500,000-token `grok/grok-4.6` context and completed a simulated context-pressure
compaction turn through ordinary Responses calls. A namespaced function-call
round trip reached the expected local dispatch path; its harmless invalid-agent
wait created no agent.

The six translated provider paths introduced in 0.4.0 pass both JSON and streaming mock
function-tool cycles through the real local Messages adapter. Those fixtures
cover Mistral, Kimi, MiMo, DeepSeek, Muse, and Cerebras, including the
authenticated encrypted reasoning round trip required by Cerebras. They prove
the translation and local lifecycle behavior; they do not constitute live
account qualification for every model in those catalogues.

A live installed Codex engine then ran through Provider Hub, the existing
Ollama daemon, and `deepseek-v4-flash:cloud`. It completed the disposable
read/edit/read cycle with three HTTP 200 Responses requests, reported 16,486
input tokens and 135 output tokens, showed the advertised 1,048,576-token
context, and finished in 5.3 seconds. This qualifies that exact Ollama route,
daemon, and account state. It does not qualify every Ollama model.

No xAI key was configured, so Grok has protocol, catalogue, and mocked endpoint
coverage but no live account qualification. The user's normal Codex GUI and
configuration were not switched during development. A full GUI launch,
restart, visual picker check, quit, and restore against the user's actual setup
remain a final user-operated check; no such GUI result is claimed here. This
harness also does not replace inference in ChatGPT cloud chat, voice, or other
product surfaces that have no custom-provider contract.

The installed Codex engine also completed a live translated read/edit/read
cycle with `cerebras/gpt-oss-120b` at 00:18 BST on 13 September. All three
provider requests returned HTTP 200, with 13,250 input tokens and 262 output
tokens reported across the complete cycle. The engine reported the exact
131,072-token context throughout and finished in 2.0 seconds. This qualifies
the Responses-to-Messages path, Cerebras reasoning replay, and that specific
account/model; other Cerebras models still require their own qualification.

One harness caution: never pipe the Mistral Vibe CLI through `head`. On a closed
pipe the CLI busy-spins at 100% CPU and ignores SIGTERM (15 September 2026:
`vibe models 2>&1 | head -30` left PID 44574 spinning for ~40 hours until SIGKILL).
Capture full output and truncate after the CLI exits.

**Shareable bundle and signing status**

The shareable app can embed a clean relocatable Python runtime so its Messages
translation and Fernet dependency do not rely on the recipient's development
environment. The prepared runtime is ARM64 CPython 3.13.13, downloaded with
`uv` in an isolated work directory and packaged with `cryptography` 50.0.0. It
does not copy Vibe or another user installation's `site-packages`. The app's
Python lookup prefers the bundled runtime. A lightweight source build can omit
it; setting `PROVIDER_HUB_PYTHON_RUNTIME` when running `Source/build.sh` embeds
the supplied clean runtime.

`Source/package_macos.py` accepts a Developer ID Application identity, signs
embedded native components and the app, verifies the signature, and creates a
zip archive. It can optionally submit that archive using a user-provided
`notarytool` Keychain profile. The final 0.5.0 build 13 submission is recorded
in `VERIFICATION.md`. Its ticket is stapled, strict signature
verification passes, and Gatekeeper identifies it as `Notarized Developer ID`.
The recipient zip is recreated after stapling. A later source rebuild requires
its own signing and notarization.


**0.5.0 provider expansion**

Qwen Token Plan, OpenRouter and Gemini API are available to both harnesses. OpenRouter uses native Responses and publishes context-specific routes; Qwen and Gemini use the existing local Messages bridge. Provider-specific contract and qualification details are in `PROVIDER-ADDITIONS.md`.
