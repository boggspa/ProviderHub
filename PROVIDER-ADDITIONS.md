# Provider additions after 0.4.0

## Qwen Token Plan

The `qwen-token-plan` connection uses the dedicated Singapore Token Plan endpoint and `QWEN_TOKEN_PLAN_API_KEY`. Coding Plan and generic DashScope/PAYG
endpoints are rejected for this connection. Claude uses native Anthropic Messages; Codex uses the existing local Responses-to-Messages bridge.

The current documented Qwen text roster is included. Qwen 3.6 Plus is labelled
in the catalogue warning as Team Edition availability. Media-generation and
retired preview aliases are not seeded. Catalogue listing does not prove account
inference access. The endpoint/model roster comes from Alibaba's Token Plan
Personal/Team documentation and its Anthropic Messages API reference.

TaskWraith's `PiModels.ts` and the installed `@earendil-works/pi-ai` 0.84.2 catalogue corroborate exact limits for the same Token Plan endpoint: 1,000,000 context tokens for Qwen 3.6 Flash/Plus, 3.7 Max/Plus, and 3.8 Max; output caps are 65,536 for Flash/Plus and 131,072 for the Max models. The source filename, package version, endpoint, and SHA-256 are recorded in `qwen_provider.py`. This is imported catalogue evidence, not a new live long-context measurement.
Qwen 3.8 Flash is newer than that snapshot, so its exact limits remain unknown.

Qwen 3.8 exposes None/Low/Medium/Extra High to Codex. Claude High/Max normalize
to the documented Extra High setting. Earlier models use a thinking switch.
Fast and alternate processing tiers are explicitly rejected. Text tool-result
arrays are normalized to the string format documented by Alibaba; image inputs
are supported as user-message images on the appropriate models.

Seven new tests cover isolated credentials/endpoints, previous settings,
catalogue provenance and context, effort mapping/conflicts, Claude JSON/SSE tool
cycles, and Codex JSON/SSE tool cycles with encrypted reasoning preservation.
The combined baseline plus Qwen suite passes 188 tests with ResourceWarnings
treated as errors. No provider credentials or paid inference were needed for
these tests.

Sources:

- https://docs.modelstudio.console.alibabacloud.com/en/model-studio/token-plan-personal-overview
- https://docs.modelstudio.console.alibabacloud.com/en/model-studio/token-plan-team-overview
- https://www.alibabacloud.com/help/en/model-studio/anthropic-api-messages
- TaskWraith `src/host-shared/pi/PiModels.ts` and Pi AI 0.84.2's
  `dist/providers/data/qwen-token-plan.json`, inspected 2026-09-13.

## Curated OpenRouter

The `openrouter` connection uses its own `OPENROUTER_API_KEY`, native Messages for Claude, and native Responses for Codex. The source shortlist is TaskWraith's
14 Pi OpenRouter registrations. Each refresh intersects that shortlist with the
current Models API, then fetches endpoint metadata concurrently. Limits and
effort sets come from the current API, not the shortlist's older snapshot.

Different endpoint context limits become distinct model choices. The highest context retains the ordinary model route; other choices use local `/context-N` route suffixes. All send the original upstream model ID. Explicit endpoint allow/ignore lists prevent a selected context from silently moving to a smaller endpoint, including variants matched by a base provider slug.
Reasoning/forced-tool/sampling/structured-output controls further restrict the
eligible hosts. Routine token and default parallel controls are forwarded to
OpenRouter's native adapter without a blanket `require_parameters` check that
would incorrectly exclude currently advertised Fugu and Inkling endpoints.
Unknown endpoint limits remain unknown. No same-model Fast tier is advertised.

An opaque session grouping hash keeps routing sticky across equivalent string and full-history requests. It contains no prompt text and does not enable server-stored continuations. Responses uses full history with `store:false`.
Both gateway fingerprints include endpoint and control metadata so refreshing
that metadata cannot activate a stale routing snapshot.

The public metadata check on 2026-09-13 returned 12 curated model IDs and 17
context variants. MiniMax M3 Free and Mercury 2.5 Preview were absent from that
public list. The installed Codex model-list parser accepted all 17 choices.
This was metadata-only, without a key or inference. The baseline plus Qwen and
OpenRouter suite passes 200 tests with ResourceWarnings treated as errors;
OpenRouter's tests cover native Claude/Codex JSON and SSE tool continuations,
opaque reasoning, endpoint binding, state fingerprints and control edge cases.

OpenRouter and upstream-brand accents reuse TaskWraith's presentation palette.
The account identity remains OpenRouter regardless of the displayed model brand.

Sources:

- https://openrouter.ai/api/v1/models
- https://openrouter.ai/docs/guides/routing/provider-selection
- https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-messages?explorer=true
- https://openrouter.ai/docs/guides/features/router-metadata
- TaskWraith `src/host-shared/pi/PiOpenRouterModelRegistration.ts`,
  `src/shared/taskWraithProviderPresentation.ts`, and renderer `styles/theme.css`.

## Gemini API

The `gemini` connection uses `GEMINI_API_KEY` and Google's official
OpenAI-compatible Chat Completions endpoint. No CLI login, OAuth, Antigravity
credential, or agent runtime is involved. Google receives an explicit Provider
Hub partner-client header. Both metadata and inference use that project's API
key, with credentials kept out of URLs and logs.

Discovery follows the Models API's pagination and intersects account-listed
generateContent models with Google's documented function-calling roster.
An exact known model ID remains usable when optional `baseModelId` is absent
or only names a family; broad prefix inference is not used. Input and output
limits remain separate. The Codex context field uses the reported input limit,
and the gateway does not subtract input usage from Google's independent output
allowance. API version, base-model identity and effort metadata participate in
alias grouping and gateway fingerprints.

Google's positional `extra_content.google.thought_signature` fields survive
complete tool and text continuations. The adapter carries them in an
authenticated `redacted_thinking` block bound to the exact model, credential
scope and visible assistant content. Codex carries that block through the
existing encrypted Responses envelope. Missing 2.5 signatures remain absent;
Gemini 3 tool calls without their required signature fail explicitly. No
fabricated bypass signature is used.

Gemini streams buffer visible content until the final signature arrives, while
the gateway keeps the client connection alive. This preserves reasoning-first
ordering and signatures that arrive on a final empty-text chunk. Actual tool
calls take precedence over the observed compatibility API's `stop` finish label.
Thought-token usage is included in the output count.

The Sol Max agent's 19 focused tests are joined by seven gateway and catalogue tests:
Claude JSON/SSE tool and text continuations, Codex JSON/SSE namespaced tool
cycles, missing-signature/quota errors, independent output limits, and distinct
model snapshots, opaque signature bytes excluded from text-token estimates,
and model-specific default effort. Google's documented medium, minimal and
off defaults are retained; models with an unnamed default keep a null Codex
default so the provider can choose its budget.
TaskWraith's Gemini API implementation and tests corroborate
the positional signature and unsigned-2.5 rules. No live Gemini key or inference
was used during implementation; account qualification follows key entry.

Sources:

- https://ai.google.dev/gemini-api/docs/openai
- https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures
- https://ai.google.dev/api/models
- https://ai.google.dev/gemini-api/docs/thinking
- https://ai.google.dev/gemini-api/docs/generate-content/gemini-3
- https://ai.google.dev/gemini-api/docs/function-calling
- https://ai.google.dev/gemini-api/docs/partner-integration
- TaskWraith `src/main/GeminiApiProvider.ts` and `GeminiApiProvider.test.ts`.

## Union Alpha — OpenRouter stealth preview

`stealth/union-alpha` joins the curated OpenRouter roster as a fifteenth ID. It
is a free, anonymously operated stealth preview: OpenRouter routes to a single
provider it does not own, and the model page carries the Stealth Model Terms
notice that prompts and completions may be retained by that provider though
they are not used for training. OpenRouter published it on 16 September 2026,
and it was announced as a seven-day window. Nothing in this repository pins
that window. Membership is the intersection of the curated shortlist with the
live Models API, so when the preview is withdrawn the route disappears and the
catalogue warning names it as no longer listed, rather than leaving a dead
choice selectable in the picker.

The ID reaches the picker on the ordinary OpenRouter API-key route, with no
separate connection, credential, or Pi hop: Provider Hub ingests OpenRouter as
a provider directly. Context, output cap, effort ladder and tool-capable
endpoints come from current endpoint metadata like every other curated entry,
so none of the figures below are hard-coded. On 16 September 2026 the public
metadata described one standard endpoint, `stealth`, with 262,144 context and
131,072 max completion tokens, text and image input, and `tools`, `tool_choice`,
`response_format`, `temperature`, `top_p` and `max_tokens`. That yields a
single route, `openrouter/stealth/union-alpha`, labelled Union Alpha.

Two consequences follow from that metadata rather than from a choice made here.
The model advertises no `reasoning`, `reasoning_effort` or `include_reasoning`
parameter, so it publishes an empty effort ladder and no reasoning control
appears beside it in either picker. Asking it to think is refused rather than
silently ignored; asking it *not* to think is accepted, since an empty ladder
means a model that never reasons rather than one whose reasoning cannot be
switched off (`reasoning_axis`, added in `3b3a39a` after Claude Desktop's
always-present disabled thinking block made the route unusable). And the
endpoint advertises `auto` tool choice only, with `required`, `function` and
`none` all false, so a forced or suppressed tool choice fails the endpoint
check instead of being sent to a host that does not support it. Ordinary auto tool cycles are unaffected. If
OpenRouter later advertises a reasoning parameter or a wider tool-choice set,
a catalogue refresh picks it up with no code change.

An anonymous provider has no brand to borrow, so it gets an arbitrary accent:
`stealth`, `#9E6C00`. The palette's rule is that an accent is a foreground
colour on both the light and the dark surface, so every hue is normalized to
one WCAG relative luminance — 0.176 to 0.182 across the accents — rather than
to a brand's own lightness, giving 4.5:1 or better against white and a clear
step off the hub's dark chrome. `#9E6C00` is about the most saturated gold sRGB
holds at that luminance: OKLCH lightness 0.569, hue 76.8, chroma 0.119, near
the gamut edge there, the way NVIDIA green, Xiaomi green and Kimi blue sit at
their own edges. It measures 4.57:1 on white, 4.60:1 on black and 3.86:1 on the
hub's `#18191A`, inside the palette's existing bands. It is placed between the
two accents it could be confused with, Claude's amber `#B16105` and Cursor's
yellow `#8C7508`, and its Ultra cut is `#B17A00` on dark, `#8B5F00` on light.
`test_accent_palette_holds_one_readable_luminance_band` now asserts that band
for every accent, so the next arbitrary hue cannot drift out of it.

The value is TaskWraith's, not this repo's. Both sides minted a gold
independently for the same namespace and landed one hue degree apart — an
unremarkable outcome, since the luminance band and the sRGB gamut leave almost
no room at that hue. `#9E6C00` is the one in TaskWraith's
`--provider-stealth-color`, so it is the one kept; this file's first draft
recorded the local `#A06B00` before the two were compared.

The accent is display only. The brand rule keys on the `stealth/` prefix and
changes the label, hue and initials to Stealth; `runtimeProvider` stays
`openrouter`, and the connection, the key and the bill stay OpenRouter's. There
is no sourced mark for an anonymous provider and no borrowed one, so the chip
falls back to its mnemonic glyph.

Four tests cover the addition: the curated ID keeping its real upstream model
ID through projection and finalization while carrying the Stealth presentation,
a withdrawal leaving no route behind, the palette-band rule across every
accent, and the resolved Stealth presentation. The metadata quoted above was
read from the public, unauthenticated Models and endpoints APIs and pushed
through the real discovery, catalogue and Codex projection path. No OpenRouter
key and no paid or free inference was used; catalogue listing is not an
inference test, and account access to a stealth route still follows key entry.

Sources:

- https://openrouter.ai/stealth/union-alpha
- https://openrouter.ai/api/v1/models
- https://openrouter.ai/api/v1/models/stealth/union-alpha/endpoints
- TaskWraith `src/renderer/src/styles/theme.css` (`--provider-stealth-color`) and
  `src/shared/piBrandTable.ts` (`openrouter/stealth`), read-only.

## Ollama display brands — filling in the rest of the table

The hub carried six of TaskWraith's seventeen `OLLAMA_DISPLAY_BRANDS` entries.
The other eleven — Cohere, Deep Reinforce, Essential AI, Google (the Gemma
spoof class), IBM, Liquid, MiniMax, NVIDIA, OpenAI, OpenBMB and Poolside —
were missing, so those local and Cloud models fell through to Ollama's own
walnut brown. They are now mirrored in full, in TaskWraith's order, with the
needles copied verbatim: the needles are the attribution, and inventing a
looser one here would put the two tables quietly out of step. The six that
were already present matched TaskWraith exactly and are unchanged.

Cloud tags need no separate rules. An Ollama Cloud id is the same name with a
`:…-cloud` suffix, so `gpt-oss:120b-cloud`, `deepseek-v3.1:671b-cloud`,
`qwen3-coder:480b-cloud`, `kimi-k2:1t-cloud`, `minimax-m2:cloud` and
`glm-4.6:cloud` all match on the local needle. Where a needle is
version-pinned — Cohere's `north-mini-code-1.0` — an unversioned tag reaches
the brand through the humanised label the hub always supplies, which is the
same second pass the upstream matcher makes. An unrecognised tag keeps
Ollama's brown rather than guessing a maker.

Six accents came with them, at TaskWraith's exact values: `codex` `#705AFF`,
`deep-reinforce` `#BE5809`, `essential` `#8462CA`, `ibm` `#3079BC`, `liquid`
`#D72D82` and `openbmb` `#E22B17`. Two are aliases rather than new hues,
mirroring the `var()` indirection upstream: `openai` resolves to `codex`, so
an Ollama-hosted GPT-OSS wears the same violet the Codex seat does, and
`google` resolves to `antigravity`, so Gemma wears Google's green rather than
the retired Gemini blue. Every one of them measures 4.5:1 or better on white
and sits inside the palette's shared luminance band, which the band test now
checks for the whole palette rather than for the accents that happened to be
there when it was written.

Each new class also gets a presentation entry so its chip carries a chosen
mnemonic instead of a derived one — OpenAI and OpenBMB both derive `OPE`, and
`NVIDIA` derives `NVI` where the palette already says `NV`. Fallback model
labels keep this repository's generic form (`Granite model`) rather than
TaskWraith's (`Granite 4.1 (3B Param)`), which names that project's own pulled
tags and parameter counts; the hub has not observed them, and the fallback is
only reached when no label is available at all.

None of this changes routing. `runtimeProvider` stays `ollama`, the daemon,
the model tag and the absence of a credential are untouched, and the Cloud
source classifier is unaffected. The visible effect is in the Codex /
ChatGPT desktop watcher, where the composer pill, the power slider, the Ultra
cut and the activity shimmer take the model's accent: before this, every
Ollama row that was not Qwen, DeepSeek, Kimi, Llama, Mistral or GLM shared one
brown.

Sources:

- TaskWraith `src/shared/ollamaBrandTable.ts` (`OLLAMA_DISPLAY_BRANDS`),
  `src/renderer/src/lib/ollamaDisplayBrand.ts`, and
  `src/renderer/src/styles/theme.css` (`--provider-*-color`), read-only.
