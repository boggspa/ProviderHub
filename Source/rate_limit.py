"""Client-side rate limiting for provider upstreams.

Multi-agent bursts (a parent plus its children and grandchildren) can exceed
a provider's request quota faster than the desktop client's own retry budget
tolerates: every rejected request burns one client retry, and persistently
busy workers terminalise the whole run ("exceeded retry limit"). This module
lets the gateway absorb transient pressure itself:

- request slots are queued, not rejected, while all workers are busy;
- upstream 429/503 responses are retried in-bridge with exponential backoff,
  honoring the provider's Retry-After hint (Mistral sends one on 429);
- an observed limit throttles the whole provider route for a window, so
  sibling agents pause instead of hammering a depleted quota.

Only sustained failures reach the client, and they carry a Retry-After hint
so the client's own retries space out instead of spinning.
"""
from __future__ import annotations

import random
import threading
import time
from email.utils import parsedate_to_datetime


#: How long a request waits for a worker slot before the gateway gives up
#: and returns 429 with a Retry-After hint.
SLOT_WAIT_TIMEOUT = 120.0
#: Retry-After hint sent when no worker slot frees in time.
SLOT_RETRY_AFTER = 5
#: Total upstream attempts per client request, including the first try.
MAX_UPSTREAM_ATTEMPTS = 5
#: First backoff step; doubles per consecutive limit hit.
BACKOFF_BASE = 1.0
#: Ceiling for the computed (pre-jitter) backoff step.
BACKOFF_CAP = 20.0
#: Longest window the gateway will absorb in-bridge. An upstream
#: Retry-After beyond this is handed back to the client instead of
#: holding a worker slot for the whole wait.
THROTTLE_CAP = 60.0
#: Upstream statuses worth absorbing with backoff. Anything else is
#: forwarded immediately: it is either permanent or not time-healable.
RETRYABLE_STATUSES = frozenset({429, 503})
#: Slice size for cancellable waits, mirroring the gateway monitors.
WAIT_SLICE = 0.25


def parse_retry_after(value, now=None):
    """Parse a Retry-After header into seconds, or None when unusable.

    Accepts delta-seconds and HTTP-dates. Past dates, negatives, and
    garbage all yield None so the caller falls back to computed backoff.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        seconds = None
    if seconds is not None:
        return seconds if seconds >= 0 else None
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if moment is None:
        return None
    stamp = moment.timestamp()
    present = time.time() if now is None else now
    return max(0.0, stamp - present)


def backoff_delay(attempt, retry_after=None):
    """Delay before retry ``attempt`` (0-based) in seconds.

    The provider's Retry-After hint is a floor, never a ceiling: the
    gateway waits at least that long, then adds exponential backoff and
    up to a second of jitter so sibling agents do not retry in lockstep.
    """
    floor = max(BACKOFF_BASE * (2 ** max(0, attempt)), retry_after or 0.0)
    return min(floor, THROTTLE_CAP) + random.uniform(0, 1.0)


class ProviderThrottle:
    """Adaptive per-provider gate shared across handler threads.

    An observed 429/503 parks the whole route until ``resume_at``; every
    thread (parent and subagents alike) waits at the gate instead of
    firing requests a depleted quota is certain to reject. A success
    clears the window immediately, so recovery is never slower than the
    next completed request.
    """

    def __init__(self, clock=time.monotonic, sleeper=time.sleep):
        self._clock = clock
        self._sleep = sleeper
        self._lock = threading.Lock()
        self._resume = {}

    def note_limit(self, key, retry_after=None, attempt=0):
        """Park ``key`` after an observed limit; returns the window applied."""
        delay = backoff_delay(attempt, retry_after)
        with self._lock:
            self._resume[str(key)] = self._clock() + delay
        return delay

    def note_success(self, key):
        """Clear any window for ``key`` after a request went through."""
        with self._lock:
            self._resume.pop(str(key), None)

    def wait(self, key, cancel=None, timeout=None):
        """Block until ``key``'s window passes.

        Returns True when the caller may proceed, False when ``cancel``
        fired or ``timeout`` elapsed first. An unset window passes at once.
        """
        key = str(key)
        deadline = None if timeout is None else self._clock() + timeout
        while True:
            with self._lock:
                remaining = self._resume.get(key, 0.0) - self._clock()
            if remaining <= 0:
                return True
            if cancel is not None and cancel():
                return False
            if deadline is not None and self._clock() >= deadline:
                return False
            step = min(WAIT_SLICE, remaining)
            if deadline is not None:
                step = min(step, max(0.0, deadline - self._clock()))
            self._sleep(step)


def wait_for_slot(semaphore, cancel=None, timeout=SLOT_WAIT_TIMEOUT,
                  clock=time.monotonic, sleeper=time.sleep):
    """Acquire ``semaphore`` within ``timeout`` seconds.

    Returns True when acquired (the caller owns one release), False when
    ``cancel`` fired or the wait expired. Polling in slices keeps the
    wait responsive to client disconnects.
    """
    deadline = clock() + timeout
    while True:
        if semaphore.acquire(blocking=False):
            return True
        if cancel is not None and cancel():
            return False
        if clock() >= deadline:
            return False
        sleeper(min(WAIT_SLICE, max(0.0, deadline - clock())))
