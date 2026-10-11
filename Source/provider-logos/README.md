The original PNG assets are copied verbatim from TaskWraith's
`src/renderer/src/assets/provider-logos`.

The following PNGs are copied verbatim from Limit Counter's
`LLMUsageCounter/Assets.xcassets`:

| Hub asset | Limit Counter source | Used by |
| --- | --- | --- |
| `provider-logo-codex.png` | `ProviderCodexLogo.imageset/logo.png` | Retained legacy Codex asset |
| `provider-logo-claude.png` | `ProviderClaudeLogo.imageset/logo.png` | Retained legacy Claude asset |
| `provider-logo-antigravity.png` | `ProviderAntigravityLogo.imageset/logo.png` | AntiGravity |
| `provider-logo-qwen.png` | `ProviderQwenLogo.imageset/logo.png` | Qwen Token Plan |
| `provider-logo-openrouter.png` | `ProviderOpenRouterLogo.imageset/logo.png` | OpenRouter |
| `provider-logo-devin.png` | `ProviderDevinLogo.imageset/logo.png` | Devin |
| `provider-logo-meta.png` | `ProviderMetaLogo.imageset/logo.png` | Muse and Meta display branding |
| `provider-logo-minimax.png` | `ProviderMiniMaxLogo.imageset/logo.png` | MiniMax |
| `provider-logo-mimo-on-light.png` | `ProviderMiMoLogo.imageset/logo-light.png` | MiMo (light appearance) |
| `provider-logo-mimo-on-dark.png` | `ProviderMiMoLogo.imageset/logo-dark.png` | MiMo (dark appearance) |

Both source checkouts remain read-only. The display palette and logo settings
are recorded in `../provider_branding.json`.

The optional `leadingMarkAspectRatio` display setting shows only the leading
glyph of a wordmark, preserving its proportions and centering the complete
glyph in the icon slot. Qwen uses the first 239 pixels of its 1024 × 235 source;
Meta uses the first 340 pixels of its 1024 × 237 source. This follows Limit
Counter's leading-glyph presentation without modifying the source PNGs.
`trailingMarkAspectRatio` is the mirror image for marks that end a wordmark:
MiMo shows the ring that closes "Xiaomi MIMO", the last 127 pixels of its
1024 × 131 source. A logo may set one crop or the other, not both.
The optional `template` setting renders a monochrome asset in the UI's primary
foreground color; Devin uses it so the black source glyph remains visible on
the hub's dark surface. Both settings are supported in user logo overrides;
omitting them preserves the original image rendering.

Runtime provider IDs and account routing stay independent of these display assets.

The active Claude and Codex artwork was supplied by the user and copied
verbatim, without redrawing or recoloring the PNGs:

| Hub asset | Supplied filename | SHA-256 |
| --- | --- | --- |
| `provider-logo-claude-spark.png` | `claude.png` | `9ad0db2010a8ef85741c92ae0dd8945126f5cd738bfb84d077a614f1967bbce7` |
| `provider-logo-codex-cloud.png` | `openai-codex-logo-1024x1024.png` | `dd046d767b00c1b49d119bcbd03404014c9e2067f138ddd06efb9d57a21dddba` |

Claude preserves its transparent orange spark. Codex uses `template: true`
with optional `tint: "accent"`, rendering its neutral source in the provider's
current accent (including user overrides). Templates without `tint` keep the
primary foreground behavior. User logo overrides support the same option.
