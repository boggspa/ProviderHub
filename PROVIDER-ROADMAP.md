**Provider expansion assessment — 12 September 2026**

One menu bar app with provider tabs is the best fit. Five of the requested connections have a straightforward model-API route. Grok Build and Muse also have plausible integrations, but preserving their CLI subscriptions points to hosting their agent sessions. A subscription-backed agent session and a raw model completion API are different interfaces.

| Requested provider | Route to investigate first | Assessment for Claude Desktop |
| --- | --- | --- |
| Kimi Code subscription | Kimi Code's documented Anthropic-compatible coding API, using a subscription API key from its console | High plausibility. Account/tier and model-ID checks remain necessary. A CLI OAuth login and a coding API key are distinct setup paths. |
| Xiaomi MiMo Token Plan | The plan's regional Anthropic-compatible endpoint and Token Plan key | High plausibility. Keep the account's CN, Singapore, or Europe endpoint paired with the plan key. |
| Grok Build subscription | Native Grok ACP session | Plausible as a delegated agent. Reusing its subscription as Claude's raw inference backend is not established by the documentation inspected. xAI also offers a separate model API route with an API key. |
| Ollama | The existing daemon's Anthropic-compatible `/v1/messages` API | High plausibility. Reuse the daemon, installed models, and its supported cloud routing. The Bridge should be the single active owner of its Claude profile while selected. |
| Meta Muse Code subscription | Muse Session Protocol through `muse serve` / the Muse Code SDK | Plausible as a stateful delegated agent. A raw completion endpoint that can consume the same subscription credentials has not been established here. |
| DeepSeek API | DeepSeek's documented Anthropic-compatible endpoint | High plausibility. Explicitly map exact DeepSeek model IDs: the upstream has its own Claude-name fallback mappings. |
| Cerebras API | Its OpenAI-compatible Chat Completions API | High plausibility with a provider-specific Messages-to-Chat adapter. Reasoning fields, context/output limits, images, and rate handling need model-specific treatment. |

The API-compatible routes still need a complete Desktop tool-use test. Claude's actual message envelopes include extensions such as system messages between tool turns, so protocol compatibility alone is not full application compatibility.

Kimi Code documents both `https://api.kimi.com/coding/v1/chat/completions` and `https://api.kimi.com/coding/v1/messages`, with subscription API keys available from the Kimi Code console. Use that coding product's credentials and entitlement rules, rather than assuming an ordinary Moonshot platform key or copied CLI login is interchangeable. [Kimi Code API access](https://www.kimi.com/code/docs/en/#api-access)

MiMo documents dedicated Token Plan endpoints in China, Singapore, and Europe, including an Anthropic-compatible route. The Token Plan and pay-as-you-go keys are separate products. The app should ask for the endpoint shown on the user's plan page and preserve that selection. [MiMo Token Plan quick access](https://mimo.mi.com/docs/en-US/tokenplan/Token%20Plan/quick-access)

DeepSeek documents `https://api.deepseek.com/anthropic` and explicitly discusses Claude Desktop gateway use. Its automatic Claude-name mapping is another reason for our router to resolve the requested slot to an exact upstream model ID. [DeepSeek Anthropic compatibility](https://api-docs.deepseek.com/guides/anthropic_api/)

Ollama already documents streaming, tool calls, tool results, thinking, and its local/cloud model support through the Messages API. We can call the existing daemon from a provider module; there is no need to modify the installed Ollama app to obtain inference. [Ollama Anthropic compatibility](https://docs.ollama.com/api/anthropic-compatibility)

Cerebras documents its OpenAI-compatible API at `https://api.cerebras.ai/v1`, with explicit model-specific differences. For example, reasoning controls vary by model, and supported image inputs use data URIs rather than external image URLs. Our current Mistral-specific defaults should not be carried over unchanged. [Cerebras compatibility guide](https://inference-docs.cerebras.ai/resources/openai)

Grok Build documents ACP integration, browser authentication, and a separately available xAI model API. That establishes an agent-hosting route and an API-key route; it does not establish that the browser subscription credential is a general-purpose completion key. [Grok Build overview](https://docs.x.ai/build/overview)

Muse Code's published SDK controls agent sessions over MSP. The SDK is currently described as a developer preview, so integrations should use an explicit supported protocol/runtime version and surface session and approval failures accurately. [Muse Code SDK](https://github.com/meta-models/muse-code-sdk)

**What TaskWraith contributes**

The local AGBench checkout contains concrete reusable designs for different integration classes:

- Kimi generation detection and the ACP transport boundary in `src/main/providers/KimiFlavour.ts`.
- Grok ACP turn, permission, and cancellation handling in `src/main/grok/GrokAcpClient.ts`.
- Muse MSP dispatch and session resume handling in `src/main/muse/MuseIpcBridge.ts` and `src/main/muse/MuseMspRun.ts`.
- Pi model/brand mappings for Xiaomi Token Plan regions, DeepSeek, and Cerebras in `src/shared/piBrandTable.ts`.
- Cerebras dispatch pacing and completion-cap handling under `src/main/pi/` and the Pi dispatch path in `src/main/index.ts`.

These findings concern the inspected source checkout, not a claim that every integration has been validated in a particular shipped TaskWraith build. The existing adapter modules provide useful protocol knowledge. They do not turn an agent-session transport into a model-completions transport automatically.

**Proposed implementation**

Keep one shared Claude profile manager, model-mapping interface, local listener, and activity view. Each provider module owns authentication, model discovery, request format, streaming, errors, and capabilities. Where an upstream already speaks the Anthropic Messages protocol, use that route with only the required Desktop-specific normalization. Keep a separate Chat Completions translator for providers such as Mistral and Cerebras.

Store each route as an exact provider/account/model tuple, with a friendly label kept separately. A user's visible selection should be sufficient to identify which account receives the request and which provider handles inference. Mixed-provider Claude slots are possible once those routes are explicit. Credentials, quotas, and errors should remain attributable to the selected provider.

Add native agent integrations as a distinct feature. A Grok or Muse MCP delegation tool would manage a resumable native session, stream progress, forward approvals, and return a result. Claude would still require its own primary model for the outer conversation. Approvals and executed tool results must retain their real origin; an agent bridge should not present already-executed actions as unexecuted Claude model tool calls.

Model context, reasoning, and speed should remain provider capabilities. The current app now groups model aliases, loads context limits from Mistral, and maps the Effort value sent by Claude. Future providers should supply their real effort ranges and genuine service-tier options. Claude's own UI does not expose an arbitrary provider capability schema for every control, so native Fast and exact context-meter behavior may require desktop support beyond request translation. Model variants should be listed separately only when the underlying model or context entitlement actually differs.

My suggested order is Kimi Code and MiMo first, followed by Ollama and DeepSeek, then the Cerebras-specific adapter. Investigate Grok and Muse subscription sessions as a parallel design track after defining the delegated-agent experience. This assessment does not add or enable any new provider in the current app; version 0.2.0 still connects Mistral only.
