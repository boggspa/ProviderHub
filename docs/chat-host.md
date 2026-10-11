# Persistent Chat Host — lifecycle and transport contract

Status: protocol 1, Python side implemented and tested on Linux
([chat_host.py](../Source/chat_host.py),
[gateway_service.py](../Source/gateway_service.py)). The Mac app still runs the
stdio worker; the Swift attach path is the next step (see
[the handoff](reconnectable-host-handoff.md)).

## What lives how long

Four owners, each with its own lifetime. None of them is the lifetime authority
for another, so a freeze, crash or restart of one does not stop the others.

| Owner | Process | Lives until | Ends with it |
| --- | --- | --- | --- |
| Gateway | `gateway.py serve --detach` | an authenticated `POST /_bridge/shutdown` (or SIGTERM) | nothing else: desktop sessions and Chat lose their endpoint until it is started again |
| Chat Host | `chat_host.py serve` | an explicit `shutdown` command (or SIGTERM) | its runs, approvals, questions and background processes |
| Chat window | the Mac UI (today: inside the hub app) | the user closes it, or it crashes or freezes | nothing: it detaches and can reattach |
| Hub UI | the Mac app | the user quits it | nothing in this list, once the app attaches instead of owning |

Consequences:

- Closing, reloading or crashing the Chat window is a **detach**. Runs,
  approvals, questions and background processes continue in the Host.
- Restarting the Chat Host ends its own work (it cannot recover threads), but
  does not restart the gateway, so the hub's desktop sessions keep their
  in-flight requests.
- There is no idle timeout. The last client leaving is not a stop.
- **UI reconnect and Host crash recovery are different guarantees.** A Host
  that dies is restarted from saved state; the existing startup recovery marks
  interrupted work and never replays a provider request or tool action.

## Files

All under `<state root>/host/`, a directory that must be owned by the user,
mode `0700`, and not a symbolic link.

| File | Purpose |
| --- | --- |
| `host.lock` | `flock` held for the Host's whole life. This is ownership. A PID is never evidence of ownership. |
| `host.sock` | `AF_UNIX` stream socket, mode `0600`. Removed at orderly shutdown. A stale socket is replaced only by the process holding the lock; any other file at that path stops startup. |
| `credential` | 32 random bytes (hex), mode `0600`, rewritten at every Host start and removed at shutdown. |

The socket path must fit in 103 bytes (the macOS `sun_path` limit); a longer
state root fails startup with a clear message.

## Starting and attaching

```text
connect(host.sock) ── ok ──────────────────────────────▶ handshake
      │ ENOENT / ECONNREFUSED
      ▼
spawn `chat_host.py serve --detach [--ensure-gateway]`, read one stdout line:
  {"ready": true, "protocol": 1, "generation": …, "pid": …, "socket": …}
  {"ready": false, "reason": "running"}  → another Host owns this root: connect again
  {"ready": false, "reason": "error", "message": …}
```

`--detach` puts the Host in its own session; it ignores SIGHUP and redirects
stdout to `/dev/null` after the readiness line, so a launcher may close its
pipe. Two concurrent launches resolve on the lock: exactly one serves; the
other exits with status 3 after announcing `running`. `chat_host.ensure()` is
the reference implementation of connect-or-launch.

The Host and the stdio worker (`chat_runtime.py`) are mutually exclusive on a
state root: both hold `chats/.lock`.

## Framing

JSON objects, one per line (`\n`), UTF-8, in both directions: the same commands
and events as the stdio worker. A hello line may be at most 64,000 bytes; any
later line at most 12,000,000 bytes (`native_result`) or 2,000,000 bytes
(everything else). An over-long line ends the attachment with
`host_detached` / `frame_too_large`.

## Handshake

The first line from the client:

```json
{"hello": {"protocol": 1, "role": "desktop", "client": "Provider Hub 0.5.6", "credential": "<hex>"}}
```

The Host checks, in order: the peer's user ID from the kernel (`SO_PEERCRED`,
or `LOCAL_PEERCRED` on macOS) equals its own; a complete hello within 5
seconds; a valid role and client label (≤128 characters); the credential
(constant-time comparison); for `desktop`, `protocol == 1`.

Accepted:

```json
{"event": "host", "protocol": 1, "generation": "<per-start id>", "attachment": "<id>",
 "role": "desktop", "pid": 123, "started": "<iso8601>", "build": {"version": "…", "build": "…"},
 "superseded": null}
```

Refused, then closed:

```json
{"event": "host_rejected", "reason": "credential|protocol|invalid_hello|peer|closing",
 "message": "…", "protocol": 1, "build": {…}}
```

A protocol mismatch is reported, never resolved by restarting the Host: live
runs are not silently ended to match a newly opened UI.

## Roles

- **desktop**: the Chat window. At most one is attached at a time; a newer
  desktop attachment supersedes the older one, which receives
  `{"event": "host_detached", "reason": "superseded", "by": "<id>"}`. One window
  therefore never steers another's selection or browser surface. Subscriptions
  are keyed by attachment and dropped when it detaches. The phone keeps its
  own separate `remote` subscription and pairing boundary.
- **control**: status and lifetime only. It accepts any integer protocol, so an
  updated app can retire an older Host. It may send `host_status`, `detach`
  and `shutdown`; anything else is answered with `host_error`. It receives no
  chat events.

## Host commands

Handled by the transport on the same serial owner as chat commands; failures
are answered to the asking attachment as
`{"event": "host_error", "command": …, "message": …}`.

| Command | Effect |
| --- | --- |
| `detach` | Ends only this attachment (`host_detached` / `detached`). |
| `host_status` | Replies `host_status`: generation, pid, build, attachments, `overflows`, `activeChats`, `pendingApprovals`, `pendingQuestions`, running `processes`, last known `gateway`. |
| `shutdown` | Explicit stop: Stop every run, end background processes, close attachments with `host_detached` / `shutdown`, remove the socket and credential, release the lock. `{"ifIdle": true}` refuses while any chat, approval, question or process is active. |
| `gateway_status` | desktop only: replies `{"event": "gateway", "state": "ready"|"stopped", "pid", "active"}`. |
| `gateway_start` | desktop only: ensures a gateway is serving, then refreshes the model catalogue. |

There is deliberately no gateway stop through the Chat Host. Stopping or
restarting the gateway is the hub's decision (`gateway_service.stop`).

Every other command goes to `ChatHost.handle` unchanged. Input a window
sent before it departed (a send, an approval, an answer) still runs; its view
requests (`select`, `subscribe`) do not, so it cannot steer its replacement. The transport rewrites
`subscribe.client` to the attachment ID and stamps `native_result` with the
attachment that sent it; clients cannot choose either.

## Delivery and reconnect

- Events are serialised under one delivery lock (the stdio worker's ordering)
  and queued for the attached desktop. No emitter ever waits for a reader.
- Each attachment's queue is bounded: 64 MiB and 20,000 frames by default
  (`--client-queue-bytes`, `--client-queue-frames`). A single frame is always
  admitted into an empty queue. A client over its bound is disconnected and
  counted in `overflows`; it must reconnect for a fresh snapshot rather than
  continue with silently missing events. An unexplained end of stream means
  the same thing.
- With no desktop attached, chat events are not retained: saved state and the
  next attachment's welcome are the record.
- After a desktop handshake, the Host republishes current state on the serial
  owner: `catalogue`, `chats`, `selected` (the current view with its busy state
  and pending approval), `plugins`, `remote`, then `ready`. The attachment is
  registered before this snapshot is built, so a concurrent update may arrive
  twice (offsets make streamed text idempotent) but not be lost between them.
- Pending approvals and question cards are untouched by detach. They wait for
  an answer from whichever window attaches next.

This is PR 1 of the reconnect work. A cursor-consistent snapshot with an
ordered replay window and durable command receipts (so a reconnecting client
can ask whether its send, approval or answer was accepted) is PR 2.

## Native browser surface

A browser action needs an attached desktop. With none attached it fails before
dispatch: "The Chat window that runs browser actions is not connected. This
action was not executed." An action dispatched to a window that then detaches
or is superseded settles as an **unknown outcome** and is never replayed
against another window. A result from a different attachment than the one the
action was sent to is rejected. The stdio worker, whose parent is its only
window, is unchanged.

## Gateway service

`gateway_service.py ensure|status|stop [--if-idle]` (also importable):

- `ensure` attaches to the state root's gateway if its lock is held and it
  answers an authenticated `/_bridge/status`, and otherwise starts
  `gateway.py serve --detach` in its own session. Concurrent callers end with
  one gateway; `started` says whether this call started it.
- `stop` sends the authenticated `POST /_bridge/shutdown`
  (`{"ifIdle": bool}`; `409` while requests are active and `ifIdle` is set) and
  waits until the gateway's lock is released. An accepted stop closes
  admission in the same critical section as the idle check: a request that
  arrives afterwards gets `503` before any upstream call, instead of being cut
  off mid-flight.
- `gateway.py serve --parent-pipe`, the app's current mode, keeps its parent
  pipe and also accepts the authenticated stop.
- The stop is authorised by the gateway token, the same credential desktop
  sessions use for requests. That grants no more than a same-user process
  already has (it can read the token file or signal the gateway); it is not a
  boundary against code running as the user.

## Updates and version changes

- The Host imports the worker's lazily imported modules at startup, so a
  bundle replaced underneath a running Host does not mix code versions on
  a later lazy import.
- An app must not replace its bundle while a Host from it holds active work.
  Use a control connection: `host_status`, then `shutdown` with `ifIdle`, or
  defer the update. `chat_host.py stop --if-idle` does the same from a shell.

## Verification

`Source/test_chat_host.py` runs real Host processes against a scripted loopback
Messages endpoint: a killed window process while a run streams and a background
process runs; detach and reconnect around a pending question and a pending
approval; a reader that stops reading under a deliberately small frame bound;
duplicate launches, wrong credentials and protocols, malformed and oversized
hellos, superseding windows, the stdio worker exclusion; stale and unexpected
socket files and a symlinked Host directory; and the gateway outliving a
killed, restarted and shut-down Host. `Source/test_gateway_service.py` covers
the shared detached gateway, the authenticated stop and the idle refusal.
They were run on Linux for this change; the repository's CI runs the
Python suite on macOS. The Swift side is not part of this change.
