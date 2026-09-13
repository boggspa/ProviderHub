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
