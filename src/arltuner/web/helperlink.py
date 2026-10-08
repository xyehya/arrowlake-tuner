"""Pipe abstraction for the helper protocol (docs/ARCHITECTURE.md, Contract 8).

Transport: UTF-8 JSON, one object per line, max 64 KiB per line. A request is ``{"id", "op", "args"}``; the helper answers each with exactly
one ``{"id", "ok": true, "result"}`` or ``{"id", "ok": false, "error": {...}}``; lines with ``"event"`` and no ``"id"`` are events.
This module neither interprets ops nor invents codes: responses and events pass through unchanged.
"""
from __future__ import annotations

import json
import queue
import threading
from typing import Any, BinaryIO

MAX_LINE = 64 * 1024
CLOSED = {"event": "__closed__"}  # internal sentinel put on subscriber queues when the pipe ends; never sent to a browser


class HelperUnavailable(Exception):
    """The pipe is closed (helper not started, exited, or EOF)."""


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
        self._wlock = threading.Lock()
        self._lock = threading.Lock()
        self._pending: dict[str, list] = {}  # id -> [Event, response|None]
        self._subs: list[Subscription] = []
        self._latest: dict[str, dict[str, Any]] = {}  # last "state" / "heartbeat" event, replayed to new subscribers
        self._seq = 0
        self._alive = True
        self._thread = threading.Thread(target=self._read_loop, name="helperlink-reader", daemon=True)

    def start(self) -> "HelperLink":
        self._thread.start()
        return self

    @property
    def alive(self) -> bool:
        return self._alive

    # ---- requests -------------------------------------------------------------------------------------------------------------
    def call(self, op: str, args: dict[str, Any] | None = None, timeout: float = 120.0) -> dict[str, Any]:
        with self._lock:
            if not self._alive:
                raise HelperUnavailable()
            self._seq += 1
            rid = f"w{self._seq}"
            slot: list = [threading.Event(), None]
            self._pending[rid] = slot
        line = json.dumps({"id": rid, "op": op, "args": args if args is not None else {}}, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(line) > MAX_LINE:
            self._drop(rid)
            raise RequestTooLarge()
        try:
            with self._wlock:
                self._w.write(line)
                self._w.flush()
        except (OSError, ValueError):
            self._drop(rid)
            self._fail_all()
            raise HelperUnavailable() from None
        if not slot[0].wait(timeout):
            self._drop(rid)
            raise HelperTimeout()
        if slot[1] is None:
            raise HelperUnavailable()
        return slot[1]

    def _drop(self, rid: str) -> None:
        with self._lock:
            self._pending.pop(rid, None)

    # ---- events ---------------------------------------------------------------------------------------------------------------
    def subscribe(self) -> Subscription:
        sub = Subscription(self)
        with self._lock:
            for ev in self._latest.values():
                sub.put(ev)
            if not self._alive:
                sub.put(CLOSED)
            self._subs.append(sub)
        return sub

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
                if len(line) > MAX_LINE and not line.endswith(b"\n"):
                    while True:  # discard the rest of an oversize line
                        rest = self._r.readline(MAX_LINE)
                        if not rest or rest.endswith(b"\n"):
                            break
                    continue
                try:
                    obj = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(obj, dict):
                    self._dispatch(obj)
        except (OSError, ValueError):
            pass
        finally:
            self._fail_all()

    def _dispatch(self, obj: dict[str, Any]) -> None:
        if "id" in obj:
            rid = obj["id"]
            if isinstance(rid, str):
                with self._lock:
                    slot = self._pending.pop(rid, None)
                if slot is not None:
                    slot[1] = obj
                    slot[0].set()
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
            pending, self._pending = list(self._pending.values()), {}
            subs = list(self._subs)
        for slot in pending:
            slot[0].set()  # slot[1] stays None -> HelperUnavailable
        for s in subs:
            s.put(CLOSED)

    def close(self) -> None:
        # Only our write end is closed here (the helper sees EOF and shuts down); the read end ends when the helper closes its stdout.
        try:
            self._w.close()
        except OSError:
            pass
        self._fail_all()
