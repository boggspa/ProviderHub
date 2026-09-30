# Fast mode in the model pickers

Checked against first-party documentation on 2026-09-23. A Fast switch appears
only when a provider documents a request setting for faster serving of the
**same model**. Availability still depends on the signed-in account, workspace,
surface, and provider entitlement; a catalogue entry cannot grant access.

## Switchable Fast modes

| Provider route | Model IDs | Picker and request behavior | Evidence |
| --- | --- | --- | --- |
| OpenAI API or Codex CLI | `gpt-5.5`, `gpt-5.6-luna`, `gpt-5.6-terra`, `gpt-5.6-sol`, `gpt-6-astra`, `gpt-6-luna`, `gpt-6-sol`, `gpt-6.1-sol` (added 2026-09-30) | Codex/ChatGPT Fast control via `service_tiers`; API requests use `service_tier: "fast"`, CLI turns use `serviceTier: "fast"`. | [ChatGPT Work/Codex speed](https://learn.chatgpt.com/docs/agent-configuration/speed), [OpenAI API Fast mode](https://developers.openai.com/api/docs/guides/fast-mode), [API pricing](https://developers.openai.com/api/docs/pricing) |
| Anthropic API or Claude Code CLI | `claude-opus-5-5`, `claude-opus-5`, `claude-opus-4-8` | Fast requests use `speed: "fast"` plus the `fast-mode-2026-02-01` beta header. The CLI receives a per-turn `--settings '{"fastMode":true}'`; Standard sends `false` so saved CLI settings cannot override the Desktop choice. | [Anthropic Fast mode](https://platform.claude.com/docs/en/build-with-claude/fast-mode), [Claude Code Fast mode](https://code.claude.com/docs/en/fast-mode) |

Claude Opus 4.7 is excluded because Anthropic rejects Fast requests for it.
Opus 4.6 is excluded because its Fast request falls back to Standard. A saved
Claude Code `opus` alias resolves to the current versioned Opus 5.5 route.
The Anthropic Fast feature is a research preview with account requirements.
Claude Desktop determines when its own native toggle appears; the hub passes
supported Fast requests through and rejects unsupported ones, but cannot force
that client to render a toggle.

## Routes that are already fast

| Provider route | Model IDs | Why there is no extra Fast switch |
| --- | --- | --- |
| Kimi Code | `kimi-for-coding-highspeed` | HighSpeed is a distinct model route with its own quota and rate. [Kimi model guide](https://www.kimi.com/code/docs/en/kimi-code/models.html) |
| AntiGravity | `gemini-3.6-flash`, `gemini-3.7-flash`, `gemini-3.8-flash` | Flash identifies the selected CLI model variant. The adapter has no separate Fast request parameter. |
| Grok CLI | `grok-4.7-build-fast` | Grok 4.7 Fast is a distinct faster route. The publisher documents it for Cursor/Grok Build, not the public xAI API. [Grok 4.7](https://docs.x.ai/developers/grok-4-7) |
| Cerebras | `gpt-oss-120b`, `qwen-3.8-27b` | Cerebras serves these models on its accelerated inference platform. Its shared endpoint has no separate Fast tier for these model IDs. [Cerebras model list](https://inference-docs.cerebras.ai/models/overview) |

The hub stores `speed_tier` metadata for these routes, labels them in its own
model details, and describes their built-in speed in the Codex model card.
They have no `service_tiers` Fast switch. This avoids offering a Standard
position that cannot change the actual route.

The Codex/ChatGPT catalogue receives `service_tiers` for switchable models.
Claude Desktop's `/v1/models` response carries `fast_mode` and fixed-route
`speed_tier` metadata, while its subtitle remains context-only. The native
client may ignore those extra fields. Both the API and CLI planners validate
the exact route before sending a Fast request.
Older cached provider inventories gain the current capability mapping when
projected, so a provider refresh is not required to see the updated controls.
