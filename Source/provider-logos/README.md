The original PNG assets are copied verbatim from TaskWraith's
`src/renderer/src/assets/provider-logos`.

The following PNGs are copied verbatim from Limit Counter's
`LLMUsageCounter/Assets.xcassets`:

| Hub asset | Limit Counter source | Used by |
| --- | --- | --- |
| `provider-logo-codex.png` | `ProviderCodexLogo.imageset/logo.png` | Codex |
| `provider-logo-claude.png` | `ProviderClaudeLogo.imageset/logo.png` | Claude |
| `provider-logo-antigravity.png` | `ProviderAntigravityLogo.imageset/logo.png` | AntiGravity |
| `provider-logo-qwen.png` | `ProviderQwenLogo.imageset/logo.png` | Qwen Token Plan |
| `provider-logo-openrouter.png` | `ProviderOpenRouterLogo.imageset/logo.png` | OpenRouter |
| `provider-logo-devin.png` | `ProviderDevinLogo.imageset/logo.png` | Devin |
| `provider-logo-meta.png` | `ProviderMetaLogo.imageset/logo.png` | Muse and Meta display branding |
| `provider-logo-minimax.png` | `ProviderMiniMaxLogo.imageset/logo.png` | MiniMax |

Both source checkouts remain read-only. The display palette and logo settings
are recorded in `../provider_branding.json`.

The optional `leadingMarkAspectRatio` display setting shows only the leading
glyph of a wordmark, preserving its proportions and centering the complete
glyph in the icon slot. Qwen uses the first 239 pixels of its 1024 × 235 source;
Meta uses the first 340 pixels of its 1024 × 237 source. This follows Limit
Counter's leading-glyph presentation without modifying the source PNGs.
The optional `template` setting renders a monochrome asset in the UI's primary
foreground color; Devin uses it so the black source glyph remains visible on
the hub's dark surface. Both settings are supported in user logo overrides;
omitting them preserves the original image rendering.

Runtime provider IDs and account routing stay independent of these display assets.
