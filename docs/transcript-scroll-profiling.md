# Transcript scrolling stall: evidence and follow-up

## Observed on 11 October 2026

The reported trigger was scrolling upward while new Team activity repeatedly
pulled the transcript toward the bottom. The running macOS app was version
0.5.6, build 67, built from `00103653edc4c1af1b705fc0f2ff6bfb1203e4f7` at
00:47 UTC. It predates the subsequent capability and question-card PRs.

Two read-only `sample` captures, about a minute apart, found:

- Roughly 99% process CPU, with the main thread spending 3,017/3,053 and
  869/875 samples in `GraphHost.flushTransactions`. The sample trees repeatedly
  enter `LazySubviewPlacements`, `LazyStack` measurement/placement and native
  transcript text sizing. The second main-thread sample had no blocking
  system-wait leaf frames.
- The app's physical footprint grew from approximately 1.2 GB to 1.4 GB between
  the two captures. This alone does not identify a retained-object leak.
- The Python processes were mostly waiting on I/O or condition variables.
  The active chat's saved data continued updating after the UI froze. The
  inspected history contained over 6,000 entries with no duplicate entry IDs.

The evidence identifies a main-thread SwiftUI layout stall rather than a
worker-wide execution deadlock. It does not identify one framework-internal
instruction as the unique cause of every possible freeze. Raw samples and
user transcript data are not included in the repository.

## Reproduced interaction

The existing transcript used a geometry preference to recompute `nearBottom`
with a 120-point tolerance, then synchronously called `ScrollViewProxy.scrollTo`
from both entry-count and transcript-version updates. A small upward scroll
was therefore still considered follow mode. Layout changes could re-enable
it without any deliberate user request.

A temporary native fixture hosted the actual `ChatTranscript`, with 180
synthetic Markdown replies. After moving up 70 points, it delivered 40 text
updates through the production model. The original view moved the viewport
down by 147 points. With manual-scroll priority, the same scenario kept its
vertical position unchanged. Both bounded runs completed; the prolonged live
freeze itself has not been deterministically reproduced in that fixture.

## Scoped repair

`ChatTranscriptFollow` owns a persistent follow intention independently of
layout estimates. An observer attached to the existing AppKit scroll view
pauses follow on wheel/trackpad, scrollbar and scroll-key input, before
automatic follow can override it. It observes input without consuming it.

Streaming updates schedule at most one follow per 1/30 second, outside the
current SwiftUI update transaction and without animation. Starting a manual
scroll invalidates pending work. Follow resumes only when the user reaches
the actual bottom, chooses **Latest**, or opens a different chat. Loading
earlier messages pauses follow. The view no longer writes follow state from
a geometry-preference callback or unconditionally follows new user rows.

Native tests cover deferred/burst scheduling, cancelling an already queued
follow, pausing just above the bottom, explicit resume, unrelated scroll
views, initial attachment, and observer teardown. Existing transcript and
pane tests continue to cover text sizing and ownership.

The change does not restart the installed app or alter its running work.
Closing and reopening the current Chat window preserves its worker but
reuses the hosting view, so it is not a window reload facility.

## Separate host-lifetime follow-up

The existing Python `ChatHost` already owns sessions, Team execution,
approvals, resource gates, histories and the serial command queue. This
incident does not show that that execution engine is inherently fragile.
Its lifetime and transport remain coupled to the UI:

- `chat_runtime.main` exits on stdin EOF and calls `ChatHost.shutdown`.
- `gateway.serve(..., parent_pipe=True)` also shuts down when its parent pipe
  closes.
- Shutdown cancels work and terminates managed background processes. Saved
  histories and Team journals support recovery; they do not keep live runs
  alive after the owning process exits.
- UI event delivery uses stdout and main-queue callbacks, so a slow/frozen UI
  still needs bounded delivery even after execution is detached.

A lean progression preserves the execution implementation:

| Follow-up | Deliverable | Critical boundary |
| --- | --- | --- |
| 1. Independent lifetime | One per-user host, a private authenticated local socket, explicit attach/detach and shutdown; host-owned gateway lifetime | Closing/reloading the UI must not cancel runs; only an explicit stop/shutdown does |
| 2. Reconnect and bounded delivery | Authoritative snapshots, ordered cursors, coalesced replaceable display events, slow-client handling, stable command IDs and receipts | Dropped/replaced display updates must never drop approvals, answers, tool results or execution receipts |
| 3. Recovery qualification | Crash/reconnect, duplicate client/command, slow reader, approvals, questions and shutdown tests; bounded history projections and memory | Preserve uncertain external-tool outcomes; never automatically replay an action whose completion is unknown |

The first two provide survival across UI reloads or crashes; the third verifies
that separation under failure. A small user-level lifecycle wrapper or launch
agent may supervise the host if required. No additional orchestration engine
is needed. Host restart is a different guarantee from UI restart and must not
be described as transparent execution recovery.

UI-backed tools need an explicit attachment policy. A browser owned by a
window can become unavailable when that window disappears. Such a tool must
report an unavailable or uncertain result accurately, preserve its invocation
identity, and never silently execute against a replacement page.
