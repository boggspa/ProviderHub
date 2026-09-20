"""Bounded response cleanup and content-free CLI latency measurements."""
from __future__ import annotations

import json
import contextvars
import logging
import os
import queue
import threading
import time
import uuid

_TIMING = contextvars.ContextVar("cli_turn_timing", default=None)


def current_timing():
    return _TIMING.get()


def check_cancelled():
    timing = current_timing()
    if timing is not None and timing.cancel is not None and timing.cancel():
        raise BrokenPipeError("The CLI request was cancelled.")


class _ExitCleanup:
    """One bounded reaper retains resources while an unresponsive child lives."""
    def __init__(self):
        self.slots = threading.BoundedSemaphore(32)
        self.condition = threading.Condition()
        self.pending = []
        self.worker = None

    def add(self, process, callback):
        # Called by cleanup workers, never by the response writer. If every
        # retained child is stuck, backpressure reaches pre-spawn admission.
        self.slots.acquire()
        with self.condition:
            self.pending.append((process, callback))
            if self.worker is None:
                self.worker = threading.Thread(target=self._work, name="cli-exit-cleanup", daemon=True)
                self.worker.start()
            self.condition.notify_all()

    def _work(self):
        while True:
            with self.condition:
                ready = []
                for entry in self.pending:
                    try:
                        if entry[0].poll() is not None:
                            ready.append(entry)
                    except Exception:
                        pass  # An unknown exit state cannot authorize deleting resources.
                for entry in ready:
                    self.pending.remove(entry)
                if not ready:
                    self.condition.wait(.25 if self.pending else None)
                    continue
            for _, callback in ready:
                try:
                    callback()
                except Exception:
                    logging.getLogger(__name__).exception("CLI resource cleanup failed")
                finally:
                    self.slots.release()


_EXIT_CLEANUP = _ExitCleanup()


def cleanup_after_exit(session, callback):
    """Run resource cleanup only after process exit has actually been observed."""
    process = getattr(session, "process", None)
    if process is not None:
        try:
            alive = process.poll() is None
        except Exception:
            alive = True
        if alive:
            _EXIT_CLEANUP.add(process, callback)
            return
    callback()


def observe_events(events, timing):
    """Measure adapter output before a validation/retry buffer can hide it."""
    try:
        while True:
            token = _TIMING.set(timing)
            try:
                event = next(events)
            except StopIteration:
                return
            finally:
                _TIMING.reset(token)
            if timing and event.get("type") in {"text_delta", "thinking_delta", "tool_call"}:
                timing.mark("first_model_event")
            yield event
    finally:
        close = getattr(events, "close", None)
        if callable(close):
            close()


class TurnTiming:
    def __init__(self, provider, model, *, clock=time.monotonic, sink=None):
        self.clock = clock
        self.started = clock()
        self.lock = threading.Lock()
        self.values = {"provider": provider, "model": model, "turn": uuid.uuid4().hex}
        self.sink = sink
        self.delivered = False
        self.cancel = None

    def mark(self, name):
        with self.lock:
            self.values.setdefault(name + "_ms", round((self.clock() - self.started) * 1000, 3))

    def label(self, **values):
        with self.lock:
            self.values.update(values)

    def finish(self):
        # Releasing a pooled session does not shut its process down. Keep that
        # boundary distinct from teardown in cold/warm latency measurements.
        self.mark("lease_release_complete" if self.values.get("cleanup_kind") == "lease_release"
                  else "cleanup_complete")
        with self.lock:
            values = dict(self.values)
        try:
            if self.sink:
                self.sink(values)
            path = os.environ.get("PROVIDER_HUB_CLI_TIMING_LOG")
            if path:
                # No prompts, tool arguments/results, task identifiers or login data.
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(values, sort_keys=True) + "\n")
        except Exception:
            logging.getLogger(__name__).debug("CLI timing log unavailable", exc_info=True)


class CleanupQueue:
    """Reserve capacity before starting a child, never at response completion."""
    def __init__(self, workers=4, capacity=32):
        self.slots = threading.BoundedSemaphore(capacity)
        self.queue = queue.Queue(maxsize=capacity)
        self.workers = workers
        self.lock = threading.Lock()
        self.started = False

    def reserve(self, timeout, cancel=None):
        deadline = time.monotonic() + max(0, timeout)
        while True:
            if cancel is not None and cancel():
                raise BrokenPipeError("The CLI request was cancelled while queued.")
            remaining = max(0, deadline - time.monotonic())
            if self.slots.acquire(timeout=min(.1, remaining)):
                return True
            if remaining <= .1:
                return False

    def submit_reserved(self, callback):
        with self.lock:
            if not self.started:
                for number in range(self.workers):
                    threading.Thread(target=self._work, name=f"cli-cleanup-{number}", daemon=True).start()
                self.started = True
        self.queue.put_nowait(callback)

    def _work(self):
        while True:
            callback = self.queue.get()
            try:
                callback()
            except Exception:
                logging.getLogger(__name__).exception("CLI cleanup failed")
            finally:
                self.slots.release()
                self.queue.task_done()


CLEANUP = CleanupQueue()


class ManagedTurn:
    """Keep the generator and all child-owned files alive until cleanup ends."""
    def __init__(self, factory, timing, timeout, *, cleanup=None, inline_close=False):
        self.factory, self.timing, self.timeout = factory, timing, timeout
        self.cleanup = cleanup or CLEANUP
        self.events = None
        self.closed = False
        self.reserved = False
        self.inline_close = inline_close

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        if self.events is None:
            self.reserved = self.cleanup.reserve(self.timeout, cancel=self.timing.cancel)
            self.timing.mark("queue_ready")
            if not self.reserved:
                self.closed = True
                self.timing.finish()
                return {"type": "error", "message": "CLI cleanup capacity is busy; retry this request."}
        try:
            if self.events is None:
                self.events = iter(self.factory())
            event = next(self.events)
        except BaseException:
            self.close()
            raise
        kind = event.get("type") if isinstance(event, dict) else None
        if kind in {"text_delta", "thinking_delta", "tool_call"}:
            self.timing.mark("first_model_event")
        if kind == "tool_call":
            self.timing.mark("tool_ready")
        return event

    def close(self):
        if self.closed:
            return
        self.closed = True
        if not self.reserved:
            return
        events, timing = self.events, self.timing

        def cleanup():
            timing.mark("cleanup_started")
            try:
                close = getattr(events, "close", None)
                if callable(close):
                    close()
            finally:
                timing.finish()

        if self.inline_close:
            try:
                cleanup()
            finally:
                self.cleanup.slots.release()
        else:
            self.cleanup.submit_reserved(cleanup)
