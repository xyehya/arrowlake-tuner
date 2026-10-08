"""The helper process: contract 8 over stdin/stdout, SPEC 4.6 state machine, heartbeat, one machine-wide instance.

Threads: the caller's thread runs the dispatch loop; a reader thread turns stdin lines into frames (so a signal or a shutdown never waits on a
blocked read); a heartbeat thread; at most one worker thread for the single long op; one telemetry emitter. Every write to stdout goes through one
locked :class:`Writer`. Shutdown (EOF, SIGTERM, SIGHUP, SIGINT, a broken stdout) sets one flag that also sets the running op's cancel flag: the
current mailbox transaction finishes, no new one starts, the op sends its one final response, telemetry stops, the process exits.
"""
from __future__ import annotations

import os
import queue
import signal
import threading
import traceback
from dataclasses import dataclass
from typing import Any, BinaryIO

from .config import HelperConfig
from .ops import OpContext, OpSpec, builtin_ops, write_gate
from .protocol import (
    BAD_REQUEST,
    BUSY,
    CANCELLED,
    INTERNAL,
    MAX_LINE,
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

__all__ = ["EXIT_INSTANCE_BUSY", "Helper", "InstanceBusy", "InstanceLock"]

EXIT_INSTANCE_BUSY = 75  # EX_TEMPFAIL: another helper holds the machine-wide instance lock
_POLL_S = 0.1


class InstanceBusy(Exception):
    pass


class InstanceLock:
    """Exactly one helper per machine: an ``flock`` held for the life of the process on ``<lock_path>.helper``, a sibling of the guard's lock
    file (the guard creates and removes ``lock_path`` itself around every transaction, so it cannot double as a lifetime lock). Released by the
    kernel when the process dies; the file is never deleted (no unlink race)."""

    def __init__(self, lock_path: os.PathLike[str] | str) -> None:
        path = os.fspath(lock_path)
        self.path = path + ".helper"
        self._fd: int | None = None

    def acquire(self) -> None:
        try:
            import fcntl
        except ImportError as exc:  # not POSIX: the helper is Linux-only
            raise InstanceBusy("the instance lock needs fcntl (Linux)") from exc
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
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


class Helper:
    def __init__(self, cfg: HelperConfig) -> None:
        self.cfg = cfg
        self.machine = StateMachine()
        self.ops: dict[str, OpSpec] = {**builtin_ops(), **dict(cfg.extra_ops)}
        self._writer: Writer | None = None
        self._shutdown = threading.Event()
        self._signal: int | None = None  # set by the signal handler (a plain attribute: no lock may be taken in a handler)
        self._seen: set[str] = set()
        self._inflight: dict[str, tuple[str, dict[str, Any]]] = {}
        self._cancel: threading.Event | None = None
        self._worker: threading.Thread | None = None
        self._tele: _Telemetry | None = None
        self._tele_lock = threading.Lock()
        self.sim_time: Any = None  # (virtual clock, anchored wall clock) of the simulator, created by the first vf.dump

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
        self._writer = Writer(stdout, on_broken=lambda: self.request_shutdown("stdout closed"))
        lock = InstanceLock(self.cfg.lock_path)
        try:
            lock.acquire()
        except (InstanceBusy, OSError) as exc:
            self._send(error_response(None, BUSY, f"the helper is already running on this machine: {exc}", {"state": "locked"}))
            self.cfg.log(f"instance lock: {exc}")
            return EXIT_INSTANCE_BUSY
        frames: queue.Queue[Frame] = queue.Queue()
        threading.Thread(target=self._reader, args=(stdin, frames), name="helper-stdin", daemon=True).start()
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
            lock.release()
        return 0

    def _finish(self, frames: queue.Queue[Frame]) -> None:
        self._shutdown.set()
        cancel = self._cancel
        if cancel is not None:
            cancel.set()
        worker = self._worker
        if worker is not None:
            worker.join()  # the in-flight transaction completes, the op answers once
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
                    frames.put(frame)
                    return
                frames.put(frame)
        except Exception as exc:
            self.cfg.log(f"stdin reader failed: {type(exc).__name__}: {exc}")
            frames.put(Frame("eof"))

    def _heartbeat(self) -> None:
        while not self._shutdown.wait(self.cfg.heartbeat_s):
            running = self.machine.running
            self.emit({"event": "heartbeat", "op_id": running.op_id if running else None, "state": self.machine.state.value})

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

    def _dispatch(self, req: Request) -> None:
        if req.id in self._seen:
            if self._inflight.get(req.id) == (req.op, req.args):
                # SPEC 4.6: a reload / second tab re-attaches to the running operation, never repeats or cancels it. The op's events carry
                # its id and its one final response is still to come, so the repeat gets nothing further.
                self.cfg.log(f"request {req.id!r} repeated while running: re-attached, not started again")
                return
            self._send(error_response(None, BAD_REQUEST, "request id already used on this connection", {"id": req.id}))
            return
        self._seen.add(req.id)
        spec = self.ops.get(req.op)
        if spec is None:
            self._send(error_response(req.id, UNKNOWN_OP, f"unknown op {req.op!r}"))
            return
        try:
            args = spec.validate(req.args)
            if spec.long or spec.write:  # mailbox-class ops run only in 'idle' (SPEC 4.6); the read-only ones are allowed in every state
                running = self.machine.running
                if running is not None:
                    raise OpError(BUSY, f"the helper is {running.state.value} ({running.op} {running.op_id}); conflicting request refused",
                                  {"state": running.state.value, "op_id": running.op_id, "op": running.op})
                if spec.write:  # D-027 c: the helper's own fresh preflight, mode 'write' with no UNKNOWN check
                    write_gate(self)
            if not spec.long:
                ctx = OpContext(req.id, threading.Event(), self.emit)
                self._send(ok_response(req.id, spec.handler(self, ctx, args)))
                return
            assert spec.state is not None
            cancel = threading.Event()
            self._cancel = cancel  # before begin(): a cancel arriving the moment the state flips must find this op's flag
            self.machine.begin(req.id, spec.name, spec.state)
        except OpError as exc:
            self._send(error_response(req.id, exc.code, exc.message, exc.detail))
            return
        except Busy as exc:  # cannot happen (only this thread begins), but never start a second op
            r = exc.running
            self._send(error_response(req.id, BUSY, str(exc), {"state": r.state.value, "op_id": r.op_id, "op": r.op}))
            return
        except Exception as exc:
            self._internal(req.id, exc)
            return
        self._inflight[req.id] = (req.op, req.args)
        self._state_event(req.id)
        worker = threading.Thread(target=self._run_long, args=(spec, req, args, cancel), name=f"helper-op-{req.op}", daemon=True)
        self._worker = worker
        worker.start()

    def _internal(self, req_id: str, exc: Exception) -> None:
        self.cfg.log("internal error:\n" + "".join(traceback.format_exception(exc)))
        self._send(error_response(req_id, INTERNAL, f"{type(exc).__name__}: {exc}"))

    def _run_long(self, spec: OpSpec, req: Request, args: dict[str, Any], cancel: threading.Event) -> None:
        ctx = OpContext(req.id, cancel, self.emit)
        try:
            response = ok_response(req.id, spec.handler(self, ctx, args))
        except OpError as exc:
            response = error_response(req.id, exc.code, exc.message, exc.detail)
        except Exception as exc:
            self.cfg.log("internal error:\n" + "".join(traceback.format_exception(exc)))
            response = error_response(req.id, INTERNAL, f"{type(exc).__name__}: {exc}")
        # idle first, then the answer: a client that sends its next request on receiving this one must not meet BUSY
        self._inflight.pop(req.id, None)
        self.machine.end(req.id)
        self._send(response)
        self._state_event(req.id)

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
        with self._tele_lock:
            if self._tele is not None:
                return {"running": True, "already_running": True, "op_id": self._tele.op_id}
            try:
                sampler = self.cfg.sampler_factory(interval_s)
                sampler.start()
            except Exception as exc:
                raise OpError(INTERNAL, f"telemetry could not start: {type(exc).__name__}: {exc}") from None
            stop = threading.Event()
            thread = threading.Thread(target=self._telemetry_loop, args=(op_id, sampler, interval_s, stop), name="helper-telemetry", daemon=True)
            self._tele = _Telemetry(op_id, sampler, stop, thread)
            thread.start()
            return {"running": True, "already_running": False, "op_id": op_id, "interval_s": interval_s}

    def telemetry_stop(self) -> dict[str, Any]:
        with self._tele_lock:
            tele, self._tele = self._tele, None
        if tele is None:
            return {"stopped": False}
        tele.stop.set()
        tele.thread.join(timeout=5)
        try:
            tele.sampler.stop()
        except Exception as exc:
            self.cfg.log(f"telemetry sampler stop failed: {type(exc).__name__}: {exc}")
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
