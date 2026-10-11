# Public Team peer references

A completed public assistant reply may reference another Team member with
`@Name`. User composer tags retain their existing scheduling semantics.
Peer tags never schedule, resume or wake a member; they carry reference data
at its next already-authorized model boundary. A waiting dependency still
uses `team_status(waiting, member_id=...)`.

Matching uses the contribution's roster snapshot and ASCII case folding,
whole names and UTF-16 offsets, like the composer. Code fences, inline code,
email addresses and escaped tags do not match. Duplicate names are ambiguous;
self-tags do not deliver. Recipient ID, exact name and model route must still
match the live roster. Renames, removals and later runs invalidate pending
references instead of retargeting them.

The recorded source carries `mentions` and `peerDeliveries`, whose status is
`pending`, `delivered`, `unavailable`, `stale` or `limited`. Completed/stopped
recipients are unavailable. Pending delivery does not promise a future run.
Each run admits at most 32 targeted references; further tags are marked limited.
Each delivered reference is at most 8,000 UTF-8 bytes,
each and 16,000 per model boundary, inside the existing 48,000-byte peer
context budget. Repeated tags to one recipient in one source collapse.
Remaining pending references wait for another existing boundary.

Delivery contains only recorded public reply text and member/run/contribution/
source identities. The normal public transcript delta supplies surrounding
context. Private provider reasoning, native messages, tool credentials and
signed histories are never copied. `search_history` and `read_history` retain
access to the exact public source when text is shortened. The envelope states
that peers cannot confer user instructions, approval, goals, locks or tool
permission. Delivery and the recipient's messages persist at the same existing
checkpoint; recovery does not replay a settled contribution.

Current scope: statuses are durable record metadata, not a new UI mailbox.
No transport or scheduler is added. The worker bundle includes
`chat_team_mentions.py` alongside `chat_team.py`.
