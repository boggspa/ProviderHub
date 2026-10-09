# Codex route friction repairs

## Failures reproduced from the 9 October 2026 reports

`Responses request is too large or empty` was Provider Hub's local 32 MiB
request gate. It ran before model selection or image-history reduction, so
the same Luna model could work in a native thread and fail behind the hub.
Image base64, tool schemas and history all count toward this byte limit;
it is separate from the model's token context window.

Ordinary Responses requests now have a 64 MiB working budget. Between that
budget and the 128 MiB hard ingress limit, the gateway returns the client's
`context_length_exceeded` signal so it can compact instead of reconnecting.
Both `/responses/compact` and older clients' checkpoint prompts through
`/responses` can receive the same history up to the hard limit. The Messages
hop also accepts 128 MiB. Provider response/event limits stay at 32 MiB.
Empty requests are malformed requests (400), while requests beyond the hard
limit still fail explicitly (413). This does not promise recovery from an
arbitrarily large single attachment or an indivisible pending tool batch.

`Codex attempted a native CLI tool (imageGeneration)` was a bridge policy
failure, not an image service connection failure. The nested runtime's image
tool was enabled by default while its events were rejected. It is now off in
the default/fallback configuration and enabled per thread for the Codex
Responses surface, which can represent it. Progress and results become
`image_generation_call` transcript items with stable identities. A failed
image stays a failed tool result; the model can continue and use a host tool.
Native shell, file modification, image viewing, plugins and agent spawning
remain controlled by the existing host-tool path.

An invalid host-tool alias or non-object arguments receive a `success:false`
RPC response before anything executes, allowing correction in the same turn.
Three rejected calls are allowed before a terminal protocol error. This
does not authorize an unoffered tool or change approval handling.

Unsupported, remote or oversized media blocks returned by host tools become
explicit placeholders for the Codex model. Other blocks, call/result pairing,
and the host's success/error flag survive. The original host transcript is
unchanged; no remote image is fetched and no pixels are resized or invented.
User attachment validation stays strict. Generated image history uses the
same honest fallback and can be compacted without feeding base64 to the
summarizer. Switching to a text-only route retains an image outcome note.

## Qualification

Deterministic tests exercise the real local HTTP gateway with scripted Codex
app-server events: streamed and buffered images, image failure followed by
host work, interleaved outputs, replay, compaction, request budgets, invalid
tool-call correction, and per-block media recovery. No external model calls
or generated images are used by those tests. Wire fields were checked against
the installed ChatGPT-bundled Codex app-server JSON schema. Actual image
rendering in a running desktop app still needs a tester pass after installing
a build containing these changes; editing source does not update a running
Provider Hub installation.

## Voice with a ChatGPT account

[OpenAI's voice documentation](https://learn.chatgpt.com/docs/features/voice)
describes GPT-Live voice directing work through an existing task's selected
model. Account, rollout and workspace availability still apply. In the
installed desktop bundle inspected on 9 October, voice has a direct ChatGPT
`/realtime/status` path and can attach an existing voice call through
`thread/realtime/start`. Provider Hub itself implements neither endpoint.

This makes voice alongside a hub-backed work model plausible; it does not
establish a tested custom-provider combination. Provider Hub's **Show your
ChatGPT account** setting preserves the sign-in UI. It is not a guarantee of
voice availability or a replacement for ChatGPT plan access. With provider
accent colours enabled, macOS attributes microphone permission to Provider
Hub, as the preference already explains. This repair does not change voice
authentication, endpoint routing, permissions, or feature gates.
