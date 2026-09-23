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
from collections import deque
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
RECOVERY_SUCCESSES = 8


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
        self._limited_at = {}

    def note_limit(self, key, retry_after=None, attempt=0):
        """Park ``key`` after an observed limit; returns the window applied."""
        delay = backoff_delay(attempt, retry_after)
        with self._lock:
            key = str(key)
            now = self._clock()
            self._resume[key] = max(self._resume.get(key, 0.0), now + delay)
            self._limited_at[key] = now
        return delay

    def note_success(self, key, started_at=None):
        """Clear the window unless this request predates a newer limit."""
        with self._lock:
            key = str(key)
            if started_at is not None and started_at < self._limited_at.get(key, 0.0):
                return False
            self._resume.pop(key, None)
            return True

    def remaining(self, key):
        """Check a gate again after slot admission, without sleeping in a slot."""
        with self._lock:
            return max(0.0, self._resume.get(str(key), 0.0) - self._clock())

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


class ProviderAdmission:
    """Share concurrent slots fairly across waiting providers and back off busy ones."""

    def __init__(self, semaphore, capacity=8):
        self._semaphore = semaphore
        self._capacity = capacity
        self._condition = threading.Condition()
        self._waiting = {}
        self._order = deque()
        self._active = {}
        self._limits = {}
        self._successes = {}

    def acquire(self, key, cancel=None, timeout=SLOT_WAIT_TIMEOUT):
        """Admit one attempt, round-robin across providers with capacity."""
        key = str(key)
        ticket = object()
        deadline = time.monotonic() + timeout
        with self._condition:
            if key not in self._waiting:
                self._waiting[key] = deque()
                self._order.append(key)
            self._waiting[key].append(ticket)
            while True:
                if cancel is not None and cancel():
                    self._remove(key, ticket)
                    return False
                eligible = next((provider for provider in self._order
                                 if self._active.get(provider, 0) < self._limits.get(provider, self._capacity)), None)
                if (eligible == key and self._waiting[key][0] is ticket
                        and self._semaphore.acquire(blocking=False)):
                    self._active[key] = self._active.get(key, 0) + 1
                    self._remove(key, ticket)
                    if key in self._waiting:
                        self._order.remove(key)
                        self._order.append(key)
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._remove(key, ticket)
                    return False
                self._condition.wait(min(WAIT_SLICE, remaining))

    def _remove(self, key, ticket):
        queue = self._waiting[key]
        queue.remove(ticket)
        if not queue:
            del self._waiting[key]
            self._order.remove(key)
        self._condition.notify_all()

    def release(self, key):
        with self._condition:
            key = str(key)
            self._active[key] -= 1
            if not self._active[key]:
                del self._active[key]
            self._semaphore.release()
            self._condition.notify_all()

    def note_limit(self, key):
        with self._condition:
            key = str(key)
            current = self._limits.get(key, self._capacity)
            self._limits[key] = max(1, current // 2)
            self._successes[key] = 0
            self._condition.notify_all()

    def note_success(self, key):
        with self._condition:
            key = str(key)
            current = self._limits.get(key, self._capacity)
            if current >= self._capacity:
                return
            self._successes[key] = self._successes.get(key, 0) + 1
            if self._successes[key] >= RECOVERY_SUCCESSES:
                self._limits[key] = current + 1
                self._successes[key] = 0
                self._condition.notify_all()
