"""Pipe abstraction for the helper protocol (docs/ARCHITECTURE.md, Contract 8 + addendum D-027).

Transport: UTF-8 JSON, one object per line, max 64 KiB per line *including* the newline. A request is ``{"id", "op", "args"}``; the helper answers
each with exactly one ``{"id", "ok": true, "result"}`` or ``{"id", "ok": false, "error": {...}}``; lines with ``"event"`` and no ``"id"`` are
events. This module neither interprets ops nor invents codes: responses and events pass through unchanged.

Writes never block a caller: requests go through a bounded queue to one writer thread. If the helper stops reading, the queue fills and callers get
``HelperUnavailable`` at once (or ``HelperTimeout`` when their wait expires), never a hang.
"""
from __future__ import annotations

import collections
import ctypes
import json
import os
import queue
import socket
import stat
import subprocess
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


class HelperConnectError(Exception):
    """The helper socket cannot be used; ``str(exc)`` says what to do about it. ``kind``: ``missing`` | ``untrusted`` | ``denied`` (no permission on the
    socket file: the node's mode is wrong) | ``not_allowed`` / ``busy`` (the helper's framed refusal) | ``unavailable`` | ``closed`` | ``timeout``."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class RequestTooLarge(Exception):
    """The serialized request would exceed the 64 KiB line limit."""


class Subscription:
    """A bounded event queue. If the consumer is too slow it is flagged ``overflowed`` and should be dropped (it can reconnect)."""

    def __init__(self, link: "HelperLink", maxsize: int = 256, sid: str | None = None) -> None:
        self._link = link
        self.sid = sid  # session the subscriber belongs to: it only receives published results tagged with that session
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
        self._recent: collections.deque[tuple[str | None, dict[str, Any]]] = collections.deque(maxlen=RECENT_RESULTS)  # (session id, result)
        self._wq: queue.Queue[bytes | None] = queue.Queue(maxsize=WRITE_QUEUE)
        self._seq = 0
        self._alive = True
        self.refusal: dict[str, Any] | None = None  # the helper's framed ``REFUSED`` line with a null id (not allowed / another client), if it sent one
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
        """Queue a request and return its id. ``callback(id, response)`` runs on the reader thread; ``response`` is None if the pipe died.
        ``ValueError`` if ``args`` is not strict JSON (NaN / Infinity)."""
        with self._lock:
            if not self._alive:
                raise HelperUnavailable()
            self._seq += 1
            rid = f"w{self._seq}"
            line = json.dumps({"id": rid, "op": op, "args": args if args is not None else {}}, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
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
    def subscribe(self, sid: str | None = None) -> Subscription:
        sub = Subscription(self, sid=sid)
        with self._lock:
            for ev in self._latest.values():
                sub.put(ev)
            for owner, ev in self._recent:
                if owner == sid:
                    sub.put(ev)
            if not self._alive:
                sub.put(CLOSED)
            self._subs.append(sub)
        return sub

    def publish(self, ev: dict[str, Any], sid: str | None = None) -> None:
        """Server-originated event (the final response of a long op, event ``result``), delivered only to subscribers of session ``sid``."""
        with self._lock:
            if ev.get("event") == "result":
                self._recent.append((sid, ev))
            subs = [s for s in self._subs if s.sid == sid]
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
            try:
                self._r.close()  # nothing more will be read; releases a socket makefile's reference
            except (OSError, ValueError):
                pass
            self._fail_all()

    def _dispatch(self, obj: dict[str, Any]) -> None:
        if "id" in obj:
            rid = obj["id"]
            if isinstance(rid, str):  # null / non-string ids (e.g. the BAD_REQUEST for an unparsable line) match nothing: dropped
                with self._lock:
                    cb = self._pending.pop(rid, None)
                if cb is not None:
                    cb(rid, obj)
            elif rid is None and obj.get("ok") is False and isinstance(obj.get("error"), dict) and obj["error"].get("code") == "REFUSED":
                with self._lock:
                    if self.refusal is None:  # the socket service's one line before it closes (D-043); kept for the connect error
                        self.refusal = obj["error"]
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


# ---- the process link (G-142 item 5) ------------------------------------------------------------------------------------------------
class SocketWriter:
    """Write side of a socket as the helper link wants it: whole-buffer writes, and ``close`` half-closes (the helper then reads EOF)."""

    def __init__(self, sock: socket.socket) -> None:
        self._s = sock

    def write(self, data: bytes) -> int:
        self._s.sendall(data)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        try:
            self._s.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        self._s.close()  # the descriptor itself goes when the reader's makefile reference does


def spawn_helper(cmd: list[str]) -> tuple[subprocess.Popen, HelperLink]:
    """Start the helper process with a ``socketpair`` as its stdin and stdout instead of two pipes. A same-user process can reopen a pipe through
    ``/proc/<pid>/fd/N`` and speak to the helper as the GUI; a socket cannot be opened that way (ENXIO). The development / simulator path
    (``--helper CMD``); the installed helper is reached through :func:`connect_helper`."""
    mine, theirs = socket.socketpair()
    try:
        proc = subprocess.Popen(cmd, stdin=theirs.fileno(), stdout=theirs.fileno(), bufsize=0)
    except BaseException:
        mine.close()
        raise
    finally:
        theirs.close()
    rfile = mine.makefile("rb")  # buffered: readline(limit) without a recv per byte; ``mine`` itself stays open for the writer
    return proc, HelperLink(rfile, SocketWriter(mine)).start()


HELPER_SOCKET = "/run/arltuner/helper.sock"  # = arltuner.helper.service.SOCKET_PATH (the GUI does not import the root helper's code)
HANDSHAKE_S = 20.0  # socket activation starts the helper on the first connect; its start (lock, journal recovery) is quick, but never unbounded


def _whoami() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, OSError):
        return os.environ.get("USER") or "YOURNAME"


def connect_helper(
    path: str = HELPER_SOCKET, *, timeout: float = HANDSHAKE_S, lstat_fn: Callable[[str], Any] = os.lstat, owner_uid: int = 0, user: str | None = None,
) -> HelperLink:
    """Connect to the root helper service (D-043) and prove the link works. The node must be a socket owned by ``owner_uid`` (root); then the first request
    (``preflight``, read-only) is the handshake: either it is answered (the helper admitted this user) or the helper's framed refusal / EOF explains why not.
    Raises :class:`HelperConnectError` with a message that names the fix."""
    who = user or _whoami()
    try:
        st = lstat_fn(path)
    except FileNotFoundError:
        raise HelperConnectError("missing", f"the helper is not installed or not started: {path} does not exist. Run: sudo bash install.sh (it enables arltuner-helper.socket)") from None
    except OSError as exc:
        raise HelperConnectError("missing", f"cannot look at {path}: {exc.strerror or exc}") from None
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != owner_uid:
        raise HelperConnectError("untrusted", f"{path} is not a socket owned by root; not connecting to it")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
    except PermissionError:
        sock.close()
        raise HelperConnectError(
            "denied", f"permission denied on the helper socket {path}: its file mode is wrong (it should be root-owned, mode 0666; the helper's allowlist "
            f"decides who may use it). Reinstall to repair it: sudo bash install.sh") from None
    except ConnectionRefusedError:
        sock.close()
        raise HelperConnectError("unavailable", f"nothing is listening on {path}: start it with: sudo systemctl enable --now arltuner-helper.socket") from None
    except OSError as exc:
        sock.close()
        raise HelperConnectError("unavailable", f"cannot connect to {path}: {exc.strerror or exc}") from None
    link = HelperLink(sock.makefile("rb"), SocketWriter(sock)).start()
    try:
        link.call("preflight", {}, timeout=timeout)
    except HelperUnavailable:
        link._thread.join(2.0)  # the framed refusal arrives before the EOF; let the reader finish so it is not missed when the writer failed first
        refusal = link.refusal
        link.close()
        if refusal is not None:
            check = (refusal.get("detail") or {}).get("check") if isinstance(refusal.get("detail"), dict) else None
            if check == "CLIENT_BUSY":
                raise HelperConnectError("busy", "another client is connected to the helper (another arltuner GUI?). Close it, then start this one again.") from None
            if check == "ACCESS_DENIED":
                raise HelperConnectError(
                    "not_allowed", f"the helper refused this user: not in the allowlist (/etc/arltuner/helper.toml). Run: sudo bash install.sh --add-user {who}") from None
            raise HelperConnectError("not_allowed", f"the helper refused the connection: {refusal.get('message', 'no reason given')}") from None
        raise HelperConnectError("closed", "the helper closed the connection right away (it may have refused to start). See: journalctl -u arltuner-helper -n 30") from None
    except HelperTimeout:
        link.close()
        raise HelperConnectError("timeout", f"the helper did not answer within {timeout:.0f} s. See: journalctl -u arltuner-helper -n 30") from None
    return link


def set_undumpable() -> bool:
    """``prctl(PR_SET_DUMPABLE, 0)``: other processes of the same user can no longer ptrace this one or read its /proc/<pid>/fd, /mem, /environ
    (those files become root-owned). Returns False where that is not available (non-Linux, no libc)."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(4, 0, 0, 0, 0) == 0   # PR_SET_DUMPABLE = 4
    except (OSError, AttributeError):
        return False
