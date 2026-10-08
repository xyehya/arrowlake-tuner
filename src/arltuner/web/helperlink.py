"""Pipe abstraction for the helper protocol (docs/ARCHITECTURE.md, Contract 8 + addendum D-027).

Transport: UTF-8 JSON, one object per line, max 64 KiB per line *including* the newline. A request is ``{"id", "op", "args"}``; the helper answers
each with exactly one ``{"id", "ok": true, "result"}`` or ``{"id", "ok": false, "error": {...}}``; lines with ``"event"`` and no ``"id"`` are
events. This module neither interprets ops nor invents codes: responses and events pass through unchanged.

Writes never block a caller: requests go through a bounded queue to one writer thread. If the helper stops reading, the queue fills and callers get
``HelperUnavailable`` at once (or ``HelperTimeout`` when their wait expires), never a hang.
"""
from __future__ import annotations

import collections
import json
import queue
import threading
from typing import Any, BinaryIO, Callable

MAX_LINE = 64 * 1024  # bytes per line, newline included, both directions
WRITE_QUEUE = 32
RECENT_RESULTS = 16
CLOSED = {"event": "__closed__"}  # internal sentinel put on subscriber queues when the pipe ends; never sent to a browser


class HelperUnavailable(Exception):
    """The pipe is closed (helper not started, exited, EOF) or the write queue is full."""


class HelperTimeout(Exception):
    """No response within the timeout (the helper may still be working; watch the events)."""


class RequestTooLarge(Exception):
    """The serialized request would exceed the 64 KiB line limit."""


class Subscription:
    """A bounded event queue. If the consumer is too slow it is flagged ``overflowed`` and should be dropped (it can reconnect)."""

    def __init__(self, link: "HelperLink", maxsize: int = 256) -> None:
        self._link = link
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=maxsize)
        self.overflowed = False

    def put(self, ev: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(ev)
        except queue.Full:
            self.overflowed = True

    def close(self) -> None:
        self._link._unsubscribe(self)


class HelperLink:
    def __init__(self, rfile: BinaryIO, wfile: BinaryIO) -> None:
        self._r, self._w = rfile, wfile
        self._lock = threading.Lock()
        self._pending: dict[str, Callable[[str, dict[str, Any] | None], None]] = {}  # id -> callback(id, response | None)
        self._subs: list[Subscription] = []
        self._latest: dict[str, dict[str, Any]] = {}  # last "state" / "heartbeat" event, replayed to new subscribers
        self._recent: collections.deque[dict[str, Any]] = collections.deque(maxlen=RECENT_RESULTS)  # published "result" events
        self._wq: queue.Queue[bytes | None] = queue.Queue(maxsize=WRITE_QUEUE)
        self._seq = 0
        self._alive = True
        self._thread = threading.Thread(target=self._read_loop, name="helperlink-reader", daemon=True)
        self._wthread = threading.Thread(target=self._write_loop, name="helperlink-writer", daemon=True)

    def start(self) -> "HelperLink":
        self._thread.start()
        self._wthread.start()
        return self

    @property
    def alive(self) -> bool:
        return self._alive

    # ---- requests -------------------------------------------------------------------------------------------------------------
    def submit(self, op: str, args: dict[str, Any] | None, callback: Callable[[str, dict[str, Any] | None], None]) -> str:
        """Queue a request and return its id. ``callback(id, response)`` runs on the reader thread; ``response`` is None if the pipe died."""
        with self._lock:
            if not self._alive:
                raise HelperUnavailable()
            self._seq += 1
            rid = f"w{self._seq}"
            line = json.dumps({"id": rid, "op": op, "args": args if args is not None else {}}, separators=(",", ":")).encode("utf-8") + b"\n"
            if len(line) > MAX_LINE:
                raise RequestTooLarge()
            self._pending[rid] = callback
        try:
            self._wq.put_nowait(line)
        except queue.Full:
            self._drop(rid)
            raise HelperUnavailable() from None
        return rid

    def call(self, op: str, args: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        done = threading.Event()
        box: list[dict[str, Any] | None] = [None]

        def cb(_rid: str, resp: dict[str, Any] | None) -> None:
            box[0] = resp
            done.set()

        rid = self.submit(op, args, cb)
        if not done.wait(timeout):
            self._drop(rid)
            raise HelperTimeout()
        if box[0] is None:
            raise HelperUnavailable()
        return box[0]

    def _drop(self, rid: str) -> None:
        with self._lock:
            self._pending.pop(rid, None)

    def _write_loop(self) -> None:
        try:
            while True:
                line = self._wq.get()
                if line is None:
                    break
                self._w.write(line)
                self._w.flush()
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._w.close()
            except (OSError, ValueError):
                pass
            self._fail_all()

    # ---- events ---------------------------------------------------------------------------------------------------------------
    def subscribe(self) -> Subscription:
        sub = Subscription(self)
        with self._lock:
            for ev in self._latest.values():
                sub.put(ev)
            for ev in self._recent:
                sub.put(ev)
            if not self._alive:
                sub.put(CLOSED)
            self._subs.append(sub)
        return sub

    def publish(self, ev: dict[str, Any]) -> None:
        """Server-originated event (the forwarded final response of a long op, event ``result``) to every subscriber."""
        with self._lock:
            if ev.get("event") == "result":
                self._recent.append(ev)
            subs = list(self._subs)
        for s in subs:
            s.put(ev)

    def _unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    # ---- reader ---------------------------------------------------------------------------------------------------------------
    def _read_loop(self) -> None:
        try:
            while True:
                line = self._r.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:  # longer than 64 KiB including the newline: drop the whole line
                    if not line.endswith(b"\n"):
                        while True:
                            rest = self._r.readline(MAX_LINE)
                            if not rest or rest.endswith(b"\n"):
                                break
                    continue
                try:
                    obj = json.loads(line.decode("utf-8"))
                    if isinstance(obj, dict):
                        self._dispatch(obj)
                except Exception:  # a malformed or odd line never kills the reader; it is dropped
                    continue
        except (OSError, ValueError):
            pass
        finally:
            self._fail_all()

    def _dispatch(self, obj: dict[str, Any]) -> None:
        if "id" in obj:
            rid = obj["id"]
            if isinstance(rid, str):  # null / non-string ids (e.g. the BAD_REQUEST for an unparsable line) match nothing: dropped
                with self._lock:
                    cb = self._pending.pop(rid, None)
                if cb is not None:
                    cb(rid, obj)
            return
        name = obj.get("event")
        if isinstance(name, str):
            with self._lock:
                if name in ("state", "heartbeat"):
                    self._latest[name] = obj
                subs = list(self._subs)
            for s in subs:
                s.put(obj)

    def _fail_all(self) -> None:
        with self._lock:
            self._alive = False
            pending, self._pending = dict(self._pending), {}
            subs = list(self._subs)
        for rid, cb in pending.items():
            try:
                cb(rid, None)
            except Exception:
                pass
        for s in subs:
            s.put(CLOSED)

    def close(self) -> None:
        # The writer thread closes our write end (the helper sees EOF and shuts down); the read end ends when the helper closes its stdout.
        with self._lock:
            self._alive = False
        try:
            self._wq.put_nowait(None)
        except queue.Full:
            pass
        self._fail_all()
