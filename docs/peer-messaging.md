# Enrolled thread mailboxes

Provider Hub provides a local directory and durable mailboxes for conversations
that explicitly enroll. Top-level Hub chats get opt-in tools. External adapters
can use the same tools over a thread-bound stdio MCP server or local socket.

This is the first inter-application messaging milestone. It does not enumerate
arbitrary Codex/ChatGPT or Claude Desktop conversation history, patch their UI,
inject a message into an idle desktop agent, or start an agent. A native desktop
adapter must establish an actual conversation binding before it can enroll.

## Identity and authority

The broker assigns a logical UUID to `(app, native_thread_id, workspace)`.
An attachment has a separate instance UUID; reconnecting invalidates the old
instance. Restarting the Host preserves logical identities and mail but
invalidates live principals. PIDs and model names are not thread identities.

Each message captures sender and recipient provenance at send time, including
application, logical/native IDs, model and provider, plus the sender's instance.
Switching a Hub
chat's model updates future messages and does not rewrite old attribution.
The application owns the conversation; the selected model is an attribute.

Hub chat IDs are obtained from the owning Host and marked `native`. An external
endpoint is marked `enrolled`: the broker authenticates its enrollment token,
but does not claim to have verified the supplied native app ID or model.
Tools cannot choose their sender, change workspace, enroll another identity,
or turn an enrolled identity into a native-verified identity.

Message records carry `origin: peer_agent` and `authority: reference_only`.
Their bodies are data even if they claim to be the user, an approval, or a
system instruction. A recipient may coordinate within its existing user task
and permissions; a `delegation` kind does not grant new authority. Replies
cannot authorize themselves through their own text. Filesystem approvals and
the repository's work-claim rules continue to apply.

## Delivery

| State | Meaning |
| --- | --- |
| `queued` | The broker committed the message to durable storage. |
| `presented` | The broker included it in an authenticated mailbox read response. Transport delivery can still be uncertain; this does not prove the adapter or model saw, understood, or acted on it. |
| `acknowledged` | The recipient explicitly acknowledged a presented message. This does not mean it accepted or completed the request. |
| `expired` | Its delivery deadline passed. |

`hub_read_messages` supports an `after` sequence and bounded `limit`. Advance a
client cursor only after handling the response. Repeating a read is safe; use
message IDs to deduplicate. Sends require an idempotency key. Retry an uncertain
send with exactly the same key and content; reusing the key for another message
fails. Expiry and retention bound storage; idempotency is not indefinite.

Mail is polled. No scheduler, wake-up, background inference, automatic reply
or interruption is created. An enrolled Hub model receives guidance to poll
at useful work boundaries, and its reads are normal persisted tool results.
Helpers, Team members, Side Chat and editing lanes do not inherit the parent's
endpoint. Only top-level Hub chats are supported in this first version.
Deleting a Hub chat revokes its endpoint without reading its transcript; saved
mail receipts remain subject to normal retention.

`online` means recent authenticated adapter activity, not that an agent is
thinking. The MCP adapter heartbeats every 30 seconds; leases expire after
120 seconds without activity. Offline endpoints can retain mail. Capabilities
always advertise polling and `wake: false`.

The directory returns at most 128 peers and reports `truncated` when more match;
filter by app or presence to narrow it. It never silently claims to be a complete
inventory of all conversations in an application.

## Local setup

The Hub Chat worker must be running. Both the current stdio UI worker and the
persistent Chat Host serve the peer protocol. These commands use the same
state root as the running worker; pass `--state /absolute/state/path` before
the subcommand for another root. Run source examples with uv CPython 3.13.

Explicitly enable tools for an existing top-level Hub chat:

```sh
uv run --python 3.13 python Source/peer_mcp.py enroll-chat EXISTING_HUB_CHAT_UUID
```

The native UUID is the saved chat's `id` (and its JSONL filename under
`<state>/chats`). Enrollment does not start or resume that chat. Its next model
request can advertise the mailbox tools. Enrollment and revocation are recorded
as visible notices in that chat, with the current enrollment saved in metadata.

Enroll one external conversation and retain its token in a new private file:

```sh
uv run --python 3.13 python Source/peer_mcp.py enroll \
  --app codex_desktop \
  --native-thread-id ACTUAL_NATIVE_THREAD_ID \
  --workspace /absolute/workspace \
  --label 'Workspace reviewer' \
  --model 'codex/gpt-6.1-sol' \
  --provider openai \
  --endpoint-file /private/directory/reviewer-endpoint.json
```

The file is mode 0600, cannot overwrite another enrollment, and must not be
committed, put in a prompt or copied into logs. Only public metadata is printed.
An adapter authenticates with this endpoint token, not with the Host's
administrative credential. Revoke an enrollment with:

```sh
uv run --python 3.13 python Source/peer_mcp.py revoke BROKER_THREAD_UUID
```

Start the MCP adapter for that one conversation:

```sh
uv run --python 3.13 python Source/peer_mcp.py serve \
  --endpoint-file /private/directory/reviewer-endpoint.json
```

The adapter speaks JSONL MCP revision `2025-11-25` over stdio. Tool results
preserve attributed peer data and never masquerade as a human turn. It exposes:

| Tool | Purpose |
| --- | --- |
| `hub_self` | Read bound identity, provenance, presence and capabilities. |
| `hub_list_threads` | Discover enrolled endpoints in the exact canonical workspace, optionally filtered by application or presence. |
| `hub_send_message` | Queue text for an exact endpoint with idempotency and optional reply linkage. |
| `hub_read_messages` | Read the current endpoint's mailbox with a sequence cursor. |
| `hub_ack_message` | Acknowledge receipt of a presented message. |
| `hub_message_status` | Read a receipt for a message the current endpoint sent or received. |

For adapter diagnostics, `call --endpoint-file PATH hub_self` invokes one tool
without starting MCP. A call creates a fresh endpoint instance, so it replaces
any previous attachment for that same enrollment. Do not run diagnostics and
a live adapter concurrently with the same credential.

**Do not configure one enrollment as an app-wide MCP server shared by unrelated
chats.** MCP process identity is not conversation identity. This adapter is
for clients that can launch/bind it per conversation, or a dedicated instance
explicitly confined to one conversation. An application-wide desktop MCP
connection alone cannot supply verified native thread attribution. Turnkey
Codex Desktop and Claude Desktop registration/wake adapters are future work.

## Scope and storage

Discovery and sending are restricted to the endpoint's canonical workspace.
Attached folders, nested directories, distinct worktrees and a common Git
remote do not automatically share scope. Same-workspace enrollment authorizes
peer communication; it does not authorize reading other chat transcripts.
Only message sender and recipient can inspect that message's receipt.

The broker stores threads, hashed enrollment tokens and message/receipt rows in
`<state>/chats/peers/peers.sqlite3`. A private directory, mode-0600 files,
exclusive lifetime lock and transactions protect storage. Plaintext message
bodies are local data; the database is not encrypted. The MCP credential file
holds the plaintext endpoint token. Authentication isolates enrolled adapter
capabilities, not hostile processes already running as the same OS user with
access to the user's files and administrative Host credential.

If mailbox storage cannot be opened, including corruption or a newer schema,
Chat starts with peer messaging disabled and shows a notice. The database is
preserved for recovery; enrollment and peer authentication remain unavailable
until storage is repaired and the worker restarts. Ordinary chats keep working.

Bodies are limited to 16 KiB of UTF-8, reads to 50 messages and pending mail to
256 messages per recipient. Messages expire after seven days; terminal rows
are retained for a further seven days past expiry. Limits and strict argument
validation apply server-side, including when bypassing MCP.

## Acceptance coverage

Tests exercise enrolled peer discovery, explicit Hub enrollment, sender
spoof rejection, workspace isolation, idempotent sends, stale instances,
revocation, read/ack ownership, restart recovery, private credential files and
the stdio MCP schema. Real socket tests verify that peer attachment preserves
the desktop UI, peer mail does not cause provider requests while idle, and a
Hub model reads the message as tool output only after a user starts a turn.

Tests use scripted providers; no native Claude or Codex conversation is sent a
message. Desktop automatic delivery, native-thread attestation and wake-up
remain explicit adapter capabilities to implement and qualify separately.
