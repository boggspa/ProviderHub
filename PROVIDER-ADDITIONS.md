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
