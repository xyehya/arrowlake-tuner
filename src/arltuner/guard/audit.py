"""Append-only audit log for every guard decision (SAFETY.md "Implementation requirements": *logs every write*).

One JSON object per line, written with ``O_APPEND | O_CREAT`` (never ``O_TRUNC``), one ``write`` per record, then
``fsync`` (unless disabled for tests). Symlinks are refused (``O_NOFOLLOW`` where the OS has it, plus an explicit
``lstat`` check everywhere else). Lines end in ``\\n`` only (``O_BINARY`` on Windows: no CRLF translation).

Record fields (always ``ts`` ISO-8601 UTC, ``seq`` per file, ``event``; the rest when applicable):

* request: ``cpu`` (int), ``cmd`` / ``p1`` / ``p2`` (``"0x1C"``), ``req_data`` (``"0x0004A02D"``), ``purpose``; a plain-MSR request (T-023,
  ``guard.MsrRequest``) has ``msr`` (``"0x00000650"``), ``msr_kind`` (``"read"`` / ``"write"``) and a 64-bit ``req_data`` instead of cmd/p1/p2;
* ``operator`` (the human's name from the confirmation; the confirmation *token* is never logged);
* response: ``rc`` (int), ``rc_name`` (``arltuner.mailbox.rc_name``), ``data`` (``"0x........"``, the *response* word);
* ``reason`` / ``detail`` (refusals, errors, trips), ``baseline`` (hex word the predicate compared against);
* anything else the guard adds (``health_gate``, ``verified``, ``expected``, ``stage``) as plain JSON.

Events: ``refused | attempt | msr_read | result | readback | error | trip``. ``msr_read`` (T-023) is the intent record of a plain ``rdmsr``:
a plain read is audited but is not an ``attempt`` -- it is not a WRMSR, so it neither counts toward the per-boot cap nor starts a dwell.
A plain write is an ``attempt`` like a mailbox request.

T-016 (cross-process limits; SAFETY.md "Mailbox-session discipline", D-011): every ``attempt`` record also carries ``boot_id``
(string or ``null``), and :func:`read_state` summarises the log for the guard -- the latest ``attempt`` time over *all*
processes and the number of ``attempt`` records per boot -- so dwell and the per-boot cap survive process boundaries. The
read-back that follows a write is logged as ``readback``, never ``attempt``, so it is neither counted nor dwell-relevant.
"""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from arltuner.mailbox import rc_name

from .policy import MsrRequestLike, RequestLike

__all__ = ["EVENTS", "AuditLog", "AuditState", "AuditUnreadable", "parse_audit_ts", "read_all", "read_state"]

EVENTS = frozenset({"refused", "attempt", "msr_read", "result", "readback", "error", "trip"})

_RESERVED = frozenset(
    {"ts", "seq", "event", "cpu", "cmd", "p1", "p2", "msr", "msr_kind", "req_data", "purpose", "operator", "rc", "rc_name", "data", "reason", "detail", "baseline", "value"}
)
_O_BINARY = getattr(os, "O_BINARY", 0)  # Windows: no text-mode newline translation
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)  # POSIX: refuse a symlink as the final path component
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)  # opening a FIFO must never block (a planted FIFO is refused by the fstat below, not waited on)
_MAX_TEXT = 500


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _hex(value: object, width: int) -> str:
    """Uppercase hex for non-negative ints; a truncated ``repr`` for anything malformed (refused requests may be garbage)."""
    if _is_int(value) and value >= 0:  # type: ignore[operator]
        return f"0x{value:0{width}X}"
    return repr(value)[:80]


def _text(value: object) -> str:
    return str(value)[:_MAX_TEXT]


def _iso(ts: float | None) -> str:
    stamp = time.time() if ts is None else ts
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(timespec="milliseconds")


def _refuse_symlink(path: Path) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise OSError(f"audit log {path} is a symlink; refusing to follow it")


def _require_plain_file(fd: int, path: Path, what: str = "audit log") -> None:
    """``fstat`` the descriptor we actually opened: a regular file with exactly one link, else close it and raise ``OSError`` (never read or
    write a FIFO, device or hard-linked alias; checking the name first and opening afterwards would be a TOCTOU)."""
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{what} {path} is not a regular file; refusing")
        if st.st_nlink != 1:
            raise OSError(f"{what} {path} has {st.st_nlink} hard links; refusing")
    except BaseException:
        os.close(fd)
        raise


def _open_read(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | _O_BINARY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC)
    _require_plain_file(fd, path)
    return fd


def _tighten(fd: int) -> None:
    """G-141: a log created group/world-readable by an older version or by the umask becomes 0600 (``fchmod``; not available on Windows before Python 3.13, and a log
    that is not ours cannot be changed: both are left alone, never an error of the audit write)."""
    fchmod = getattr(os, "fchmod", None)
    if fchmod is None:
        return
    try:
        if os.fstat(fd).st_mode & 0o077:
            fchmod(fd, 0o600)
    except OSError:
        pass


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write to the audit log")
        view = view[written:]


class AuditLog:
    """Append-only JSON-lines log. ``append`` raises ``OSError`` if the record cannot be written -- callers decide what
    that means (the guard refuses to send anything it cannot record first)."""

    def __init__(self, path: Path | str, *, fsync: bool = True) -> None:
        self._path = Path(path)
        self._fsync = bool(fsync)
        self._mutex = threading.Lock()
        _refuse_symlink(self._path)
        fd = self._open()  # creates the file now so a bad path fails at construction, not at the first write
        os.close(fd)
        with os.fdopen(_open_read(self._path), "rb") as handle:
            existing = handle.read()
        self._seq = existing.count(b"\n")
        # A previous crash may have left a last line without its newline: start on a fresh line so it stays isolated.
        self._pending_newline = bool(existing) and not existing.endswith(b"\n")

    @property
    def path(self) -> Path:
        return self._path

    def _open(self) -> int:
        _refuse_symlink(self._path)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _O_BINARY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC
        fd = os.open(self._path, flags, 0o600)  # G-141: the log is private (install.sh creates it 0600; it names CPUs, words and purposes)
        _require_plain_file(fd, self._path)
        _tighten(fd)
        return fd

    def append(
        self,
        event: str,
        *,
        ts: float | None = None,
        request: RequestLike | MsrRequestLike | None = None,
        operator: str | None = None,
        response: Any = None,
        reason: str | None = None,
        detail: str | None = None,
        baseline: int | None = None,
        value: int | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Write one record and return it. ``ts`` is epoch seconds (wall clock); ``None`` means now."""
        if event not in EVENTS:
            raise ValueError(f"unknown audit event {event!r}")
        clash = _RESERVED.intersection(extra)
        if clash:
            raise ValueError(f"extra audit fields clash with reserved names: {sorted(clash)}")
        with self._mutex:
            rec: dict[str, Any] = {"ts": _iso(ts), "seq": self._seq, "event": event}
            if request is not None:
                cpu = getattr(request, "cpu", None)
                rec["cpu"] = cpu if _is_int(cpu) else repr(cpu)[:80]
                if hasattr(request, "msr"):  # T-023: a plain-MSR request (guard.MsrRequest)
                    rec["msr"] = _hex(getattr(request, "msr", None), 8)
                    rec["msr_kind"] = _text(getattr(request, "kind", None))
                    rec["req_data"] = _hex(getattr(request, "value", None), 16)
                else:
                    rec["cmd"] = _hex(getattr(request, "cmd", None), 2)
                    rec["p1"] = _hex(getattr(request, "p1", None), 2)
                    rec["p2"] = _hex(getattr(request, "p2", None), 2)
                    rec["req_data"] = _hex(getattr(request, "data", None), 8)
                rec["purpose"] = _text(getattr(request, "purpose", None))
            if operator is not None:
                rec["operator"] = _text(operator)
            if response is not None:
                code = getattr(response, "rc", None)
                rec["rc"] = code if _is_int(code) else _hex(code, 2)
                try:
                    rec["rc_name"] = rc_name(code)  # type: ignore[arg-type]
                except ValueError:
                    rec["rc_name"] = "INVALID"
                rec["data"] = _hex(getattr(response, "data", None), 8)
            if value is not None:
                rec["value"] = _hex(value, 16)
            if reason is not None:
                rec["reason"] = _text(reason)
            if detail is not None:
                rec["detail"] = _text(detail)
            if baseline is not None:
                rec["baseline"] = _hex(baseline, 16 if hasattr(request, "msr") else 8)
            rec.update(extra)
            line = json.dumps(rec, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii") + b"\n"
            if self._pending_newline:
                line = b"\n" + line
            fd = self._open()
            try:
                _write_all(fd, line)
                self._seq += 1
                self._pending_newline = False
                if self._fsync:
                    os.fsync(fd)
            finally:
                os.close(fd)
            return rec


def read_all(path: Path | str) -> list[dict[str, Any]]:
    """Parse a whole audit file (for tests and for the human). Raises ``ValueError`` on a line that is not JSON."""
    records: list[dict[str, Any]] = []
    with open(path, "rb") as handle:
        for number, raw in enumerate(handle, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"{path}:{number}: not a valid audit record: {exc}") from exc
            if not isinstance(record, Mapping):
                raise ValueError(f"{path}:{number}: audit record is not an object")
            records.append(dict(record))
    return records


class AuditUnreadable(Exception):
    """The audit log cannot be interpreted (a malformed line that is not a trailing partial one, a bad timestamp, ...).
    The guard treats this as ``AUDIT_UNAVAILABLE`` and sends nothing: limits it cannot evaluate are limits it cannot honour."""


@dataclass(frozen=True)
class AuditState:
    """What the audit log says about earlier mailbox traffic (all processes, all boots).

    ``last_attempt_wall``: epoch seconds of the latest ``attempt`` record (``None`` = no history).
    ``attempts_by_boot``: number of ``attempt`` records per ``boot_id``; ``None`` is its own bucket (boot id unknown, or a
    record written before T-016 that has no ``boot_id``).
    """

    last_attempt_wall: float | None
    attempts_by_boot: Mapping[str | None, int]


def _parse_ts(value: object, where: str) -> float:
    if not isinstance(value, str):
        raise AuditUnreadable(f"{where}: attempt record has no ISO-8601 'ts'")
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            raise ValueError("timestamp has no UTC offset")
        return stamp.timestamp()
    except (ValueError, OverflowError, OSError) as exc:
        raise AuditUnreadable(f"{where}: bad attempt timestamp {value[:40]!r}: {exc}") from exc


parse_audit_ts = _parse_ts  # public name for other readers of the audit log, so they apply the guard's exact timestamp rule


def read_state(path: Path | str) -> AuditState:
    """Scan the whole audit file and summarise it for the guard's cross-process limits.

    A missing or empty file is "no history". A non-empty line that is not a JSON object with a string ``event`` raises
    :class:`AuditUnreadable`, *except* a final line with no terminating newline that fails to parse (a write cut short by a
    crash or caught mid-append by another process): that partial line is ignored. ``attempt`` records must carry a valid
    timezone-aware ISO-8601 ``ts`` and a ``boot_id`` that is a string or null (absent = null). OS errors propagate as
    ``OSError`` (the guard treats them like :class:`AuditUnreadable`). A symlink is refused, as by :class:`AuditLog`.
    """
    target = Path(path)
    _refuse_symlink(target)
    last: float | None = None
    by_boot: dict[str | None, int] = {}
    try:
        handle = os.fdopen(_open_read(target), "rb")
    except FileNotFoundError:
        return AuditState(None, MappingProxyType({}))
    with handle:
        for number, raw in enumerate(handle, 1):
            partial = not raw.endswith(b"\n")  # only ever true for the last line of the file
            text = raw.strip()
            if not text:
                continue
            where = f"{target}:{number}"
            try:
                record = json.loads(text.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if partial:
                    continue  # truncated trailing line: tolerated
                raise AuditUnreadable(f"{where}: not a valid audit record: {exc}") from exc
            if not isinstance(record, Mapping) or not isinstance(record.get("event"), str):
                raise AuditUnreadable(f"{where}: audit record is not an object with an 'event'")
            if record["event"] != "attempt":
                continue
            stamp = _parse_ts(record.get("ts"), where)
            boot = record.get("boot_id")
            if boot is not None and not isinstance(boot, str):
                raise AuditUnreadable(f"{where}: attempt 'boot_id' is neither a string nor null")
            last = stamp if last is None else max(last, stamp)
            by_boot[boot] = by_boot.get(boot, 0) + 1
    return AuditState(last, MappingProxyType(by_boot))
