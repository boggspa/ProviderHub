**Provider Hub Preview 0.3.0**

A native macOS menu bar app that connects Claude Desktop to model APIs from Mistral, Kimi Code, Xiaomi MiMo Token Plan, Ollama, DeepSeek, and Cerebras. Configure accounts, pick readable model names, and launch Claude through its native third-party profile system.

This repository contains the multi-provider extension to Mistral Bridge, merged into `main`. The app remains labelled Preview while the remaining provider accounts are qualified. The stable Mistral app and the preview have separate bundle IDs, settings directories, Keychain namespaces, gateway ports, and Claude profile IDs. The preview does not replace the stable app.

**Start using the preview**

1. Open **Provider Hub Preview.app**. It requires an Apple Silicon Mac, macOS 14 or newer, and Python 3.11 or newer. The existing Mistral Vibe installation can supply Python; Vibe's agent does not need to keep running.
2. Select a provider in **Providers**. Mistral can use Vibe's saved credentials. Enter other provider API keys with **Save key** to store them in the preview's macOS Keychain namespace. Ollama connects to the existing local daemon. MiMo's account region selects its official Token Plan endpoint.
3. Use **Refresh catalogue**. This fetches model metadata, not a chat completion. Kimi and MiMo use a documented catalogue because an account-scoped list endpoint has not been established. Their presence in the picker does not prove that the current account can use them.
4. In **Models**, choose a provider and model behind each Claude option. The same underlying model is shown once when several Claude slots use it. **Test** makes a small inference request. Exact provider/model IDs remain available under **Show technical IDs**.
5. Finish current work in Claude and quit Claude. Choose **Launch Claude**. The app saves the mappings, starts the gateway, verifies readiness, switches to its dedicated profile, and opens the installed Claude app.
6. Quitting Claude restores the prior profile selection and normally stops the gateway. The menu bar app remains available.

The main repository is `/Users/chrisizatt/Documents/Mistral Bridge`. Build there to create `Provider Hub Preview.app` alongside the original Mistral app. The development worktree remains at `/Users/chrisizatt/Documents/Mistral Bridge-worktrees/provider-hub` on `feature/provider-hub`. The original Mistral baseline was committed in three slices before development began. The provider extension was subsequently split into provider adapters/reasoning replay, branding/model names, app/gateway integration, and documentation before its fast-forward merge. The original two feature commits remain available on `archive/provider-hub-before-split`.

**Provider connections**

| Provider | Credential | Outbound interface | Catalogue evidence |
| --- | --- | --- | --- |
| Mistral | Vibe sign-in or Mistral API key | Mistral Chat Completions | Authenticated `/v1/models`; canonical aliases grouped; archived/retired IDs omitted |
| Kimi Code | Subscription API key from Kimi Code console | Native Anthropic-compatible Messages | Documented models; membership controls availability/context |
| MiMo Token Plan | Token Plan API key for CN, Singapore, or Amsterdam | Native Anthropic-compatible Messages | Documented Token Plan models |
| Ollama | Existing local daemon/login | Native Anthropic-compatible Messages | `/api/tags` plus `/api/show`; no downloads performed |
| DeepSeek | DeepSeek API key | Native Anthropic-compatible Messages | Authenticated model list plus documented capabilities |
| Cerebras | Cerebras API key | Chat Completions with authenticated reasoning replay | Authenticated list enriched with public model metadata |

No CLI OAuth tokens are extracted to impersonate model API credentials. Grok Build and Muse Code subscriptions expose stateful ACP/MSP agent sessions; hosting those agents is a separate feature from changing the model behind Claude's agent. See `NATIVE-AGENTS.md` for the researched paths, source links, and implementation boundary. Separate xAI and Meta Model API credentials would support future API adapters, with their own API billing.

**Names, branding, context, and model controls**

The display-name parser recognizes provider spelling, versions, sizes, release dates, and qualifiers such as Cloud and Preview. Provider metadata and explicit display overrides take precedence. Examples: `model-name-v1-2504` becomes `Model Name V1 · Apr 2025`; `gpt-oss:120b-cloud` becomes `GPT OSS · 120B Cloud`. Routing always uses the original ID.

The appearance layer reuses TaskWraith's provider presentation schema: `displayProvider`, `hueKey`, `accent`, `shortCode`, `modelLabels`, and optional bundled `logo` assets. It is keyed by immutable runtime provider identity. Provider names, accents, initials, and model labels can be edited in **Appearance**. An Ollama-hosted upstream brand can have its own logo and accent while the route still identifies Ollama as the account/provider. Logos and palette values were copied from the read-only AGBench source; that checkout was not changed.

Context is read-only. Exact integer values come from provider metadata or exact documented fixed-ID limits. Genuine context variants remain separate entries. Plan-dependent or imprecise limits remain provider-managed instead of inventing a number. Ollama's architectural maximum and effective runtime allocation are distinct: `/api/show` does not establish the latter. Claude's own context meter can still use Claude-specific presets; the gateway cannot fully customize it through discovery.

Effort is translated only through established provider controls, using Claude's own request fields. Mistral retains Vibe's coarse Low → standard, Medium and above → reasoning behavior. Effort values are never manufactured as extra models. Fast requests require a documented same-model capability and never silently change the selected model. Kimi's high-speed model has a distinct ID/context and remains a distinct catalogue entry.

A listed model is labelled as advertised or documented until an actual inference succeeds. Historical errors distinguish quota/rate limiting from model rejection. Refreshing the catalogue does not run an inference sweep.

**Claude Auto mode**

The existing **Enable Claude Auto mode** toggle is preserved and defaults off. It enables Claude's native approval classifier through the ordinary gateway route. It does not select an independent reviewer model.

Claude Desktop 1.52386.3 and the inspected Claude Code builds choose the classifier model internally, using server configuration and provider/model fallbacks. The gateway receives no stable Auto-specific request-role marker. Mapping the classifier's Claude-facing ID affects every request using that ID, including main/background traffic if they share it. Consequently this preview has no misleading independent reviewer picker and does not infer reviewer role from prompt text. **Accept edits**, **Auto**, and **Bypass permissions** remain different Claude permission modes. See `NATIVE-AGENTS.md` for the installed-code trace and current official documentation.

**Sessions and isolation**

The app uses the native third-party profile mechanism used by Ollama. It switches the installed Claude app's profile; it does not launch a concurrent cloned Claude instance. Claude stores first-party and third-party history separately. Ollama and this gateway share Claude's third-party history store, so existing third-party conversations can appear together. Start a fresh conversation when changing incompatible providers or reasoning models.

Profile activation records a recovery journal before changing the dedicated profile, selection metadata, and deployment-mode fields. Restoration changes only still-owned values and preserves changes another manager made in the meantime. An already-running Claude on another profile must be closed before switching. Transcripts are never read, copied, or edited by the profile manager.

**Local data and privacy**

Preview state lives under `~/Library/Application Support/Provider Hub Preview/`. Its default port is 11438; its profile ID is `14c58c94-d7e8-4a15-96b8-81668956e474`. Production Mistral Bridge continues to use its original state directory, port 11436, Keychain namespace, and profile ID.

- `settings.json`: provider connections, credential-source modes/revisions, model mappings, appearance overrides, and preferences; no API keys.
- `catalogues/*.json`: metadata scoped to the provider connection and credential revision. Key replacement invalidates that provider's catalogue.
- `catalog.json`: optional metadata-only import from the stable Mistral build. No keys or transcripts are imported.
- `gateway-token`: private local authentication credential supplied to Claude's profile.
- `reasoning-signing-key`: separate private key used to authenticate Cerebras reasoning replay; never supplied to Claude as a credential.
- `activity.jsonl`: event, real provider/model, HTTP status, and usage metadata. No prompts, tool arguments, response text, or authorization headers.
- `last-request-shape.json`: field/role/content-type names only.
- `profile-transaction.json`: temporary restoration journal, removed after successful recovery.

The gateway binds to literal loopback, authenticates desktop requests, rejects browser-origin requests, and uses the selected provider's credentials for outbound inference. Hosted endpoints are constrained to official HTTPS URLs; Ollama accepts literal loopback HTTP. Native Messages requests preserve protocol version/beta headers, tools, thinking, and IDs without forwarding the local gateway token.

Cerebras reasoning tool turns need replayable assistant reasoning. The adapter returns a thinking block signed over the exact provider/model/account scope, reasoning, visible text, tool IDs/names, and arguments. The next turn validates it before replaying the provider's reasoning field. Foreign or edited traces fail explicitly. Cerebras streams are buffered until the complete envelope can be signed, while gateway ping events keep the connection active. This adds time before visible content compared with native streaming.

**Build and verification**

From the repository root:

```bash
bash Source/build.sh
python3 -m unittest discover -s Source -v
```

Use Python 3.11 or newer; on this Mac the Vibe runtime is `/Users/chrisizatt/.local/share/uv/tools/mistral-vibe/bin/python3`. Building requires Apple's command-line developer tools. The build is ad-hoc signed for local development, not notarized, and does not bundle Python.

`VERIFICATION.md` records the actual tests and their limits. The stable Mistral baseline completed a live Claude read → edit → read cycle before this branch. The preview adds native protocol, credential separation, alias/context, replay, cancellation, and UI checks. It must not be described as live-tested on accounts that have not been configured. No new Mistral inference was spent for this expansion.
