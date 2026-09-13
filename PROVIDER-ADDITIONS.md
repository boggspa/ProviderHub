# Provider additions after 0.4.0

## Qwen Token Plan

The `qwen-token-plan` connection uses the dedicated Singapore Token Plan
endpoint and `QWEN_TOKEN_PLAN_API_KEY`. Coding Plan and generic DashScope/PAYG
endpoints are rejected for this connection. Claude uses native Anthropic
Messages; Codex uses the existing local Responses-to-Messages bridge.

The current documented Qwen text roster is included. Qwen 3.6 Plus is labelled
in the catalogue warning as Team Edition availability. Media-generation and
retired preview aliases are not seeded. Catalogue listing does not prove account
inference access. The endpoint/model roster comes from Alibaba's Token Plan
Personal/Team documentation and its Anthropic Messages API reference.

TaskWraith's `PiModels.ts` and the installed `@earendil-works/pi-ai` 0.84.2
catalogue corroborate exact limits for the same Token Plan endpoint: 1,000,000
context tokens for Qwen 3.6 Flash/Plus, 3.7 Max/Plus, and 3.8 Max; output caps are
65,536 for Flash/Plus and 131,072 for the Max models. The source filename,
package version, endpoint and SHA-256 are recorded in `qwen_provider.py`.
This is imported catalogue evidence, not a new live long-context measurement.
Qwen 3.8 Flash is newer than that snapshot, so its exact limits remain unknown.

Qwen 3.8 exposes None/Low/Medium/Extra High to Codex. Claude High/Max normalize
to the documented Extra High setting. Earlier models use a thinking switch.
Fast and alternate processing tiers are rejected explicitly. Text tool-result
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

The `openrouter` connection uses its own `OPENROUTER_API_KEY`, native Messages
for Claude, and native Responses for Codex. The source shortlist is TaskWraith's
14 Pi OpenRouter registrations. Each refresh intersects that shortlist with the
current Models API, then fetches endpoint metadata concurrently. Limits and
effort sets come from the current API, not the shortlist's older snapshot.

Different endpoint context limits become distinct model choices. The highest
context retains the ordinary model route; other choices have local
`/context-N` route suffixes. All send the original upstream model ID. Explicit
endpoint allow/ignore lists prevent a selected context from silently moving to
a smaller endpoint, including variants matched by a base provider slug.
Reasoning/forced-tool/sampling/structured-output controls further restrict the
eligible hosts. Routine token and default parallel controls are forwarded to
OpenRouter's native adapter without a blanket `require_parameters` check that
would incorrectly exclude currently advertised Fugu and Inkling endpoints.
Unknown endpoint limits remain unknown. No same-model Fast tier is advertised.

An opaque session grouping hash keeps routing sticky across equivalent string
and full-history requests. It contains no prompt text and does not enable
server-stored continuations. Responses uses full history with `store:false`.
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
