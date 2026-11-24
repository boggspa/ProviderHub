# Antigravity app harness

`/Applications/Antigravity.app` (Google Antigravity, bundle id `com.google.antigravity`)
is a desktop surface in the Hub beside Claude Desktop, Codex / ChatGPT Desktop and
Vibe. The Hub launches it with its own model catalogue and routing injected, so the
app's agent can use every provider model the Hub serves: API connections and
CLI-backed routes alike, multi-account.

Only the desktop app is targeted. The `agy` CLI route is out of scope.

## How injection works

Injection is environment only. The Hub never patches the app bundle and never
writes Antigravity's configuration files or Google account credentials.

Antigravity's agent speaks Gemini's `generateContent` protocol. Its language
server discovers models from a configured LLM gateway when these variables are
present in its environment:

- `AGY_LLM_GATEWAY_URL` — the loopback gateway base URL;
- `AGY_LLM_GATEWAY_API_KEY` — the local gateway token, read by the client as a
  bearer credential;
- `AGY_LLM_GATEWAY_WIRE_PROTOCOL=genai` — selects the Gemini-protocol surface.

The app inherits the variables for the language server it spawns. The Hub passes
them through `NSWorkspace.OpenConfiguration.environment` at launch
(`AntigravityHarness.launchAntigravity`), so the credential never reaches a file.

## The gateway's Gemini surface

`Source/gemini_bridge.py` is the adapter between the two protocols:

- `GET /v1beta/models` answers the language server's model discovery in Gemini's
  model-list format. Rows come from `antigravity_catalogue.project_antigravity`,
  which projects Hub routes as picker entries; the model id is a flat slug
  (`antigravity_catalogue.slug_for`) because it travels as one URL path segment.
- `POST /v1beta/models/<slug>:generateContent` and `:streamGenerateContent`
  translate the Gemini request into an Anthropic Messages request and hand it to
  the gateway's own `/v1/messages` relay over the loopback. One relay therefore
  serves every protocol: the Antigravity surface shares the routing, compaction,
  metering and credential handling the other surfaces use.
- Responses translate back: content blocks become candidate parts, tool calls
  correlate by name (Gemini carries no call id, so `call_N` ids are minted), the
  stop reason becomes a finish reason, and usage becomes `usageMetadata`.
  Streaming is SSE with one JSON chunk per `data:` frame.

The credential is accepted as `Authorization: Bearer`, `x-api-key`,
`x-goog-api-key`, or a `key` query parameter, matching how Gemini clients send
keys.

## Lifecycle

| Step | Command | Behaviour |
|---|---|---|
| Prepare | `antigravity-prepare` | Freshness-scoped like Codex: checks the selected routes are servable, records the catalogue digest and the default model. |
| Activate | `antigravity-activate` | Requires the running gateway to serve the prepared digest, then returns the projected model names and base URL. |
| Launch | `AntigravityHarness.launchAntigravity` | Requires Antigravity to be quit, prepares and activates, then opens the app with the gateway environment and records the launched pid in `antigravity-session.json`. |
| Register | `antigravity-register` | Records the launched pid so the Hub can tell its own session from one the user started. |
| Restore | `antigravity-restore` | Removes the session record once the app has quit; refused while a Hub-launched Antigravity is still running. |

`AntigravityProfile` owns the session record and the installed-app detection; it
never edits user configuration, so there is nothing to restore inside the app
itself.

## Picker and effort

`project_antigravity` supplies the curated `antigravity_catalogue`, or every
compatible tool-capable route when the selection is "all". The language server
reads display names, context and output limits from the model-list response.

Gateway discovery advertises one entry per model. Hover over a model with
adjustable reasoning, or focus it and press Right Arrow, to open its Thinking
effort menu. Binary switches show Off and On. Routes through the Antigravity
CLI show the levels that provider exposes. The submenu works with provider
colours and logos turned off.

Provider Hub remembers effort selections per conversation and model. New
conversations start with the last effort picked for that model, or its default.
The gateway uses the selected level for conversation turns and preserves
support for saved model IDs with an effort suffix.
