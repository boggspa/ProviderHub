# Claude API continuation from Codex

Provider Hub translates Codex/ChatGPT desktop Responses requests to Anthropic
Messages when the Claude connection uses API credentials. Responses can ask
for another output after an assistant commentary message or a saved summary.
Current Claude models reject that assistant-ending history as unsupported
assistant prefill.

For this API Responses path, the gateway preserves the completed assistant
history and appends a separate, stable user-role continuation request. It
does not relabel the assistant text as user instructions, change native
thinking signatures, or fabricate tool outcomes. A trailing tool call without
results is rejected locally with a missing-results explanation. Histories
already ending in user input or tool results are unchanged.

The correction runs in the API Responses translation before request sizing
and cache-marker placement. It checks the last user/assistant message even
when system/developer instructions follow. Direct Messages callers, lean
Chat, CLI-backed Claude and other providers keep their existing behavior.

An authenticated, empty reasoning receipt precedes the response's output.
It records the fixed continuation-boundary version so the next
full-history Responses request can recreate the same user-turn boundary
before replaying new signed thinking or assistant text. This applies even
to text-only replies. The receipt is scoped to the model/account and does
not contain provider reasoning or arbitrary instructions. It also works
when ordinary compaction summarizes older input but retains the recent
receipt. Native thinking remains subject to the provider's independent
prefix-binding policy if the history changes.

This is separate from the existing normalization of subagent messages into
attributed user-role input. Tests cover both streaming and buffered requests
through the local Responses gateway against a mock Anthropic endpoint that
rejects assistant prefill. No external inference is used.

See [Anthropic's prefill error documentation](https://platform.claude.com/docs/en/api/errors#prefill-not-supported).
