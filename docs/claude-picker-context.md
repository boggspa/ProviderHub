# Claude Desktop picker context

Checked against first-party documentation on 2026-09-19.

The `/v1/models` subtitle contains only the context window, for example
`500,000 token context`. Account labels, inference history, tier labels and
baseline notices remain available as metadata rather than subtitle text.

## Previously unknown windows

| Route | Published model ceiling (tokens) | Evidence |
| --- | ---: | --- |
| Claude Fable 5.1, Fable 5, Opus 5, Sonnet 5 | 1,000,000 | [Anthropic context windows](https://platform.claude.com/docs/en/build-with-claude/context-windows) |
| Claude Opus 4.8, 4.7, 4.6 and Sonnet 4.6 | 1,000,000 | Same Anthropic context-window table |
| Claude Haiku 4.5 | 200,000 | Same Anthropic context-window table |
| Grok 4.6 | 500,000 | [xAI model documentation](https://docs.x.ai/developers/grok-4-6) |
| Grok 4.5 | 500,000 | [xAI model card](https://docs.x.ai/developers/models/grok-4.5) |
| DeepSeek V4 Pro | 1,048,576 | [Publisher config, pinned revision](https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro/blob/b5968e9190ef611bbf34a7229255be88a0e937c1/config.json), `max_position_embeddings` |
| DeepSeek Flash (V4.1) | 1,048,576 | [Publisher config, pinned revision](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/blob/dba1be0a40aa45a94ad051997016db3960a90277/config.json), `text_config.max_position_embeddings` |
| Qwen 3.8 Flash | 1,000,000 | [Alibaba model card](https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen3-8-flash); exact ID is in the [Token Plan roster](https://docs.modelstudio.console.alibabacloud.com/en/model-studio/token-plan-personal-overview) |

These are published model ceilings, not account entitlement measurements.
Claude subscription access and CLI settings can restrict extended context;
see [Claude Code model configuration](https://code.claude.com/docs/en/model-config#extended-context).
DeepSeek's [API pricing table](https://api-docs.deepseek.com/quick_start/pricing/)
identifies the served versions and advertises `1M`; the exact integer above
comes from the publisher's model configuration, not an API boundary test.
Qwen's window is combined input and output. Its input limit is smaller, and
the model card separately lists an output ceiling of 131,072 tokens.

Discovery attaches provenance to known exact models. Claude's projection
also fills missing context in older saved inventories, so refreshing each
provider is unnecessary. Existing numeric limits and account-dependent
ranges take precedence. Unknown future models and unpinned aliases retain
an unknown context; limits are never copied across hosting providers.

The managed Claude Code picker and Messages planning use the same resolved
specification as Desktop's model list. Existing Codex config overrides and
AntiGravity Gemini handling are unchanged.

Validation uses disposable state and simulated discovery, without inference
or changes to saved account settings. A read-only projection of the user's
37 selected routes returned context-only subtitles and no unknown windows.
