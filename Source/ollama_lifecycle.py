"""Idle eviction for the models Ollama keeps resident after a turn.

Ollama's scheduler holds a model in memory for five minutes after the
request that loaded it — ``keep_alive`` defaults to ``5m``, and the daemon
re-arms that lease at the end of every turn. Inside one conversation that
is exactly right: a steer, a queued message or an immediate follow-up
finds the weights already loaded. Once the conversation is finished it is
the opposite. An abandoned 30B model holds its memory for five more
minutes, and a session that moves between local models leaves each one
resident behind it until the daemon is restarted.

``keep_alive`` is a native-API field, and the two endpoints this hub
actually talks to are compatibility layers: Anthropic Messages at
``/v1/messages`` and OpenAI Responses at ``/v1/responses``. Neither maps
it — a ``/v1/messages`` request carrying ``keep_alive: "90s"`` still
leaves the runner at the daemon default (checked against Ollama 0.33.3).
The lease has to be set through a separate native call instead, once the
turn's own upstream connection is closed so the daemon cannot re-arm the
default behind it.

That is all this module does. After a turn on an Ollama route it asks the
daemon which models are resident and, when the turn's model is one of
them, POSTs ``/api/generate`` with no prompt and the configured
``keep_alive``. The daemon answers ``done_reason: "load"`` in a few
milliseconds without generating a token and re-arms that runner's expiry
to the hub's lease. Eviction itself stays the daemon's own job, which is
what makes the lease outlive the hub: a hub that is quit or crashes
between turns has already handed the runner its short expiry.

The residency check is not an optimisation. A no-prompt ``/api/generate``
against a model that is *not* loaded is a preload — it would pull the
weights into memory, the exact opposite of the point — so a model the
daemon does not report is left alone. Cloud-tagged models never appear
there, so they are skipped for free, and so is a model another client
already unloaded.

Nothing here is allowed to fail a turn. The client has been answered by
the time it runs, so a daemon that is slow, wedged or gone is reported as
"unreachable" and forgotten.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import urllib.parse

#: The lease handed to a model the hub has just used, in seconds, when the
#: connection does not name its own. Long enough for a steer, a queued
#: message or a follow-up to land on warm weights; short enough that a
#: finished conversation does not hold memory for the daemon's five
#: minutes.
DEFAULT_LEASE_SECONDS = 90

#: Ollama reads a negative keep_alive as "never expire". Kept as a named
#: value because it is the one setting that switches idle eviction off
#: without the hub having to stop managing the lease.
KEEP_RESIDENT = -1

#: A day. Not a daemon limit — a bound on what the settings file can ask
#: for, so a typo cannot pin a model for a month while still reading like
#: an ordinary duration.
MAX_LEASE_SECONDS = 86_400

#: Best effort, after the client already has its answer: a wedged daemon
#: costs one handler thread this long and nothing else.
TRANSPORT_TIMEOUT = 5


def lease_seconds(connection) -> int:
    """The keep_alive this connection hands its models, in seconds."""
    value = (connection or {}).get("idle_unload_seconds")
    return DEFAULT_LEASE_SECONDS if value is None else int(value)


def canonical_model(model: str) -> str:
    """``gemma3`` -> ``gemma3:latest``; an explicit tag is left alone.

    The daemon reports resident models by their canonical ``name:tag``.
    A route may carry either form, and a namespace (``hf.co/user/model``)
    may contain slashes but never a tag, so only the last segment decides.
    """
    if not isinstance(model, str) or not model.strip():
        return ""
    model = model.strip()
    return model if ":" in model.rpartition("/")[2] else model + ":latest"


def daemon_url(base_url: str, path: str) -> str:
    """Join a validated loopback daemon base with a native API path.

    Settings validation already constrains the Ollama base to loopback
    HTTP, but this module builds its own requests rather than inheriting a
    planned one, so it repeats the check instead of trusting its caller.
    """
    parsed = urllib.parse.urlsplit(base_url or "")
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("The Ollama daemon is addressed over loopback HTTP.")
    if not ipaddress.ip_address(parsed.hostname).is_loopback:
        raise ValueError("The Ollama daemon must be a loopback address.")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def resident_models(base_url: str, *, transport) -> set[str]:
    """Canonical names of the models the daemon currently holds in memory."""
    raw = transport({"method": "GET", "url": daemon_url(base_url, "/api/ps"),
                     "headers": {"Accept": "application/json"}, "body": None})
    resident = set()
    for entry in (raw or {}).get("models", []) or []:
        if not isinstance(entry, dict):
            continue
        # name and model are the same string in practice; both are read so
        # a daemon that fills only one still matches.
        for field in ("name", "model"):
            named = canonical_model(entry.get(field) or "")
            if named:
                resident.add(named)
    return resident


def release(base_url: str, model: str, seconds: int, *, transport) -> str:
    """Re-lease one resident model, and report what was done to it.

    ``absent`` means the daemon is not holding the model — a cloud route,
    an already-evicted runner, or a turn that never reached the weights.
    Those are left untouched precisely because the same call would load
    them. ``unreachable`` is the daemon failing to answer, which is not
    this hub's emergency: the runner keeps whatever expiry it had.
    """
    wanted = canonical_model(model)
    if not wanted:
        return "absent"
    try:
        if wanted not in resident_models(base_url, transport=transport):
            return "absent"
        transport({"method": "POST", "url": daemon_url(base_url, "/api/generate"),
                   "headers": {"Content-Type": "application/json", "Accept": "application/json"},
                   "body": {"model": wanted, "keep_alive": int(seconds)}})
    except (OSError, ValueError, TypeError, KeyError):
        return "unreachable"
    if seconds < 0:
        return "pinned"
    return "unloaded" if seconds == 0 else "leased"


def http_transport(timeout: int = TRANSPORT_TIMEOUT):
    """The daemon transport used in production: loopback, short, closed.

    Kept behind the same injectable contract the rest of the module takes
    so tests never open a socket, and deliberately not the gateway's
    pooled upstream path: an inference connection's minutes-long timeout
    is the wrong budget for a housekeeping call the client is no longer
    waiting on.
    """
    def transport(plan):
        parsed = urllib.parse.urlsplit(plan["url"])
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 11434, timeout=timeout)
        try:
            body = plan.get("body")
            encoded = json.dumps(body).encode() if body is not None else None
            connection.request(plan.get("method", "GET"), parsed.path, encoded, plan.get("headers", {}))
            response = connection.getresponse()
            # A 404 is an ordinary answer here (the model was removed
            # between the turn and this call), so the status is read
            # rather than raised on, and only a body worth parsing is.
            raw = response.read(65536)
            if response.status != 200 or not raw:
                return {}
            return json.loads(raw)
        finally:
            connection.close()
    return transport
