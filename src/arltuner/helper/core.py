"""The helper process: contract 8 over stdin/stdout (or, G-144, over one Unix-socket connection at a time), SPEC 4.6 state machine, heartbeat, one machine-wide instance.

Threads: the caller's thread runs the dispatch loop; a reader thread turns stdin lines into frames (so a signal or a shutdown never waits on a
blocked read); a heartbeat thread; at most one worker thread for the single long op; one telemetry emitter. Every write to stdout goes through one
locked :class:`Writer`. Shutdown (EOF, SIGTERM, SIGHUP, SIGINT, a broken stdout) sets one flag that also sets the running op's cancel flag: the
current mailbox transaction finishes, no new one starts, the op sends its one final response, telemetry stops, the process exits.

Two lifetimes (D-043): the **process** (instance lock, ``tuned_this_boot``, journal recovery: :meth:`Helper.start` / :meth:`Helper.stop`) and a **connection**
(:meth:`Helper.serve`: writer, reader, heartbeat, dispatch loop, and at its end the same orderly stop as on EOF). ``run`` is both around one stdio pair.
The socket service (:mod:`arltuner.helper.service`) calls ``start``, then ``serve`` once per accepted client, then ``stop``; between clients the helper is
idle and ready (no stale request ids, no writer, no telemetry).
"""
from __future__ import annotations

import collections
import os
import queue
import signal
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, BinaryIO

from .config import HelperConfig
from .ops import BudgetMeter, OpContext, OpSpec, builtin_ops, write_gate
from .protocol import (
    BAD_REQUEST,
    BUSY,
    CANCELLED,
    INTERNAL,
    MAX_LINE,
    REFUSED,
    UNKNOWN_OP,
    Frame,
    OpError,
    ProtocolError,
    Request,
    Writer,
    error_response,
    ok_response,
    parse_request,
    read_frame,
)
from .state import Busy, State, StateMachine
from .trust import TrustError, open_checked

__all__ = ["EXIT_INSTANCE_BUSY", "Helper", "InstanceBusy", "InstanceLock"]

EXIT_REFUSED_TO_START = 77  # the lock file is not trustworthy
EXIT_INSTANCE_BUSY = 75  # EX_TEMPFAIL: another helper holds the machine-wide instance lock
_POLL_S = 0.1
SEEN_MAX = 1024  # request ids remembered for the uniqueness rule; the oldest finished one is forgotten beyond this (an id older than 1024 requests may be reused)
QUEUE_MAX = 64  # requests read from stdin but not yet dispatched; beyond this a request is refused with BUSY (detail.queue = "full"), never buffered
SLOW_MAX = 4  # read-only ops (preflight, audit.tail) that may run at the same time, each on its own thread; the 5th is refused with BUSY
_SLOW_JOIN_S = 5.0


class InstanceBusy(Exception):
    pass


class InstanceLock:
    """Exactly one helper per machine: an ``flock`` held for the life of the process on ``<lock_path>.helper``, a sibling of the guard's lock
    file (the guard creates and removes ``lock_path`` itself around every transaction, so it cannot double as a lifetime lock). Released by the
    kernel when the process dies; the file is never deleted (no unlink race)."""

    def __init__(self, lock_path: os.PathLike[str] | str, *, euid: int | None = None) -> None:
        path = os.fspath(lock_path)
        self.path = path + ".helper"
        self._euid = getattr(os, "geteuid", lambda: 0)() if euid is None else euid
        self._fd: int | None = None

    def acquire(self) -> None:
        """``TrustError`` if the file is a symlink, foreign-owned or writable by others (``LOCK_UNTRUSTED``): never followed or taken over."""
        try:
            import fcntl
        except ImportError as exc:  # not POSIX: the helper is Linux-only
            raise InstanceBusy("the instance lock needs fcntl (Linux)") from exc
        fd = open_checked(self.path, write=True, create=True, euid=self._euid, what="instance lock file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                holder = os.pread(fd, 32, 0).decode("ascii", "replace").strip() or "unknown pid"
            except OSError:
                holder = "unknown pid"
            os.close(fd)
            raise InstanceBusy(f"another helper holds {self.path} ({holder})") from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing drops the flock
            self._fd = None


@dataclass
class _Telemetry:
    op_id: str
    sampler: Any
    stop: threading.Event
    thread: threading.Thread
    interval_s: float = 1.0


class Helper:
    def __init__(self, cfg: HelperConfig) -> None:
        self.cfg = cfg
        self.machine = StateMachine()
        self.ops: dict[str, OpSpec] = {**builtin_ops(), **dict(cfg.extra_ops)}
        self._writer: Writer | None = None
        self._shutdown = threading.Event()
        self._signal: int | None = None  # set by the signal handler (a plain attribute: no lock may be taken in a handler)
        self._seen: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._slow: set[threading.Thread] = set()
        self._slow_lock = threading.Lock()
        self._inflight: dict[str, tuple[str, dict[str, Any]]] = {}
        self._cancel: threading.Event | None = None
        self._worker: threading.Thread | None = None
        self._tele: _Telemetry | None = None
        self._tele_lock = threading.Lock()
        self._lock: InstanceLock | None = None
        self.peer_uid: int | None = None  # socket service: the uid of the connected client (SO_PEERCRED), None on stdio
        self.socket_mode = False  # set by the socket service: the invoker is the peer uid, never an environment variable
        self.budget = BudgetMeter(self)  # heartbeat ops_used / ops_budget (G-107)
        self.sim_time: Any = None  # (virtual clock, anchored wall clock) of the simulator, created by the first vf.dump
        self.last_words: dict[int, list[int]] | None = None  # G-112: cpu -> [OC word, point 1, point 2, ...] after the last complete tune.read / vf.dump (and the read-backs of tune.apply); None = unknown
        self.fused_words: dict[int, int] = {}  # D-036: cpu -> the live 0x09 P1=0x11 P2=0 word of the last tune.read (every CPU of a unit shares its first CPU's word); {} = not read
        self.tuned_this_boot = False  # G-120/G-122: a V/F write happened this boot (tune.apply sets it; ``run()`` derives it from the audit log of this boot_id at start); "stock" is never claimed after it
        self.measure_wiring: Any = None  # ops_measure.MeasureWiring (G-111): cpufreq / MSR read / store / journal for vf.measure, curves.*, mem.read; set by main (None: those ops answer INTERNAL)

    # ---------------------------------------------------------------- output
    def emit(self, event: dict[str, Any]) -> bool:
        writer = self._writer
        return writer.send(event) if writer is not None else False

    def _send(self, message: dict[str, Any]) -> None:
        self.emit(message)

    def _state_event(self, op_id: str | None) -> None:
        self.emit({"event": "state", "op_id": op_id, "state": self.machine.state.value})

    # ---------------------------------------------------------------- shutdown
    def install_signal_handlers(self) -> None:
        """Main thread only (the launcher). SIGTERM / SIGHUP / SIGINT: orderly stop at a safe point."""

        def handler(signum: int, _frame: object) -> None:
            self._signal = signum

        for name in ("SIGTERM", "SIGHUP", "SIGINT"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), handler)

    def request_shutdown(self, reason: str) -> None:
        """Stop at the next safe point: the running op's cancel flag is set (a transaction in flight completes), the main loop ends."""
        self.cfg.log(f"shutting down: {reason}")
        cancel = self._cancel
        self._shutdown.set()
        if cancel is not None:
            cancel.set()

    # ---------------------------------------------------------------- the loop
    def run(self, stdin: BinaryIO, stdout: BinaryIO) -> int:
        """One stdio pair for the life of the process (tests, ``--sim``, the dev path)."""
        self._writer = Writer(stdout, on_broken=lambda: self.request_shutdown("stdout closed"))
        code = self.start(self._send)
        if code is not None:
            return code
        try:
            self._serve_loop(stdin)
        finally:
            self.stop()
        return 0

    def start(self, notify: Callable[[dict[str, Any]], object] | None = None) -> int | None:
        """Process-level start: the machine-wide instance lock, ``tuned_this_boot`` from the audit log, the stale measure journal. ``None`` = started;
        otherwise the exit code (the refusal was passed to ``notify`` and logged)."""
        lock = InstanceLock(self.cfg.lock_path, euid=self.cfg.euid_fn())
        try:
            lock.acquire()
        except TrustError as exc:  # a symlink, a foreign-owned or a writable-by-others lock file: never followed, never taken over
            if notify is not None:
                notify(error_response(None, REFUSED, f"untrusted lock file: {exc}", {"check": "LOCK_UNTRUSTED", "path": exc.path, "reason": exc.reason}))
            self.cfg.log(f"lock: {exc}")
            return EXIT_REFUSED_TO_START
        except (InstanceBusy, OSError) as exc:
            if notify is not None:
                notify(error_response(None, REFUSED, f"the helper is already running on this system: {exc}", {"check": "INSTANCE_LOCK"}))
            self.cfg.log(f"instance lock: {exc}")
            return EXIT_INSTANCE_BUSY
        self._lock = lock
        self._derive_tuned_this_boot()
        self._recover_measure_journal()  # a stale journal = a measurement that died with the previous helper: put cpufreq back before anything else runs
        return None

    def stop(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()

    @property
    def signalled(self) -> bool:
        """A SIGTERM / SIGHUP / SIGINT arrived (or a shutdown was requested for the whole process)."""
        return self._signal is not None

    def busy(self) -> bool:
        """An operation (long, or a read-only one on its own thread) is running or telemetry is on."""
        with self._slow_lock:
            slow = bool(self._slow)
        return self.machine.running is not None or slow or self._tele is not None

    def serve(self, rfile: BinaryIO, wfile: BinaryIO, *, peer_uid: int | None = None) -> None:
        """One connection of the socket service: the protocol over ``rfile`` / ``wfile`` until the client leaves (EOF, a broken socket) or a signal. At
        the end an operation in flight has completed its read-back / restore, telemetry is stopped and the helper is ready for the next client. The
        peer's uid is the invoker (audit ``operator``, the stress child's user) for exactly this connection."""
        self._writer = Writer(wfile, on_broken=lambda: self.request_shutdown("client socket closed"))
        self.peer_uid = peer_uid
        operator = self.cfg.operator
        if peer_uid is not None:
            self.cfg.operator = f"gui:uid{peer_uid}"
        try:
            self._serve_loop(rfile)
        finally:
            self.cfg.operator = operator
            self.peer_uid = None
            self._writer = None
            self._reset_connection()

    def _reset_connection(self) -> None:
        self._shutdown.clear()
        self._seen.clear()
        self._cancel = None
        self._worker = None
        self._inflight.clear()

    def _serve_loop(self, stdin: BinaryIO) -> None:
        frames: queue.Queue[Frame] = queue.Queue(maxsize=QUEUE_MAX)
        reader = threading.Thread(target=self._reader, args=(stdin, frames), name="helper-stdin", daemon=True)
        reader.start()
        beat = threading.Thread(target=self._heartbeat, name="helper-heartbeat", daemon=True)
        beat.start()
        try:
            while True:
                if self._signal is not None:
                    self.request_shutdown(f"signal {self._signal}")
                if self._shutdown.is_set():
                    break
                try:
                    frame = frames.get(timeout=_POLL_S)
                except queue.Empty:
                    continue
                if frame.kind == "eof":
                    self.request_shutdown("EOF on stdin")
                    break
                self._handle(frame)
        finally:
            self._finish(frames)
            beat.join(_SLOW_JOIN_S)  # _finish set the shutdown flag the heartbeat waits on; it must be gone before the next connection clears the flag

    def _derive_tuned_this_boot(self) -> None:
        """G-122: after the instance lock (so a restart sees what the previous helper's guard logged), before any frame: ``tuned_this_boot`` from the audit log."""
        from .ops_measure import audit_shows_tuning  # lazy: ops_measure imports ops

        try:
            self.tuned_this_boot = self.tuned_this_boot or audit_shows_tuning(self)
        except Exception:  # cannot prove "stock": the conservative answer
            self.tuned_this_boot = True

    def _recover_measure_journal(self) -> None:
        wiring = self.measure_wiring
        if wiring is None:
            return
        from .ops_measure import recover_journal  # lazy: ops_measure imports ops

        try:
            recover_journal(wiring, self.cfg.log)
        except Exception as exc:  # never keep the helper from starting; the failure is loud in the log and the journal stays for the next start
            self.cfg.log(f"measure journal recovery FAILED: {type(exc).__name__}: {exc}")

    def _finish(self, frames: queue.Queue[Frame]) -> None:
        self._shutdown.set()
        cancel = self._cancel
        if cancel is not None:
            cancel.set()
        worker = self._worker
        if worker is not None:
            worker.join()  # the in-flight transaction completes, the op answers once
        with self._slow_lock:
            slow = list(self._slow)
        for thread in slow:
            thread.join(_SLOW_JOIN_S)  # preflight / audit.tail are bounded reads; do not wait forever for a wedged filesystem
        self.telemetry_stop()
        while True:  # requests read but not started: each still gets its one answer
            try:
                frame = frames.get_nowait()
            except queue.Empty:
                break
            if frame.kind == "line":
                try:
                    req = parse_request(frame.data)
                    self._send(error_response(req.id, CANCELLED, "the helper is shutting down; the request was not started"))
                except ProtocolError as exc:
                    self._send(error_response(None, exc.code, exc.message))

    def _reader(self, stdin: BinaryIO, frames: queue.Queue[Frame]) -> None:
        try:
            while True:
                frame = read_frame(stdin)
                if frame.kind == "eof":
                    cancel = self._cancel  # stop a running op at its next safe point now, not when the dispatch loop gets to the EOF frame
                    if cancel is not None:
                        cancel.set()
                    while not self._shutdown.is_set():  # the EOF frame must get in: wait for room, never drop it
                        try:
                            frames.put(frame, timeout=_POLL_S)
                            break
                        except queue.Full:
                            continue
                    return
                try:
                    frames.put_nowait(frame)
                except queue.Full:
                    self._refuse_queue_full(frame)
        except Exception as exc:
            self.cfg.log(f"stdin reader failed: {type(exc).__name__}: {exc}")
            try:
                frames.put(Frame("eof"), timeout=1)
            except queue.Full:
                self.request_shutdown("stdin reader failed with a full queue")

    def _refuse_queue_full(self, frame: Frame) -> None:
        """The dispatch loop is not keeping up: refuse instead of buffering without bound. The request id is echoed only when it is not one in use."""
        req_id = None
        if frame.kind == "line":
            try:
                rid = parse_request(frame.data).id
                req_id = None if rid in self._seen else rid
            except ProtocolError:
                pass
        self.emit(error_response(req_id, BUSY, f"the request queue is full ({QUEUE_MAX}); the request was dropped, nothing was started",
                                 {"state": self.machine.state.value, "queue": "full"}))

    def _heartbeat(self) -> None:
        while not self._shutdown.wait(self.cfg.heartbeat_s):
            running = self.machine.running
            used, budget = self.budget.snapshot()  # incremental / cached: an fstat per beat, a read only for newly appended audit bytes
            self.emit({"event": "heartbeat", "op_id": running.op_id if running else None, "state": self.machine.state.value, "ops_used": used, "ops_budget": budget})

    # ---------------------------------------------------------------- dispatch
    def _handle(self, frame: Frame) -> None:
        if frame.kind == "oversize":
            self._send(error_response(None, BAD_REQUEST, f"line exceeds the {MAX_LINE}-byte cap"))
            return
        try:
            req = parse_request(frame.data)
        except ProtocolError as exc:
            reuse = exc.req_id is not None and exc.req_id in self._seen
            self._send(error_response(None if reuse else exc.req_id, exc.code, exc.message, exc.detail))
            return
        self._dispatch(req)

    def _remember(self, req_id: str) -> None:
        self._seen[req_id] = None
        while len(self._seen) > SEEN_MAX:
            for old in self._seen:
                if old not in self._inflight:
                    del self._seen[old]
                    break
            else:
                break

    def _dispatch(self, req: Request) -> None:
        if req.id in self._seen:
            if self._inflight.get(req.id) == (req.op, req.args):
                # SPEC 4.6: a reload / second tab re-attaches to the running operation, never repeats or cancels it. The op's events carry
                # its id and its one final response is still to come, so the repeat gets nothing further.
                self.cfg.log(f"request {req.id!r} repeated while running: re-attached, not started again")
                return
            self._send(error_response(None, BAD_REQUEST, "request id already used on this connection", {"id": req.id}))
            return
        self._remember(req.id)
        spec = self.ops.get(req.op)
        if spec is None:
            self._send(error_response(req.id, UNKNOWN_OP, f"unknown op {req.op!r}"))
            return
        threaded = not spec.long and (spec.slow or spec.write)
        try:
            args = spec.validate(req.args)
            if spec.long or spec.write:  # mailbox-class ops run only in 'idle' (SPEC 4.6); the read-only ones are allowed in every state
                running = self.machine.running
                if running is not None:
                    raise OpError(BUSY, f"the helper is {running.state.value} ({running.op} {running.op_id}); conflicting request refused",
                                  {"state": running.state.value, "op_id": running.op_id, "op": running.op})
            if spec.long:
                assert spec.state is not None
                cancel = threading.Event()
                self._cancel = cancel  # before begin(): a cancel arriving the moment the state flips must find this op's flag
                self.machine.begin(req.id, spec.name, spec.state)
            elif threaded:
                with self._slow_lock:
                    if len(self._slow) >= SLOW_MAX:
                        raise OpError(BUSY, f"{SLOW_MAX} read-only operations are already running; try again", {"state": self.machine.state.value, "slow_ops": SLOW_MAX})
            else:
                self._send(ok_response(req.id, spec.handler(self, OpContext(req.id, threading.Event(), self.emit), args)))
                return
        except OpError as exc:
            self._send(error_response(req.id, exc.code, exc.message, exc.detail))
            return
        except Busy as exc:  # a quick op (profiles.accept / profiles.fingerprint, G-141) took the machine between the check above and begin(): never start a second op
            self._cancel = None  # the flag made for this refused op belongs to nobody; a cancel must not report it as the running op's
            r = exc.running
            self._send(error_response(req.id, BUSY, str(exc), {"state": r.state.value, "op_id": r.op_id, "op": r.op}))
            return
        except Exception as exc:
            self._internal(req.id, exc)
            return
        self._inflight[req.id] = (req.op, req.args)
        if spec.long:
            self._state_event(req.id)
            worker = threading.Thread(target=self._run_long, args=(spec, req, args, cancel), name=f"helper-op-{req.op}", daemon=True)
            self._worker = worker
            try:
                worker.start()
            except Exception as exc:  # e.g. "can't start new thread": the machine must not stay in 'planning' with nothing running
                self._worker = None
                self._inflight.pop(req.id, None)
                self.machine.end(req.id)
                self._internal(req.id, exc)
                self._state_event(req.id)
        else:
            thread = threading.Thread(target=self._run_slow, args=(spec, req, args), name=f"helper-op-{req.op}", daemon=True)
            with self._slow_lock:
                self._slow.add(thread)
            try:
                thread.start()
            except Exception as exc:
                with self._slow_lock:
                    self._slow.discard(thread)
                self._inflight.pop(req.id, None)
                self._internal(req.id, exc)

    def _internal(self, req_id: str, exc: Exception) -> None:
        self.cfg.log("internal error:\n" + "".join(traceback.format_exception(exc)))
        self._send(error_response(req_id, INTERNAL, f"{type(exc).__name__}: {exc}"))

    def _answer(self, spec: OpSpec, req: Request, args: dict[str, Any], cancel: threading.Event) -> dict[str, Any]:
        """Run one op to its one response. The D-027 c gate (the helper's own fresh preflight) is part of the op, on the op's thread."""
        try:
            if spec.write:
                write_gate(self)
            return ok_response(req.id, spec.handler(self, OpContext(req.id, cancel, self.emit), args))
        except OpError as exc:
            return error_response(req.id, exc.code, exc.message, exc.detail)
        except Exception as exc:
            self.cfg.log("internal error:\n" + "".join(traceback.format_exception(exc)))
            return error_response(req.id, INTERNAL, f"{type(exc).__name__}: {exc}")

    def _run_long(self, spec: OpSpec, req: Request, args: dict[str, Any], cancel: threading.Event) -> None:
        response = self._answer(spec, req, args, cancel)
        # idle first, then the answer: a client that sends its next request on receiving this one must not meet BUSY
        self._inflight.pop(req.id, None)
        self.machine.end(req.id)
        self._send(response)
        self._state_event(req.id)

    def _run_slow(self, spec: OpSpec, req: Request, args: dict[str, Any]) -> None:
        """preflight / audit.tail / a quick write op: off the dispatch thread, so cancel, EOF and signals stay responsive while they read."""
        try:
            response = self._answer(spec, req, args, threading.Event())
        finally:
            self._inflight.pop(req.id, None)
            with self._slow_lock:
                self._slow.discard(threading.current_thread())
        self._send(response)

    # ---------------------------------------------------------------- ops that need the helper's own state
    def cancel_running(self, op_id: str | None) -> dict[str, Any]:
        running = self.machine.running
        cancel = self._cancel
        if running is None or cancel is None:
            return {"cancelled": False, "state": State.IDLE.value}
        if op_id is not None and op_id != running.op_id:
            raise OpError(BAD_REQUEST, f"{op_id!r} is not the running operation", {"state": running.state.value, "op_id": running.op_id})
        cancel.set()  # takes effect between mailbox transactions; the op then answers CANCELLED itself
        return {"cancelled": True, "op_id": running.op_id, "state": running.state.value}

    def telemetry_start(self, op_id: str, interval_s: float) -> dict[str, Any]:
        """Start the sampler. Running at the same interval: attach (``already_running``). Running at another one (contract 9): stop it and start again at the new
        interval (``restarted: true``, under the new ``op_id``, which its events carry)."""
        with self._tele_lock:
            old = self._tele
            if old is not None and old.interval_s == interval_s:
                return {"running": True, "already_running": True, "restarted": False, "op_id": old.op_id, "interval_s": old.interval_s}
            self._tele = None
        if old is not None:
            self._stop_tele(old)  # outside the lock: telemetry_stop takes it too
        with self._tele_lock:
            sampler = None
            try:
                sampler = self.cfg.sampler_factory(interval_s)
                sampler.start()
            except Exception as exc:
                if sampler is not None:  # release whatever the sampler holds (a port's descriptors) when its start failed
                    try:
                        sampler.stop()
                    except Exception as stop_exc:
                        self.cfg.log(f"telemetry sampler stop after a failed start: {type(stop_exc).__name__}: {stop_exc}")
                note = " (the previous sampler was stopped)" if old is not None else ""
                raise OpError(INTERNAL, f"telemetry could not start: {type(exc).__name__}: {exc}{note}") from None
            stop = threading.Event()
            thread = threading.Thread(target=self._telemetry_loop, args=(op_id, sampler, interval_s, stop), name="helper-telemetry", daemon=True)
            self._tele = _Telemetry(op_id, sampler, stop, thread, interval_s)
            thread.start()
            return {"running": True, "already_running": False, "restarted": old is not None, "op_id": op_id, "interval_s": interval_s}

    def _stop_tele(self, tele: _Telemetry) -> None:
        tele.stop.set()
        tele.thread.join(timeout=5)
        try:
            tele.sampler.stop()
        except Exception as exc:
            self.cfg.log(f"telemetry sampler stop failed: {type(exc).__name__}: {exc}")

    def telemetry_stop(self) -> dict[str, Any]:
        with self._tele_lock:
            tele, self._tele = self._tele, None
        if tele is None:
            return {"stopped": False}
        self._stop_tele(tele)
        return {"stopped": True, "op_id": tele.op_id}

    def _telemetry_loop(self, op_id: str, sampler: Any, interval_s: float, stop: threading.Event) -> None:
        last = None
        while not stop.is_set():
            try:
                snap = sampler.snapshot()
            except Exception as exc:
                self.cfg.log(f"telemetry snapshot failed: {type(exc).__name__}: {exc}")
                snap = {}
            ts = snap.get("ts") if isinstance(snap, dict) else None
            if ts is not None and ts != last:
                last = ts
                self.emit({"event": "telemetry", "op_id": op_id, "t": ts, "values": snap})
            stop.wait(max(0.02, interval_s / 4))
