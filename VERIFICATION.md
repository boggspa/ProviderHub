**Mistral Bridge — verification on 12 September 2026**

**0.2.0 update:** 27 offline tests pass. These include exact per-model context limits, preserving distinct context variants, alias grouping, retirement metadata, treating a 429 as a quota/rate event rather than model retirement, mapping Claude's Effort request without changing the model, and refusing to fabricate Fast mode. A fresh metadata-only `/v1/models` read returned 31 chat IDs grouped into 10 model/context combinations, with 21 aliases grouped and zero IDs marked retired. No additional inference requests were made for this update because the user's quota may be exhausted. The native context/effort sliders in the Bridge app have been removed. Claude's own exact context-meter and Fast-mode UI limitations are documented in the guide.

**0.1.1 update:** rebuilt and opened from `/Users/chrisizatt/Documents/Mistral Bridge/`. The 22 existing protocol, cancellation, and restoration tests pass. The native model controls display friendly labels, and the local `/v1/models` endpoint returns `Mistral Medium 3.5 · Vibe` while preserving all five routing IDs. The model display changes do not alter upstream model selection. Claude was not restarted for this display-only update; its next launch will refresh the model catalogue. The live Desktop read/edit/read evidence below was established with 0.1.0.

The app was compiled for Apple Silicon with Swift 6.2.4, opened through macOS, and tested with Claude Desktop 1.52386.3 and Vibe 2.25.0.

| Check | Result |
| --- | --- |
| Offline regression suite | 27 tests passed in 0.2.0 |
| App signature | Ad-hoc signature verified with `codesign --verify --strict` |
| Existing credential | Vibe Keychain credential resolved without printing or copying it to the app bundle |
| Live Mistral discovery | 31 chat models returned |
| Live app Test connection button | Returned “Mistral Bridge is connected.” |
| Live adapter test | Four streamed turns: read file, write file, read again, final confirmation |
| Actual Claude deployment | Account menu displayed Mistral Bridge; model picker displayed `mistral-vibe-cli-latest` |
| Actual Claude Code tool cycle | Read, edit, read back, and final confirmation completed |
| Fixture result | `colour=blue` became `colour=green` |
| Automatic restoration | Previous deployment-mode values and selected profile restored after Claude quit |
| Automatic gateway shutdown | App showed Stopped after the Claude test ended |
| Saved Ollama profile | Byte-identical to the pre-test file |
| Profile selection metadata | Byte-identical to the pre-test file |
| Existing session files | 200 unchanged; the one changed usage ledger retained all original bytes and appended 343 bytes of new usage records |
| Existing session files deleted | Zero |

The actual Desktop test found that Claude inserts system messages between tool turns. The adapter now preserves those messages, and an offline regression test covers the behavior. An earlier rejected request remains visible in the disposable test conversation; the subsequent retry completed successfully after the correction.

The standalone streamed API test used 582 reported input tokens and 67 output tokens over four requests. These figures cover that small synthetic test only, not model discovery, the UI’s connection tests, or Claude’s larger coding prompts.

The offline suite checks structured messages, instruction roles, image translation, tool-name normalization, tool-call ID consistency, parallel tool calls, interleaved JSON deltas, nullable tool arrays, truncated streams, cancellation, rate-limit translation, local authentication, Origin/Host rejection, context budgeting, profile restoration, concurrent configuration changes, interrupted writes, symlink rejection, and refusing to switch a running Claude instance.

Live tests established the coding workflow with the Vibe Medium route. They do not establish compatibility with every discovered model or with Cowork, audio, PDF uploads, hosted web search, or all future Claude protocol extensions.
