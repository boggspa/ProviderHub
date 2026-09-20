"""Exclusive, bounded app-server leases; suspended host calls retain their thread.

The gateway bounds active work. This pool bounds retained idle processes and
retiring children; unrelated active tasks always have exclusive processes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import OrderedDict
import logging
import threading
import time

from cli_lifecycle import check_cancelled


@dataclass(eq=False)
class Lease:
    key: object
    session: object = None
    workspace: object = None
    config: dict = field(default_factory=dict)
    thread_id: str | None = None
    turn_id: str | None = None
    pending: dict | None = None
    fragments: dict = field(default_factory=dict)
    state: str = "busy"
    used: float = 0
    request_key: str | None = None


class SessionPool:
    def __init__(self, dispose, *, capacity=4, idle_ttl=120, pending_ttl=600,
                 clock=time.monotonic):
        self.dispose = dispose
        self.capacity = capacity
        self.idle_ttl, self.pending_ttl = idle_ttl, pending_ttl
        self.clock = clock
        self.condition = threading.Condition()
        self.leases = []
        self.closed = False
        self.reaper = None
        self.requests = {}
        self.completed = OrderedDict()

    def _start_reaper(self):
        if self.reaper is None:
            self.reaper = threading.Thread(target=self._reap, name="codex-pool-reaper", daemon=True)
            self.reaper.start()

    def _expired(self, lease):
        return (lease.session is not None and lease.session.returncode is not None or
                self.clock() - lease.used >= (self.pending_ttl if lease.pending else self.idle_ttl))

    def acquire(self, key, *, match, timeout, request_key=None):
        """Return (lease, cold/warm/resumed); never share an active reader."""
        deadline = self.clock() + timeout
        with self.condition:
            self._start_reaper()
            while True:
                check_cancelled()
                if self.closed:
                    raise RuntimeError("The Codex session pool is closed.")
                for old_key, stamp in list(self.completed.items()):
                    if self.clock() - stamp >= self.pending_ttl:
                        self.completed.pop(old_key)
                if request_key and (request_key in self.requests or request_key in self.completed):
                    raise RuntimeError("This Codex tool continuation has already been consumed.")
                for lease in self.leases:
                    if lease.state == "idle" and self._expired(lease):
                        lease.state = "retiring"
                for lease in self.leases:
                    if lease.key == key and lease.pending and match(lease):
                        # A response may arrive while its previous generator is
                        # still closing. Wait for that owner to release it.
                        if lease.state == "idle":
                            lease.state = "busy"
                            lease.request_key = request_key
                            if request_key:
                                self.requests[request_key] = lease
                            return lease, "resumed"
                        if lease.state == "busy":
                            # Only this task's previous response is releasing
                            # ownership. Other tasks continue on their own PID.
                            break
                else:
                    for lease in self.leases:
                        if lease.key == key and lease.state == "idle" and not lease.pending:
                            lease.state = "busy"
                            lease.request_key = request_key
                            if request_key:
                                self.requests[request_key] = lease
                            return lease, "warm"
                    # Each active host task owns a process. The gateway bounds
                    # active work globally; this pool bounds retained processes,
                    # never serializes unrelated tasks by model/provider.
                    if len(self.leases) >= self.capacity + 32:
                        raise RuntimeError("Codex process cleanup is at capacity; retry shortly.")
                    lease = Lease(key=key, used=self.clock(), request_key=request_key)
                    self.leases.append(lease)
                    if request_key:
                        self.requests[request_key] = lease
                    return lease, "cold"
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for a Codex session slot.")
                self.condition.notify_all()
                self.condition.wait(min(remaining, 0.25))

    def release(self, lease, *, healthy):
        with self.condition:
            if lease not in self.leases:
                return
            lease.used = self.clock()
            if lease.request_key:
                self.requests.pop(lease.request_key, None)
                if healthy:
                    self.completed[lease.request_key] = self.clock()
                    while len(self.completed) > 1024:
                        self.completed.popitem(last=False)
                lease.request_key = None
            lease.state = "idle" if healthy and not self.closed else "retiring"
            idle = sorted((entry for entry in self.leases if entry.state == "idle"),
                          key=lambda entry: entry.used)
            for entry in idle[:max(0, len(idle) - self.capacity)]:
                entry.state = "retiring"
            self.condition.notify_all()

    def _reap(self):
        while True:
            with self.condition:
                for lease in self.leases:
                    if lease.state == "idle" and self._expired(lease):
                        lease.state = "retiring"
                doomed = next((entry for entry in self.leases if entry.state == "retiring"), None)
                if doomed is None:
                    if self.closed and not self.leases:
                        return
                    self.condition.wait(0.5)
                    continue
                doomed.state = "closing"
            try:
                self.dispose(doomed)
            except Exception:
                logging.getLogger(__name__).exception("Codex session cleanup failed")
            finally:
                with self.condition:
                    self.leases.remove(doomed)
                    self.condition.notify_all()

    def close(self, *, timeout=12):
        with self.condition:
            self.closed = True
            for lease in self.leases:
                # Active owners may still be creating the process or reading
                # it. They retire their own lease in release() after observing
                # gateway cancellation; shutdown must not race their reader.
                if lease.state == "idle":
                    lease.state = "retiring"
            self.condition.notify_all()
        if self.reaper:
            self.reaper.join(timeout=timeout)
