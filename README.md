**Mistral Bridge 0.2.0**

A native macOS menu bar app that connects Claude Desktop to the Mistral API using your existing Vibe sign-in. This is a locally built prototype for Apple Silicon Macs, with its Swift and Python source included.

The app has been tested with Claude Desktop 1.52386.3 and Mistral Vibe 2.25.0. An actual Claude Code session completed a read → edit → read verification through `mistral-vibe-cli-latest`.

**Start using it**

1. Open Mistral Vibe and sign in if needed. Vibe supplies the saved model configuration and credentials; its agent does not need to keep running during inference.
2. Open **Mistral Bridge.app**. Its icon appears in the menu bar. You can move the app into Applications if you want to keep it there.
3. In **Connection**, check that it says **Connected through Vibe Keychain**, Vibe environment, or Vibe .env. A separate Mistral API key can also be stored in macOS Keychain.
4. In **Models**, click **Refresh models** and choose friendly model names for each Claude slot. Enable **Show technical IDs** to see or edit exact API IDs, or choose **Enter a custom model ID…** in a selector. **Use Vibe for all** maps every slot to the actual API ID behind Vibe’s selected model. **Test** sends a small request to the selected route.
5. Finish any current work in Claude and quit Claude. Click **Launch Claude** in Mistral Bridge. The app saves the mappings, starts the gateway, waits for it to be ready, switches to its Mistral profile, and opens Claude.
6. Use Claude normally. Quitting Claude restores the previous profile configuration and, by default, stops the gateway. Mistral Bridge stays in the menu bar.

The default mappings use the installed Vibe model. On the Mac where this build was tested, the displayed alias `mistral-medium-3.5` resolves to API model `mistral-vibe-cli-latest`. The current model catalogue advertises 31 chat IDs, which describe 10 distinct model/context combinations. The app groups 21 duplicate aliases. Discovery does not prove that a model currently has available quota or supports every Claude tool.

**Friendly model names**

Labels use provider metadata and Vibe's active display name where available. A small fallback parser recognises brand spelling, versions, parameter sizes, date suffixes, and qualifiers such as Latest, Preview, and Cloud. For example:

| API ID | Display label |
| --- | --- |
| `mistral-vibe-cli-latest` | Mistral Medium 3.5, using this installation's Vibe metadata and its canonical model group |
| `zai-glm-5-2` | GLM 5.2 |
| `codestral-2508` | Codestral · Aug 2025 |
| `model-name-v1-2504` | Model Name V1 · Apr 2025 |

The exact API IDs remain unchanged in settings and requests. Labels are also returned in the local `/v1/models` catalogue for Claude's picker. An existing Claude instance may need its normal next launch to refresh that catalogue. Raw IDs remain available in tooltips and under **Show technical IDs**.

**Context, availability, and Claude controls**

Context limits come directly from each model's `max_context_length`; there is no user-editable context slider or override. Current examples are 262,144 tokens for Medium, 1,048,576 for GLM 5.2, 131,072 for Ministral 3B, and 256,000 for Codestral. Models sharing a family name but reporting different context limits remain separate catalogue entries. The same upstream model mapped to several Claude slots is advertised once in Claude's picker; the internal slot routes remain valid.

Retired or archived models identified by provider metadata are omitted from the default catalogue. None of the 10 current model groups has a past retirement date in the inspected metadata. Each route is labelled **Advertised · not tested**, **Previously responded**, **Quota or rate limited**, or **Rejected by provider**, based on actual local observations. A quota error does not mark a model as retired. Only the Medium/Vibe route has completed the recorded live tests; no inference sweep of the other models was performed.

Use Claude's own Effort control. The bridge reads `output_config.effort` and follows the installed Vibe Mistral backend's coarse mapping: Low → `none`, Medium/High/Extra/Max → `high`. Explicitly disabling thinking also selects `none`. Models whose metadata says they do not support reasoning omit the parameter. The slider positions are not advertised as different models or falsely presented as five distinct Mistral reasoning levels.

Claude Fast mode is not mapped in this Mistral connection. `mistral-vibe-cli-fast` is an alias of Mistral Small, not a faster service tier of Medium. Mistral's separate Priority Tier needs eligible account/model capacity; no such entitlement was established here. A request explicitly asking for unsupported fast speed receives an explanatory error rather than silently changing models or claiming acceleration.

Claude itself currently retains some Claude-specific UI assumptions. The bridge publishes exact provider limits and applies those limits to requests, but the inspected Desktop discovery code only derives its 1M-context marker from that metadata; its own context meter can still use a Claude preset such as 200k. Arbitrary context-meter values and per-provider Fast/effort UI affordances are not fully configurable through the documented gateway interface. The app does not patch Claude to change those assumptions.

The development copy lives at `/Users/chrisizatt/Documents/Mistral Bridge/`, with the runnable app, source, verification notes, and provider roadmap together. Git is initialized there for ongoing work. Account credentials and runtime state remain in the separate application-support directory and are not included in the shareable ZIP.

**How Claude isolation works**

This follows the native third-party profile approach used by Ollama. It switches the installed Claude app between configurations; it does not launch two independent Claude apps simultaneously. Standard Claude history and third-party history are stored separately by Claude. Mistral and Ollama use the third-party history store, so their saved conversations can appear together. Starting a new conversation leaves existing ones intact.

Mistral Bridge creates its own profile ID. It leaves Ollama’s profile file intact, records the prior selection and deployment modes before activation, and restores the settings it still owns. It refuses to switch an already-running Claude instance. If another app selects a different profile, that selection is preserved. An interrupted transaction is recovered at the next Bridge launch; a running Mistral Claude session can reconnect to its gateway after a Bridge restart.

The profile manager never reads, copies, or writes conversation transcripts. The test recorded 201 existing session files: 200 were byte-identical afterwards, and the remaining usage ledger retained all its original bytes with new usage records appended. The original Ollama profile and profile-selection metadata were byte-identical after restoration.

**Capabilities in this prototype**

| Capability | Status |
| --- | --- |
| Native menu bar and configuration window | Implemented and visually checked |
| Existing Vibe credentials | Verified against macOS Keychain |
| Model discovery and editable Claude slot mappings | 31 advertised IDs grouped into 10 models; exact routes retained |
| Non-streaming text and streaming text | Live tested |
| Claude Desktop Code tools | Live Read/Edit/Read cycle passed |
| Tool IDs, partial JSON arguments, multi-turn results | Covered by live and offline tests |
| System messages between Claude tool turns | Supported and regression tested |
| Cancellation, truncated streams, upstream errors | Offline integration tested |
| Images in user messages | Translation tested offline; not verified live in Claude |
| Reasoning | Uses Claude's Effort request with Vibe's none/high mapping; Claude thinking/signature display is not implemented |
| Token counting | Local estimate; actual usage figures come from Mistral |
| Context limit | Provider-reported for each model; no manual context setting |
| PDF/document uploads | Text documents supported; PDF/base64 documents rejected with a clear error |
| Hosted web search | Disabled in this profile; no hosted search service is provided |
| Cowork VM, audio, advanced hosted tools | Not validated in this prototype |
| Codex Responses API | Not implemented in this Claude-first version |

The adapter supports the Messages subset needed for the verified coding workflow. It rejects unsupported content and hosted tools explicitly. It does not run Vibe’s agent loop, tools, or skills. Claude remains responsible for tool execution and permissions.

**Local data and runtime**

Mistral inference goes to `https://api.mistral.ai/v1/chat/completions`. The local gateway listens only on `127.0.0.1` (port 11436 by default), requires a local token, and rejects browser-origin requests. The Mistral API key is held in worker memory. The bridge does not put it in Claude’s profile or in logs.

App data lives under `~/Library/Application Support/Mistral Bridge/`:

- `settings.json`: model mappings and preferences.
- `catalog.json`: the latest provider metadata, canonical model groups, aliases, context limits, and capabilities.
- `gateway-token`: a private credential for the local gateway, separate from the Mistral key.
- `activity.jsonl`: event, model, status, and usage metadata; no prompts, tool arguments, responses, or authorization headers.
- `last-request-shape.json`: field names, message roles, content-type names, and tool-format names for compatibility diagnostics; no message content.
- `profile-transaction.json`: temporary recovery journal, removed after restoration.

Claude’s own third-party mode stores conversations under its normal `Claude-3p` application-support directory. The Bridge transaction changes only its dedicated profile, selection metadata, and deployment-mode fields.

The app uses the Python runtime supplied by an existing Vibe installation. The tested installation uses `uv` under `~/.local/`. No LiteLLM installation, Docker daemon, local model download, additional OpenAI key, or Ollama dependency is required. No login item is installed. This build targets Apple Silicon and macOS 14 or newer and is ad-hoc signed for local use, not notarized for broad distribution.

**Build and verify**

The source requires the macOS command-line developer tools and Python 3.11 or newer. From the included `Source` folder:

```bash
bash build.sh
python3 -m unittest -v test_bridge
```

Use Vibe’s Python if the shell’s `python3` is older than 3.11. Set `MISTRAL_BRIDGE_BUILD_DIR` to choose an alternate location for Swift’s intermediate files. The application bundle is generated next to the Source directory.

The implementation consists of a SwiftUI/AppKit frontend, a Python standard-library HTTP worker, provider catalogue and label handling, protocol translation, and a transactional Claude profile manager. No changes are made to the installed Claude application bundle, its code signature, Chromium switches, system trust store, or the user’s HOME environment.

For recovery, quit Claude and choose **Restore previous setup** in the app. If the app cannot open, the included worker also exposes a `restore` command through Vibe’s Python. Keep the Bridge data folder until restoration has completed. Removing the app alone does not delete any Claude conversation.

Integration references: [Claude Desktop gateway](https://claude.com/docs/third-party/claude-desktop/gateway), [Claude third-party architecture](https://claude.com/docs/third-party/claude-desktop/overview), [Ollama’s Claude Desktop integration](https://docs.ollama.com/integrations/claude-desktop), [Ollama’s public launcher implementation](https://github.com/ollama/ollama/blob/main/cmd/launch/claude_desktop.go), [Mistral Chat API](https://docs.mistral.ai/api/endpoint/chat).

Mistral Bridge is an independent prototype, not a product published by Mistral, Anthropic, or Ollama.
