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
appears beside it in either picker; a client that asks for one is refused
rather than silently ignored. And the endpoint advertises `auto` tool choice
only, with `required`, `function` and `none` all false, so a forced or
suppressed tool choice fails the endpoint check instead of being sent to a host
that does not support it. Ordinary auto tool cycles are unaffected. If
OpenRouter later advertises a reasoning parameter or a wider tool-choice set,
a catalogue refresh picks it up with no code change.

An anonymous provider has no brand to borrow, so it gets an arbitrary accent:
`stealth`, `#A06B00`. The palette's rule is that an accent is a foreground
colour on both the light and the dark surface, so every hue is normalized to
one WCAG relative luminance — 0.176 to 0.182 across the existing accents —
rather than to a brand's own lightness, giving 4.5:1 or better against white
and a clear step off the hub's dark chrome. `#A06B00` is the most saturated
gold sRGB holds at that luminance: OKLCH lightness 0.570, hue 74.9, chroma
0.120, which is the gamut edge at that lightness, matching the way NVIDIA
green, Xiaomi green and Kimi blue sit at their own edges. It measures 4.57:1 on
white and 3.85:1 on the hub's `#18191A`, inside the palette's existing
4.54-4.64 and 3.80-3.88 bands. Its hue is 23 degrees off Ollama's brown and 37
off Mistral's orange, and Grok's grey carries no hue to collide with. Its Ultra
cut is `#B37800` on dark and `#8D5E00` on light.
`test_accent_palette_holds_one_readable_luminance_band` now asserts that band
for every accent, so the next arbitrary hue cannot drift out of it. TaskWraith
is taking the same `#A06B00` for the same model on its own Pi-routed path.

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
- TaskWraith chat "OpenRouter Stealth model in Pi Catalogue", which registers
  the same model on Pi and takes the same `#A06B00` override.
