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

**Desktop briefing and image-capable routes**

Codex resolves a catalogue flag it is not given to `false`, so a projected
route read a thinner prompt than a native row on the same machine: its skills
were still listed, but the progressive-disclosure how-to, the plugin briefing,
and the strict auto-review of `node_repl` JavaScript were absent. The
projection now states `include_skills_usage_instructions`,
`include_plugin_usage_instructions`, `include_apps_usage_instructions`, and
`node_repl_auto_review_required` for every route. The installed 26.908.70816
runtime accepts and echoes all four through `codex debug models`, and
`codex debug prompt-input` against a disposable `CODEX_HOME` shows the restored
"How to use skills" block for a hub route.

The bundled Computer Use and Browser Use plugins are wired per install rather
than per provider: the desktop app writes their `node_repl` and `cua_repl` MCP
servers into the Codex configuration at launch, and it keeps doing so while the
hub provider is selected, so a projected route is handed the `js` tools exactly
like a native row. Only the screenshots need image input — `sky.get_app_state`
returns an accessibility tree as text — and Codex delivers a screenshot as an
`input_image` inside the tool result, where the parts live under `output`
rather than `content`. The native Responses path checks both fields, so a route
whose catalogue entry advertises text alone is refused at this gateway with a
clear message instead of failing at the provider.

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
compaction is set to 85 percent, and the gateway's own compaction (which
drops whole tool cycles and keeps a contiguous tail, with a per-route estimate
calibrated from the provider's reported input counts) remains as a backstop
behind it. When the provider does not establish a numeric
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

Any route that publishes a reasoning ladder advertises
`multi_agent_version: "v2"`, together with a `multi_agent_reasoning_effort`
taken from that model's own top advertised rank rather than a fixed value.
This used to require a reasoning flag as well, which withheld the runtime
from every plain instruct route for no gain: the collaboration tools and the
multi-agent briefing arrive from the desktop's own `features.multi_agent_v2`
either way. On activation, Provider Hub enables that feature flag in the
Codex `config.toml`. It deliberately does **not** write
`default_subagent_model` or `default_subagent_reasoning_effort`, and clears
them when an older journal left them behind: both resolve once at
activation, but the model is switched in the app and an in-app switch never
re-runs activation, so a pinned sub-agent target goes stale the moment the
user changes route. Codex's own `spawn_agent` contract prefers the inherited
parent model, so with no default the children of a Mistral thread are
Mistral and the children of a DeepSeek thread are DeepSeek; the model can
still name a different route per sub-agent through `spawn_agent`'s `model`
argument. Together these let the Ultra slider position opt into autonomous
sub-agent orchestration using the configured provider connection.

The `multi_agent_mode` hint is left at its default, so Ultra never forces
delegation on every turn. Advertising the runtime is not the same as asking
for it, though, and a slider position that changes nothing a model does is
the same as no slider. So when Ultra is the rank the user selected **and**
the request still carries the collaboration spawn tool, the gateway appends
a short note to the request `instructions` asking the model to run
investigation, implementation and verification as sub-agents, and to answer
solo on conversational or trivial turns. This is the Codex counterpart of
the Claude tab's Ultracode orchestration note. The note names the spawn tool
by the name it actually travels under, not the name Codex declares it with,
because instructions pointing at an uncallable name produce no delegation at
all. It is withheld whenever the spawn tool is absent, so a sub-agent
forbidden to spawn again, and any provider whose `spawn_depth_limit` is `0`,
is never told to call a tool it was not handed. A route with no published
ladder receives no multi-agent keys or feature flag.

`model_reasoning_effort` is cleared on activation so the catalogue's
`default_reasoning_level` governs the starting slider position; the user's prior value is saved in the journal and
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

**Power-slider provider accents (opt-in)**

The Codex composer paints its power slider with one app-wide design token
(`--color-chart-blue`); model records carry no colour and the app has no
theming hook, so the hub cannot colour the slider through the catalogue.
With the Codex-tab switch on, the worker's `codex-accent` command starts the
app's executable as a child with `--remote-debugging-pipe`, attaches to each
page target over the pipe (the app's fds 3 and 4; nothing listens on a
port), and installs a watcher with `Page.addScriptToEvaluateOnNewDocument`
plus `Runtime.evaluate`. The watcher keys on the picker's data attributes,
not its hashed class names: it reads the explicit-model row's display name,
looks it up in the label-to-accent table the hub projected from the
catalogue's presentation, and sets the token on the picker. Native Codex
names also come from the local `~/.codex/models_cache.json` and the Codex
provider's inventory, including models outside the Hub's curated catalogue.
Exact Hub labels take precedence; native names additionally match across
spaces and hyphens (`GPT-6-Astra` / `GPT-6 Astra`). Native models default to
`#705AFF` for the effort word, picker title and slider, with the same exact
violet beneath Ultra's shimmer. Missing or unfinished cache data falls back
to the inventory. The helper
ignores SIGTERM and lives until the app exits,
because closing the pipe is the app's cue to quit (Electron's pipe handler
calls `Browser::Quit()` on EOF); it also outlives Provider Hub, discarding
its status output once the hub is gone, and a message it cannot handle is
reported rather than raised. Should the helper itself die, the app treats
the closed pipe as a quit request and, with local chats running, shows its
own "Quit?" dialog first. Verified against the installed 26.908.70816
build: its picker CSS is byte-identical to the 26.908.40834 build the hooks
were read from, the app neither strips nor requires the switch, and its
`devTools:false` window preference only gates the in-app DevTools. The app
takes no single-instance lock on macOS, so the helper refuses to spawn while
a copy is running; pipe ends are parked above fd 10 before the dup2 so the
child never inherits a close-on-exec descriptor; the app starts in its own
session with its stdio on `/dev/null` and a launch-services-like environment
(login basics, launchd's PATH, the bundle's `LSEnvironment`) rather than the
hub's, so no provider key or `NODE_OPTIONS` reaches it. The bridge
auto-attaches to page targets only and detaches again from anything that is
not the app's own `app://-/` document (sandboxed app frames, browser-panel
windows on outside sites), and the watcher bails out in any frame that is
not such a top-level document. The watcher also tints the effort word of the
composer's model pill (`[data-codex-intelligence-trigger]`, whose
`data-selected-reasoning-effort` names the level): the trigger stacks one
span per level (`[data-reasoning-effort]`) and crossfades them inside an
effort label that carries the pill's tertiary grey, while the Ultra span
paints itself with the app's purple token. An inline colour on that label,
marked with `data-provider-hub-tint`, gives the visible word the model's
accent below Ultra; anything the app already paints purple is left alone,
and the colour is removed again when the pill changes. Ultra itself takes
the model's hue rather than the app's purple: the app paints its top level
with one token (`--color-chart-purple`) in three places (the popover's
title, `[data-maximum="true"]`; the slider's fill gradient beneath it,
which blends that token with chart-blue; and the pill's Ultra layer,
`[data-reasoning-effort="ultra"]`), so while the pill's
`data-selected-reasoning-effort` is `ultra` the watcher sets that token,
and `--provider-hub-ultra`, inline on the picker targets and on the pill
trigger to the model's Ultra hue, and marks the title and the Ultra layer
with `data-provider-hub-ultra` for a shimmer sweep. The Ultra hue is the
accent in OKLCH with its lightness moved 0.05 away from the surface, up on
dark and down on light, and its chroma raised by half or to the sRGB gamut
edge at that lightness, whichever comes first (an accent already at the
edge can end a little under its base chroma once moved, and a grey stays
grey); contrast can only improve, and the step stays visible for every
accent. The surface is read from the model name's text colour, as for the
shimmer. The sweep is a zero-specificity rule in the
adopted stylesheet: a 240%-wide gradient of the hue with one lighter
highlight, clipped to the text and slid across it every 3.2 s; only the
text fill goes transparent, so `color` keeps drawing anything that uses
it, the hue falls back to `currentColor` so a marked word can never
vanish, and `prefers-reduced-motion` gets a still fill in the plain hue.
For native Codex, Max also receives the accent when it shares the maximum
title attribute; only Ultra receives the shimmer. Other providers retain
their existing Max styling. The watcher removes its own tokens and marks
when the model or level changes.

The native-model change was verified with 24 accent tests and the full
1,173-test Python suite. Chromium checks covered the older two-part pill
and the current stacked effort layers in light and dark themes: High,
Extra High, Max, Ultra shimmer, reduced motion, provider switching, and
cleanup for an unknown model. This verifies the injected watcher in a
picker fixture; the installed Hub must include the updated worker, then
Codex / ChatGPT must be relaunched through it to install the new watcher.

The app's loading shimmer is
tinted through the app's own knobs: its shimmer text derives every tone
from `--loading-shimmer-foreground` (the base gray, falling back to
`--color-codex-description`, which in the desktop windows is the text
colour at 70%), takes its sweep from `--loading-shimmer-highlight` (a
per-theme constant, `#ffffffbf` light, `#0009` dark), and resets both on
the element with a zero-specificity `:where()` rule. The watcher sets
`--provider-hub-accent`, `--provider-hub-hue` (the accent's OKLCH hue in
degrees, projected by the hub; an achromatic accent gets none) and
`data-provider-hub-theme` on the root from the main composer's model, and a
zero-specificity rule in the adopted stylesheet sets the foreground to
`oklch(from var(--color-codex-description) l 0.07 var(--provider-hub-hue))`:
the app's own gray at its own lightness and alpha with a fixed 0.07 of
chroma in the provider's hue, so the text reads as a slightly cooler gray
and its legibility does not move, while the sweep stays the app's own.
Being later in the cascade the rule beats the reset, while any component
that sets its own foreground still wins, and with no hue the value is
invalid and the app's fallback returns. What that reaches is narrower than
the knob's name suggests. The labels a running turn draws — "Thinking",
"Reading …", "Listing files…", "Editing files" — are not that shimmer at
all: they go through one component (`gya` in `app-initial`, imported into
`conversation-blocks` as `G`), which renders a hashed CSS-module shimmer
while the row is active (`_cadencedShimmer_py5xu_2`, with a
`_cadencedShimmerSweep_py5xu_35` child holding a duplicate of the text) and
a plain `<span>` when it is not. The string `loading-shimmer` does not occur
once in `app-initial`, and occurs exactly once in `conversation-blocks` — at
byte 68842, as `_activeCommentary_r9zcq_5 loading-shimmer`, the assistant's
own message body while it streams. The rule therefore leaves the transcript
largely alone and does its work on the Codex chrome around it, where the app
takes the class bare: a task row's meta cell in the sidebar while it loads, a
subagent row's status while it is active, the pull-request hover card's
"Loading pull request…", the composer's permissions pill while its options
arrive, the artifact tab's status line. The cadenced shimmer reads the same
knobs behind a reset of the same shape, so the same value would tint it — but
that reset is a declaration on the element itself rather than something
inherited, so the foreground has to be set there, and the only name on that
element is hashed; the authored triple it also carries,
`relative inline-block align-top`, occurs once in the whole bundle and is the
hook if this is ever worth taking.

Side Chats and selected subagent transcripts have their own accent scope.
The watcher identifies their app-shell tab panels by the authored
`role="tabpanel"`, `data-app-shell-tab-panel-controller` and `data-tab-id`
hooks (`sidechat:`, `sidechat-loading:` and `subagents:` prefixes). Their
composers cannot supply the root's accent, even if a child appears before
the main composer in DOM order. Each child panel gets local accent, hue
and theme values from its own composer, or from the selected subagent
header's model metadata when there is no composer. Header routing IDs use
the same resolved catalogue presentation as picker labels, including custom
branding and hosted model brands; native Codex names keep the existing
punctuation matching. The optional localised effort suffix is ignored.

The child values stay inside that panel, including when a Side Chat moves
to the bottom panel. Model changes, tab reuse and panel removal refresh or
restore only the watcher's own values. An unknown or loading child keeps
the app's original grey glyphs; an achromatic child blocks inherited hue.
The main transcript keeps its parent's accent. Provider identicons and
warning glyphs still keep their own colours. Optional Chromium regressions
in `Source/test_codex_accent_dom.py` exercise these behaviours using the
installed app's pane/header markup; they require Node, Playwright and its
Chromium browser (a bundled package directory can be supplied in `NODE_PATH`).
The worker must be rebuilt into Provider Hub, then Codex / ChatGPT relaunched
through it for the new watcher to take effect.

No rule keys on that shimmer to find an icon. One did —
`svg:has(~ .loading-shimmer-pure-text)`, and the same icon one wrapper deep —
on the understanding that an activity row is an inline-flex box holding its
icon and its shimmer label as siblings; that understanding no longer holds
anywhere it mattered. Walked over all fifteen chunks that carry the class,
the pattern matches exactly one element in the build, and it is not an
activity row: the shield leading the ChatGPT safety-review notice
(`viewer-…js` @75344), whose `<svg>` and shimmering body span really are
siblings inside `div.flex.min-w-0.flex-1.items-start.gap-2`. Everywhere else
the shimmer sits on the icon's own ancestor (the composer's permissions pill,
the memory-summary spinner), or one level deeper than the icon's sibling (the
automation plan's step list, the Work-home catch-up row, the artifact comment
thread), or the mark that leads the row is an `<img>` identicon rather than an
`<svg>` (the background-agents row, the subagent row). So the rule is gone: a
per-model accent on a safety notice is the wrong reading, and a running row
needed no hook of its own anyway.
The glyph that leads a tool-call row — "Reading …" while it runs, "Ran
commands", "Read files, ran commands", "Edited a file, read files, ran a
command" once it lands — takes a flat accent. One rule serves both states
because it is one element in both: the header picks its glyph inside the
running/settled branch but builds the slot after it, from the same variable,
and the running branch calls the very same icon switch the settled branch
does, so the two draw identical markup and differ only in the label. The row
carries no shimmer to key on in either state, so the glyph is found by the two
names the app wrote by hand and leans on itself: the header is a Tailwind
named group (`group/activity-header`, which its own stylesheet references
in fourteen rules for the row's hover and focus states), and inside it the
glyph is handed to a single `display:contents` slot — a class pair that
occurs twice in the whole bundle, and both are that slot. The rule is
`[class~="group/activity-header"] span[class~="contents"] > svg[class~=
"text-text/60"]`, scoped to the root's theme attribute for a specificity of
0-5-2 and carrying `!important`. The grey it replaces is a plain utility in
the app's `utilities` layer, which an `!important` declaration outranks
whatever its specificity; the row's own `!important` grey sits on the label
span, a *sibling* of the slot, so it never reaches the glyph at all, and
`!important` is kept only because the icon component can merge an inline
`style` onto the `svg`. Matching the app's grey on the glyph itself is what
does the excluding, with no list to maintain: the disclosure chevron sits
outside the slot, a remote MCP logo is an `<img>` that `> svg` skips, and
the two glyphs that name a colour of their own — a denied approval's
warning mark and the subagent identicon — carry no grey to match, so they
keep their meaning. Every other activity row that draws a muted glyph
(a stream or system error, a compaction, a worktree init) is tinted along
with the tool calls; those glyphs are already the same grey, so no semantic
colour is lost. The individual rows revealed by expanding a group put their
glyph outside the slot and stay grey, and the plain "Thinking" placeholder a
turn can show before any tool is in flight takes its mark from a prop threaded
in from outside the component rather than from that icon switch, so it is the
one row whose colour this does not decide. Nothing here is localised and nothing
is hashed, so a build that renames either hook loses the colour and nothing
else; the watcher's status reports the live match count as `glyphs`.
The accent is whichever model the composer currently names, not the model
that produced the row, so switching models re-tints the scrollback.
The watcher observes the document node rather
than its root element (a document-start script runs before the root exists,
and the evaluate sent at attach time is queued until the window's first real
document is created, so it lands at that same moment), and the helper
evaluates the idempotent script again on each load event, logging the
script's own status (`installed`, `skipped`, or `error` with the exception
text). What remains by design: the pipe is a full
control channel into the app (the injected script sits in the origin the
app's main process trusts for its own IPC), so only this helper holds it and
no gateway endpoint reaches it; and macOS attributes the child's privacy
prompts (microphone, camera, calendars, reminders, location, folders,
automation) to Provider Hub. Unsupported by OpenAI; a picker redesign
switches the colour off with no other effect. The same route is closed for
Claude Desktop: its composer is remote claude.ai content, the Ultracode
violet is a fixed design-system ramp, and the app refuses to start with a
debugging switch unless an Anthropic-signed token is present.

**ChatGPT usage banner (opt-in, with the accents)**

With **Show your ChatGPT account in Codex** on, the app shows its
rate-limit banner ("You're out of Codex and Work usage") above the composer
whenever the signed-in plan is exhausted, although hub traffic never spends
that plan. The banner is the app's generic banner component: an `aside`
with utility classes only, no role and no test id, and a title from a
localised message table (`codex.upsellBanner.*`; the per-model variant,
`codex.modelLimitBanner.*`, sits alongside; the bundle ships those tables
in some sixty locales), so neither classes nor text give a hook that
survives an update or a locale. What both variants share is their icon,
the gauge glyph, whose path begins `M10.8343 12.0693`; that icon appears
inside an `aside` nowhere else in the app (its other two uses are the
`/status` and `/usage` slash-command rows). With the Codex-tab switch on,
the watcher's adopted stylesheet carries
`aside:has(svg path[d^="M10.8343 12.0693"]){display:none}`: the selector
outranks the utility classes on specificity alone, so no `!important` is
needed, and the watcher's status reports how many banners it currently
matches. The state behind the banner is not touched: the app still receives
the account's rate-limit status, the modal it may open on submit and the
account and usage pages are unchanged, and an update that redraws the icon
brings the banner back with no other effect. Off by default, and only
reachable through the helper, since the stylesheet is the helper's.

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

The bundle must stay byte-identical to its signature after installation, so
the app never writes into it: the worker's Python bytecode cache is redirected
to the hub's Application Support directory (`PYTHONPYCACHEPREFIX`), the build
strips any `__pycache__` before signing, and the app removes a cache that an
outside Python run left under `Resources/worker` before it starts the worker.
`codesign --verify --deep --strict` on the installed app is the check.


**0.5.0 provider expansion**

Qwen Token Plan, OpenRouter and Gemini API are available to both harnesses. OpenRouter uses native Responses and publishes context-specific routes; Qwen and Gemini use the existing local Messages bridge. Provider-specific contract and qualification details are in `PROVIDER-ADDITIONS.md`.
