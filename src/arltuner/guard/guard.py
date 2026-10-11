"""``WriteGuard``: the only path that may call ``MailboxTransport.transact`` (ARCHITECTURE.md Contract 5).

Every OC-mailbox request -- reads included, because a mailbox "read" is a WRMSR to MSR 0x150 (registers.md "OC mailbox --
request model"; SAFETY.md "OC mailbox rule") -- goes through :meth:`WriteGuard.execute`. The guard fails closed: a request
that is not explicitly enabled, confirmed, allowlisted, baselined, bounded, rate-limited, serialised and auditable is
refused and **nothing is sent**. It never sleeps and never retries (SAFETY.md "Nothing in the app is allowed to
loop-retry a failing write"; "One transaction at a time").

The guard depends only on the structural :class:`Transport` protocol, not on ``arltuner.msr``. It performs no I/O of its
own besides the audit log and the lock file.

Order of checks in :meth:`WriteGuard.execute` (first failure raises :class:`GuardRefused` before anything is sent, and is
audited): TRIPPED, NO_ENV_FLAG, BAD_CONFIRMATION, CPU_NOT_ALLOWED, NOT_ALLOWLISTED, BASELINE_MISSING, CHECK_FAILED,
SESSION_LIMIT, [audit log read: AUDIT_UNAVAILABLE], BOOT_LIMIT, DWELL (in-process, then cross-process), LOCKED,
HEALTH_GATE. Then: audit ``attempt`` -> transact -> audit ``result`` -> (write with rc 0)
read back through the same transport and compare the whole word -> ``health_gate("after")`` -> :class:`Result`.

Trip (terminal for this guard instance): any exception from the transport (``MailboxTimeout``, ``MsrError``, anything),
a read-back mismatch or unreadable read-back, a failing after-gate, or a lock that cannot be released. Once tripped,
every call is refused ``TRIPPED``; a new guard instance (a new session, after the human looked) is the only reset.

Cross-process limits (T-016; SAFETY.md "Mailbox-session discipline", D-011). The CLI is one process per command, so the
in-process session limit and dwell cannot stop back-to-back invocations -- the sweep-like mailbox traffic behind the
2026-08-26 incident. The guard therefore also reads the append-only audit log (``audit.read_state``) and refuses:

* ``DWELL`` if the latest ``attempt`` record *of any process* is younger than ``policy.min_dwell_s`` by the wall clock (or is
  more than 1 s in the future: the clock went backwards, so the elapsed time cannot be trusted);
* ``BOOT_LIMIT`` if the ``attempt`` records carrying the current ``boot_id`` already number ``policy.max_operations_per_boot``
  (an unknown boot id -- ``None`` -- is its own bucket and is capped the same way);
* ``AUDIT_UNAVAILABLE`` if the log cannot be read or interpreted: limits that cannot be evaluated are not waived.

Both audit-based checks are repeated once the lock file is held, so two processes that start together cannot both pass them.
The read-back after a write is part of the same operation: it is logged as ``readback``, never ``attempt``, so it is exempt.
The guard still never sleeps; it refuses.

Plain MSRs (T-023, D-017): a plain ``rdmsr`` / ``wrmsr`` of a turbo ratio table is an :class:`MsrRequest` and goes through the **same**
chain (:meth:`WriteGuard._execute_msr`): TRIPPED, NO_ENV_FLAG, BAD_CONFIRMATION, [no MSR port], CPU_NOT_ALLOWED, NOT_ALLOWLISTED
(``Policy.msr_allow``; a CPU the entry does not cover is CPU_NOT_ALLOWED), BASELINE_MISSING (a write needs an ``rdmsr`` of the same MSR on the
same CPU earlier in this session), CHECK_FAILED (``turbo_ratio_bounded`` for a write), SESSION_LIMIT, BOOT_LIMIT, DWELL, LOCKED, HEALTH_GATE.
A plain **read** is audited (event ``msr_read``, never ``attempt``) but is not a WRMSR: it passes the same limit checks yet does not count toward
the session / per-boot caps and does not start a dwell. A plain **write** counts like a mailbox request. Read-back of a write: ``rdmsr`` on the
same CPU must equal the value written, then ``rdmsr`` on ``verify_cpu`` (a CPU of the other core kind: the scope of ``0x650`` / ``0x1AD`` is
``[UNVERIFIED]``, so a value that does not show up there means the MSR is not package-wide) -- a mismatch trips. The guard gets the port through
``WriteGuard(msr_port=...)``; without it every :class:`MsrRequest` is refused (fail closed).

Session facts (T-021, ``[MEASURED win 2026-10-01]`` XTU's per-core Apply: ``0x11`` -> ``0x1D`` -> ``0x26`` read -> ``0x27``): the named
checks also see what this session has read for the request's CPU (``0x1C`` favored word, ``0x09 P1=0x11`` fused point, and -- D-016 --
``0x28`` hardware maximum voltage) and the ``0x11`` word it executed there (``policy.CheckContext``), together with the policy's bounds
for the CPU's core kind (ratio / V target ceilings, offset window, step limits: safety by bounds, not by direction). Read twins:
``0x11->0x10``, ``0x15->0x14``, ``0x1D->0x1C``, ``0x27->0x26``; the read-back of ``0x1D`` compares only ``[7:0]`` (``0x1C`` also carries
the fused ratio in ``[15:8]``), every other write compares the whole word. The read-backs are extra WRMSRs: they are exempt from this
guard's session / per-boot counters (``audit.py`` logs them as ``readback``, never ``attempt``) and counted by the planner's pre-flight
budget instead (T-022, ``controls.planner.count_operations``). ``revert_plan`` is XTU-shaped: see :meth:`WriteGuard.revert_plan`.

T-025 (per-V/F-point offsets): ``0x11`` with ``P1=0`` and ``P2`` 1..15 is judged by its own check ``vf_point_offset_bounded`` against the ``0x10`` read of the
same CPU **and the same P2** (baselines are keyed by cpu/p1/p2); read-back is the whole word as for ``P2=0``; its revert is the baseline point word
exactly (never filled from ``0x09``). The facts the other checks collect (``0x1C``, ``0x09``, ``0x28`` and the last ``0x11`` sent) concern the OC point
(``P2=0``) only; a point write never feeds them and no ``0x1D`` / ``0x27`` follows it.
"""
from __future__ import annotations

import hmac
import math
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import NoReturn, Protocol, overload

from arltuner.mailbox import Cmd, Response, VfWord, is_write

from .audit import AuditLog, AuditState, AuditUnreadable, read_state
from .policy import (
    CHECKS,
    MSR_CHECKS,
    AllowEntry,
    CheckContext,
    MsrAllowEntry,
    Policy,
    build_context,
    build_msr_context,
    top_fused_ratio,
    turbo_bytes,
)

__all__ = [
    "ENV_FLAG",
    "Confirmation",
    "GuardRefused",
    "MsrPortLike",
    "MsrRequest",
    "MsrResult",
    "Refusal",
    "Request",
    "Result",
    "Transport",
    "WriteGuard",
    "anchored_wall_clock",
    "read_boot_id",
]

ENV_FLAG = "ARLTUNER_ALLOW_MAILBOX"  # SAFETY.md "Implementation requirements": explicit env flag / CLI confirmation

#: write command -> its read twin (registers.md "Mailbox commands": 0x10/0x11, 0x14/0x15, 0x1C/0x1D, 0x26/0x27 are the rd/wr pairs).
#: Only writes with a twin can be baselined and read back; any other write is refused BASELINE_MISSING even if a policy lists it.
_READ_TWIN: Mapping[int, int] = MappingProxyType(
    {
        int(Cmd.VOLTAGE_FREQ_WRITE): int(Cmd.VOLTAGE_FREQ_READ),
        int(Cmd.MISC_GLOBAL_CONFIG_WRITE): int(Cmd.MISC_GLOBAL_CONFIG_READ),
        int(Cmd.FAVORED_CORE_WRITE): int(Cmd.FAVORED_CORE_READ),
        int(Cmd.GRANULAR_RATIO_WRITE): int(Cmd.GRANULAR_RATIO_READ),
    }
)


def _same_word(written: int, read: int) -> bool:
    return read == written


def _same_low_byte(written: int, read: int) -> bool:
    """0x1D: the favored-core word is the ratio (``[7:0]``); 0x1C also returns the fused ratio in ``[15:8]`` ([UNVERIFIED]: what 0x1C
    returns after a 0x1D write was never read on hardware -- a different answer trips the guard)."""
    return (read & 0xFF) == written


#: write command -> does the read-back (twin read) prove the write? Default: the whole word equals what was written.
_READBACK_MATCH: Mapping[int, Callable[[int, int], bool]] = MappingProxyType(
    {
        int(Cmd.VOLTAGE_FREQ_WRITE): _same_word,
        int(Cmd.MISC_GLOBAL_CONFIG_WRITE): _same_word,
        int(Cmd.FAVORED_CORE_WRITE): _same_low_byte,
        int(Cmd.GRANULAR_RATIO_WRITE): _same_word,
    }
)


def _revert_vf_word(baseline: int, favored: int | None, fused: int | None, kind: str = "P", cap_ratio: bool = True) -> int:
    """The XTU-shaped word that puts a core's *offset* (and the other fields) back: the baseline word, with a zero ratio / V target
    filled the way XTU fills it (``0x1C[7:0]`` / the fused ``0x09`` voltage) -- **G-122:** with ``cap_ratio`` the filled ratio is capped at the top fused ratio of
    the core ``kind`` (P 57, E 46), so reverting a word that had no OC point never creates one (the caller passes ``False`` when the word being undone had an OC
    ratio: its undo keeps the XTU fill, which that ratio's step limits were measured from) -- and then **the word the hardware keeps** (D-035, ``[MEASURED lnx 2026-10-11]``):
    with no OC point (ratio <= the top fused ratio) the V target is 0 whatever the offset (G-124: the hardware drops a written one, ``0xFEC5B439`` read back
    ``0xFEC00039``, and at offset 0 it kept ``0x00000039`` for ``0x0005B439`` when only the 0x11 was sent). (Same rule as ``controls.planner.kept_word`` / ``has_oc_point``, which this module cannot import.) So a baseline
    ``0xFEC00039`` is restored exactly. **A baseline word with a zero field (``0x0``, 23 of 24 cores at boot) cannot itself be written back** -- XTU never
    sends one; a reboot restores it."""
    base = VfWord.from_u32(baseline)
    top = top_fused_ratio(kind)
    fav = (favored or 0) & 0xFF
    ratio = base.ratio or (min(fav, top) if fav and cap_ratio else fav)
    v_target = base.v_target_raw or (((fused or 0) >> 8) & 0xFFF)
    if 0 < ratio <= top:  # G-124: no OC point = V target 0, whatever the offset or where the target came from
        v_target = 0
    return base.with_(ratio=ratio, v_target_raw=v_target).to_u32()


_U32_MAX = 0xFFFFFFFF
_OPERATOR_LOG_MAX = 200
_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"  # one UUID per boot; Linux only
_BOOT_ID_RE = re.compile(r"[0-9a-fA-F-]{32,36}")
_FUTURE_SLACK_S = 1.0  # an audit timestamp this far ahead of the wall clock is tolerated; more means the clock went backwards


def read_boot_id() -> str | None:
    """The kernel's boot id (``/proc/sys/kernel/random/boot_id``), or ``None`` if unreadable, malformed or not Linux.
    ``None`` is not "no limit": the guard counts unknown-boot operations in their own capped bucket."""
    try:
        with open(_BOOT_ID_PATH, "rb") as handle:
            text = handle.read(128).decode("ascii", "replace").strip()
    except OSError:
        return None
    return text.lower() if _BOOT_ID_RE.fullmatch(text) else None


def anchored_wall_clock(clock: Callable[[], float], wall: Callable[[], float] = time.time) -> Callable[[], float]:
    """A wall clock that moves exactly as ``clock`` does, starting from ``wall()`` now. For simulations and tests where the
    dwell waits run on a virtual/fake monotonic clock: real wall time would not advance, so the audit-based dwell would
    refuse a run that legitimately waited. Production passes the real ``time.time`` and never uses this."""
    base_wall, base = wall(), clock()
    return lambda: base_wall + (clock() - base)


def _when(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds")
    except (ValueError, OverflowError, OSError):
        return repr(ts)


class Transport(Protocol):
    """Structural type of ``arltuner.msr.MailboxTransport`` (Contract 4). The guard never imports that module."""

    def transact(self, cpu: int, cmd: int, p1: int, p2: int, data: int = 0) -> Response: ...


@dataclass(frozen=True)
class Request:
    """One mailbox request. ``purpose`` is free text for the audit log and must be non-blank."""

    cpu: int
    cmd: int
    p1: int
    p2: int
    data: int
    purpose: str


class MsrPortLike(Protocol):
    """Structural type of ``arltuner.msr.MsrPort`` (Contract 4) for the plain-MSR path. The guard never imports that module."""

    def rdmsr(self, cpu: int, msr: int) -> int: ...

    def wrmsr(self, cpu: int, msr: int, value: int) -> None: ...


@dataclass(frozen=True)
class MsrRequest:
    """One plain-MSR request (T-023): ``kind`` ``"read"`` (``value`` must be 0) or ``"write"`` of ``value`` (u64). ``verify_cpu`` (writes only)
    is the CPU of the other core kind the guard reads back on after the same-CPU read-back. ``purpose`` is free text, non-blank."""

    cpu: int
    msr: int
    value: int
    purpose: str
    kind: str = "read"
    verify_cpu: int | None = None


@dataclass(frozen=True)
class MsrResult:
    """Outcome of an executed :class:`MsrRequest`. Read: ``value`` is the word read, ``verify_value`` / ``verified`` are ``None``. Write:
    ``value`` is the read-back on ``request.cpu``, ``verify_value`` the read-back on ``request.verify_cpu``, ``verified`` True only when both
    equal the value written (else the guard has tripped)."""

    request: MsrRequest
    value: int
    verify_value: int | None
    verified: bool | None


@dataclass(frozen=True)
class Confirmation:
    """The human's per-session confirmation: the session token they were shown/typed and their name. The token is
    excluded from ``repr`` so it does not leak into logs or tracebacks."""

    token: str = field(repr=False)
    operator: str


@dataclass(frozen=True)
class Result:
    """Outcome of an executed request. ``response.rc != 0`` is returned, not raised. ``readback``/``verified`` are set only
    for a write that returned rc 0 (``verified`` True = the full word read back equals the word written); ``verified is
    None`` means *no read twin was consulted* (a read, or a write that did not succeed) -- it is not a pass."""

    request: Request
    response: Response
    readback: Response | None
    verified: bool | None


class Refusal(str, Enum):
    NO_ENV_FLAG = "NO_ENV_FLAG"
    BAD_CONFIRMATION = "BAD_CONFIRMATION"
    CPU_NOT_ALLOWED = "CPU_NOT_ALLOWED"
    NOT_ALLOWLISTED = "NOT_ALLOWLISTED"
    CHECK_FAILED = "CHECK_FAILED"
    BASELINE_MISSING = "BASELINE_MISSING"
    SESSION_LIMIT = "SESSION_LIMIT"
    AUDIT_UNAVAILABLE = "AUDIT_UNAVAILABLE"
    BOOT_LIMIT = "BOOT_LIMIT"
    DWELL = "DWELL"
    LOCKED = "LOCKED"
    HEALTH_GATE = "HEALTH_GATE"
    TRIPPED = "TRIPPED"


class GuardRefused(Exception):
    """Raised instead of sending. ``reason`` says which check failed; ``detail`` is human-readable. ``retry_after_s`` is set
    for a ``DWELL`` refusal: the seconds until the dwell window closes (the guard does not wait; the caller may)."""

    def __init__(self, reason: Refusal, detail: str, retry_after_s: float | None = None) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class _Executed:
    """A write that was sent (rc 0, or outcome unknown after a transport exception) and the word it would be undone with
    (``baseline``: the twin read of this session; ``favored`` / ``fused``: the 0x1C / 0x09 words known then, to fill a zero field)."""

    request: Request
    baseline: int
    favored: int | None = None
    fused: int | None = None
    stamp: int = 0  # identifies the read of the twin this baseline came from: a re-read starts a new chain of writes
    prior: int | None = None  # 0x11 only: the data of the previous 0x11 to this CPU on the same baseline read (the state this write left)


@dataclass(frozen=True)
class _MsrExecuted:
    """A plain write that was sent (success, or outcome unknown after a port exception) and the word it is undone with (the ``rdmsr``
    baseline of this session)."""

    request: MsrRequest
    baseline: int


def _require_u64(value: object, what: str) -> int:
    """Fail closed on a port that answers with anything but an int in 0..2**64-1."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"port returned {value!r} for {what} (expected an int 0..2**64-1)")
    return value


def _require_response(value: object) -> Response:
    """Fail closed on a transport that answers with anything but a well-formed ``Response`` (rc u8, data u32)."""
    if not isinstance(value, Response):
        raise TypeError(f"transport returned {type(value).__name__}, not a Response")
    for name, top in (("rc", 0xFF), ("data", _U32_MAX)):
        item = getattr(value, name)
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= top:
            raise ValueError(f"transport returned a Response with {name}={item!r} (expected int 0..{top:#x})")
    return value


def _operator_of(confirm: object) -> str | None:
    operator = getattr(confirm, "operator", None)
    return operator[:_OPERATOR_LOG_MAX] if isinstance(operator, str) else None


class WriteGuard:
    """The single gate for all mailbox traffic. One instance = one session (counters, baselines, trip state live here).
    The audit log (``audit.path``) is the only state shared with other processes: cross-process dwell and the per-boot cap
    are read from it on every call (module docstring, "Cross-process limits")."""

    def __init__(
        self,
        transport: Transport,
        policy: Policy,
        audit: AuditLog,
        *,
        session_token: str,
        env: Mapping[str, str] | None = None,
        health_gate: Callable[[str], tuple[bool, str]] | None = None,
        lock_path: Path | str,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] | None = None,
        boot_id_fn: Callable[[], str | None] = read_boot_id,
        msr_port: MsrPortLike | None = None,
    ) -> None:
        if not callable(getattr(transport, "transact", None)):
            raise TypeError("transport must have a transact(cpu, cmd, p1, p2, data) method")
        if not isinstance(policy, Policy):
            raise TypeError("policy must be a arltuner.guard.Policy")
        if not callable(getattr(audit, "append", None)):
            raise TypeError("audit must be a arltuner.guard.AuditLog")
        if not isinstance(session_token, str) or not session_token:
            raise ValueError("session_token must be a non-empty string (an empty token would confirm anything)")
        if msr_port is not None and not (callable(getattr(msr_port, "rdmsr", None)) and callable(getattr(msr_port, "wrmsr", None))):
            raise TypeError("msr_port must have rdmsr(cpu, msr) and wrmsr(cpu, msr, value) methods")
        self._msr_port = msr_port  # None: every MsrRequest is refused (plain MSR access is off)
        self._transport = transport
        self._policy = policy
        self._audit = audit
        self._token = session_token.encode("utf-8", "surrogatepass")
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._health_gate = health_gate
        self._lock_path = Path(lock_path)
        self._clock = clock
        # ``wall_clock=None``: real time.time with the default monotonic clock; with an injected ``clock`` (simulation/tests)
        # the wall clock moves with it, so dwell waits done on that clock are also seen by the audit-based dwell.
        if wall_clock is None:
            wall_clock = time.time if clock is time.monotonic else anchored_wall_clock(clock)
        self._wall_clock = wall_clock
        if not callable(boot_id_fn):
            raise TypeError("boot_id_fn must be callable")
        self._boot_id_fn = boot_id_fn
        self._busy = threading.Lock()  # in-process serialisation; never waits (contention is refused LOCKED)
        self._tripped = False
        self._trip_reason = ""
        self._ops = 0  # executed operations this session (read-backs excluded)
        self._last_op_at: float | None = None
        self._baselines: dict[tuple[int, int, int, int], int] = {}  # (read cmd, cpu, p1, p2) -> word from a successful read
        self._stamps: dict[tuple[int, int, int, int], int] = {}  # the same keys -> sequence number of that read (for the revert chain)
        self._reads_done = 0
        self._journal: list[_Executed] = []
        self._msr_baselines: dict[tuple[int, int], int] = {}  # (cpu, msr) -> value of a successful plain rdmsr (T-023)
        self._msr_journal: list[_MsrExecuted] = []

    # ---------------------------------------------------------------- public
    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def trip_reason(self) -> str:
        """Why the guard tripped (empty while it has not)."""
        return self._trip_reason

    def revert_plan(self) -> list[Request | MsrRequest]:
        """Inverse requests (``purpose="revert"``) for every write sent this session (SAFETY.md "Revert path"). Includes a write
        whose outcome is unknown because the transport raised. The guard never executes them; sending one is a new ``execute``
        call subject to every check (including the session limit and dwell).

        XTU-shaped (``[MEASURED win 2026-10-01]``: XTU's revert is ``0x11`` with the baseline offset, ``0x1D`` = its ratio, ``0x27``):
        a ``0x11`` is undone with the word that was in effect before it -- the baseline word with a zero ratio / V target filled from
        ``0x1C[7:0]`` / the fused ``0x09`` voltage (a baseline of ``0x0`` cannot be written back -- only a reboot restores it), or the
        previous ``0x11`` of the same chain; a ``0x1D`` with the ratio of the last ``0x11`` undo; a ``0x27`` and a ``0x15`` with the word
        read before them (``percore_enable_only`` refuses the ``0x15`` inverse, as before).

        D-016 order: per CPU, newest-written CPU first; that CPU's ``0x11`` undos **newest first** -- so a voltage-first raise (V target
        word, then full word) is undone **frequency first** (ratio back, then V target and offset, each word passing the ordering rule of
        ``vf_bounded``) -- then its ``0x1D`` / ``0x27`` undos in the order they were sent (the ``0x1D`` check needs its ``0x11`` first); the
        package-wide ``0x15`` inverses last, newest first. A reboot restores the BIOS-programmed state whatever this plan says.

        T-023: a plain write of a turbo table is undone with the ``rdmsr`` baseline of this session (:class:`MsrRequest`, ``purpose="revert"``,
        newest first). Frequency-safe order: an undo that **lowers** a bucket (the write had raised it) comes before every mailbox undo
        (frequency back first, then the voltage), an undo that raises one (the write had lowered it) comes after them (voltage up first)."""
        per_cpu = [e for e in self._journal if int(e.request.cmd) != int(Cmd.MISC_GLOBAL_CONFIG_WRITE)]
        package = [e for e in self._journal if int(e.request.cmd) == int(Cmd.MISC_GLOBAL_CONFIG_WRITE)]
        cpus: list[int] = []
        for e in reversed(per_cpu):
            if e.request.cpu not in cpus:
                cpus.append(e.request.cpu)
        ordered: list[_Executed] = []
        for cpu in cpus:
            mine = [e for e in per_cpu if e.request.cpu == cpu]
            vf = [e for e in mine if int(e.request.cmd) == int(Cmd.VOLTAGE_FREQ_WRITE)]
            ordered += list(reversed(vf)) + [e for e in mine if int(e.request.cmd) != int(Cmd.VOLTAGE_FREQ_WRITE)]
        ordered += list(reversed(package))
        mailbox: list[Request | MsrRequest] = [
            Request(e.request.cpu, e.request.cmd, e.request.p1, e.request.p2, self._inverse_data(e, per_cpu), "revert")
            for e in ordered
        ]
        msr_first: list[Request | MsrRequest] = []
        msr_last: list[Request | MsrRequest] = []
        for m in reversed(self._msr_journal):
            undo = MsrRequest(m.request.cpu, m.request.msr, m.baseline, "revert", "write", m.request.verify_cpu)
            raises = any(u > w for u, w in zip(turbo_bytes(m.baseline), turbo_bytes(m.request.value)))
            (msr_last if raises else msr_first).append(undo)
        return msr_first + mailbox + msr_last

    def _undo_vf_word(self, entry: _Executed) -> int:
        """The word before a ``0x11``: the previous ``0x11`` of its chain, else the XTU-shaped baseline. T-025: a **point** word (``P2 != 0``)
        is undone with the baseline word of that point **exactly** (its V target is 0 by design: filling it from ``0x09`` would turn a
        per-point offset write into a voltage write)."""
        if entry.prior is not None:
            return entry.prior
        if int(entry.request.p2) != 0:
            return entry.baseline
        kind = self._policy.core_kind(entry.request.cpu)
        written_oc_ratio = VfWord.from_u32(entry.request.data).ratio > top_fused_ratio(kind)  # G-122: that word made an OC point (e.g. XTU's E 48)
        return _revert_vf_word(entry.baseline, entry.favored, entry.fused, kind, cap_ratio=not written_oc_ratio)

    def _inverse_data(self, entry: _Executed, entries: list[_Executed]) -> int:
        cmd = int(entry.request.cmd)
        if cmd == int(Cmd.VOLTAGE_FREQ_WRITE):
            return self._undo_vf_word(entry)
        if cmd == int(Cmd.FAVORED_CORE_WRITE):
            for other in entries:  # the ratio of the LAST undo word of this CPU = its oldest 0x11's undo (what the 0x1D check wants)
                if (
                    other.request.cpu == entry.request.cpu
                    and int(other.request.cmd) == int(Cmd.VOLTAGE_FREQ_WRITE)
                    and (int(other.request.p1), int(other.request.p2)) == (0, 0)  # T-025: a point word's ratio is not the 0x1D ratio
                ):
                    return VfWord.from_u32(self._undo_vf_word(other)).ratio
            return entry.baseline & 0xFF
        return entry.baseline

    @overload
    def execute(self, req: Request, confirm: Confirmation) -> Result: ...

    @overload
    def execute(self, req: MsrRequest, confirm: Confirmation) -> MsrResult: ...

    def execute(self, req: Request | MsrRequest, confirm: Confirmation) -> Result | MsrResult:
        """Run one request through every check and, if all pass, send it. See the module docstring for the order. A :class:`MsrRequest`
        (plain MSR, T-023) takes the same chain and returns an :class:`MsrResult`."""
        if not isinstance(req, (Request, MsrRequest)):
            raise TypeError("execute() needs a arltuner.guard.Request or MsrRequest")
        if not self._busy.acquire(blocking=False):
            self._refuse(Refusal.LOCKED, "another execute() is in progress in this process", req, confirm)
        try:
            if isinstance(req, MsrRequest):
                return self._execute_msr(req, confirm)
            return self._execute(req, confirm)
        finally:
            self._busy.release()

    # ---------------------------------------------------------------- checks
    def _execute(self, req: Request, confirm: Confirmation) -> Result:
        if self._tripped:
            self._refuse(Refusal.TRIPPED, f"guard tripped earlier: {self._trip_reason}", req, confirm)
        if not self._env_flag_set():
            self._refuse(Refusal.NO_ENV_FLAG, f"environment variable {ENV_FLAG} is not '1'", req, confirm)
        problem = self._confirmation_problem(confirm)
        if problem:
            self._refuse(Refusal.BAD_CONFIRMATION, problem, req, confirm)
        if not self._policy.cpu_allowed(req.cpu):
            self._refuse(Refusal.CPU_NOT_ALLOWED, f"cpu {req.cpu!r} is not in the policy CPU set", req, confirm)
        entry = self._policy.match(req.cmd, req.p1, req.p2)
        if entry is None:
            self._refuse(
                Refusal.NOT_ALLOWLISTED,
                f"cmd/p1/p2 {req.cmd!r}/{req.p1!r}/{req.p2!r} matches no allow entry (default deny)",
                req,
                confirm,
            )
        assert entry is not None
        cmd = int(req.cmd)
        write = is_write(cmd)
        twin: int | None = None
        baseline: int | None = None
        if write:
            twin = _READ_TWIN.get(cmd)
            if twin is None:
                self._refuse(Refusal.BASELINE_MISSING, f"write cmd {cmd:#04x} has no read twin to baseline against", req, confirm)
            assert twin is not None
            baseline = self._baselines.get((twin, int(req.cpu), int(req.p1), int(req.p2)))
            if baseline is None:
                self._refuse(
                    Refusal.BASELINE_MISSING,
                    f"no successful read of cmd {twin:#04x} for cpu {req.cpu}/p1 {req.p1}/p2 {req.p2} in this session",
                    req,
                    confirm,
                )
        self._check_request(req, entry, baseline, confirm)
        if self._ops >= self._policy.max_operations_per_session:
            self._refuse(
                Refusal.SESSION_LIMIT,
                f"{self._ops} operation(s) already executed; session limit is {self._policy.max_operations_per_session}",
                req,
                confirm,
            )
        boot_id = self._current_boot_id()
        state = self._audit_state(req, confirm)
        self._check_boot_limit(state, boot_id, req, confirm)
        self._check_dwell(req, confirm)
        self._check_audit_dwell(state, req, confirm)
        self._take_lock(req, confirm)
        try:
            # Under the lock no other process can be between its own checks and its attempt record: look again.
            state = self._audit_state(req, confirm)
            self._check_boot_limit(state, boot_id, req, confirm)
            self._check_audit_dwell(state, req, confirm)
            gate_before = self._run_health("before")
            if gate_before is not None and not gate_before[0]:
                self._refuse(Refusal.HEALTH_GATE, f"health gate (before) says no: {gate_before[1]}", req, confirm)
            self._record(
                "attempt",
                req,
                confirm,
                baseline=baseline,
                health_gate=None if gate_before is None else {"before": gate_before[1]},
                boot_id=boot_id,
            )
            self._ops += 1  # from here on the request counts, whatever happens
            try:
                return self._send_and_verify(req, confirm, baseline, twin)
            except BaseException as exc:
                self._trip(f"{type(exc).__name__}: {exc}", req, confirm)
                raise
            finally:
                self._last_op_at = self._clock()
        finally:
            self._release_lock(req, confirm)

    # ---------------------------------------------------------------- plain MSRs (T-023)
    def _execute_msr(self, req: MsrRequest, confirm: Confirmation) -> MsrResult:
        """The plain-MSR twin of :meth:`_execute`: same order, same refusals; the entry comes from ``Policy.msr_allow``."""
        if self._tripped:
            self._refuse(Refusal.TRIPPED, f"guard tripped earlier: {self._trip_reason}", req, confirm)
        if not self._env_flag_set():
            self._refuse(Refusal.NO_ENV_FLAG, f"environment variable {ENV_FLAG} is not '1'", req, confirm)
        problem = self._confirmation_problem(confirm)
        if problem:
            self._refuse(Refusal.BAD_CONFIRMATION, problem, req, confirm)
        if self._msr_port is None:
            self._refuse(Refusal.NOT_ALLOWLISTED, "plain MSR access is off: this guard was built without an msr_port", req, confirm)
        if not self._policy.cpu_allowed(req.cpu):
            self._refuse(Refusal.CPU_NOT_ALLOWED, f"cpu {req.cpu!r} is not in the policy CPU set", req, confirm)
        entry = self._policy.match_msr(req.msr, req.kind)
        if entry is None:
            self._refuse(
                Refusal.NOT_ALLOWLISTED,
                f"plain msr/kind {req.msr!r}/{req.kind!r} matches no msr_allow entry (default deny)",
                req,
                confirm,
            )
        assert entry is not None
        if not entry.covers_cpu(int(req.cpu)):
            self._refuse(Refusal.CPU_NOT_ALLOWED, f"cpu {req.cpu} is not covered by msr_allow entry {entry.name!r}", req, confirm)
        write = entry.kind == "write"
        baseline: int | None = None
        if write:
            baseline = self._msr_baselines.get((int(req.cpu), int(req.msr)))
            if baseline is None:
                self._refuse(
                    Refusal.BASELINE_MISSING,
                    f"no successful rdmsr of {int(req.msr):#x} on cpu {req.cpu} in this session (the baseline comes first)",
                    req,
                    confirm,
                )
        self._check_msr_request(req, entry, baseline, confirm)
        if self._ops >= self._policy.max_operations_per_session:
            self._refuse(
                Refusal.SESSION_LIMIT,
                f"{self._ops} operation(s) already executed; session limit is {self._policy.max_operations_per_session}",
                req,
                confirm,
            )
        boot_id = self._current_boot_id()
        state = self._audit_state(req, confirm)
        self._check_boot_limit(state, boot_id, req, confirm)
        self._check_dwell(req, confirm)
        self._check_audit_dwell(state, req, confirm)
        self._take_lock(req, confirm)
        try:
            state = self._audit_state(req, confirm)
            self._check_boot_limit(state, boot_id, req, confirm)
            self._check_audit_dwell(state, req, confirm)
            gate_before = self._run_health("before")
            if gate_before is not None and not gate_before[0]:
                self._refuse(Refusal.HEALTH_GATE, f"health gate (before) says no: {gate_before[1]}", req, confirm)
            self._record(
                "attempt" if write else "msr_read",
                req,
                confirm,
                baseline=baseline,
                health_gate=None if gate_before is None else {"before": gate_before[1]},
                boot_id=boot_id,
            )
            if write:
                self._ops += 1  # from here on the write counts, whatever happens (a plain read is not a WRMSR and does not)
            try:
                return self._send_and_verify_msr(req, confirm, baseline)
            except BaseException as exc:
                self._trip(f"{type(exc).__name__}: {exc}", req, confirm)
                raise
            finally:
                if write:
                    self._last_op_at = self._clock()
        finally:
            self._release_lock(req, confirm)

    def _check_msr_request(self, req: MsrRequest, entry: MsrAllowEntry, baseline: int | None, confirm: Confirmation) -> None:
        """CHECK_FAILED: well-formedness, the write's verify CPU, then the entry's named predicate (writes only)."""
        value = req.value
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
            self._refuse(Refusal.CHECK_FAILED, f"value {value!r} is not a u64", req, confirm)
        if not isinstance(req.purpose, str) or not req.purpose.strip():
            self._refuse(Refusal.CHECK_FAILED, "purpose must be a non-blank string (it goes into the audit log)", req, confirm)
        if entry.kind == "read":
            if value != 0 or req.verify_cpu is not None:
                self._refuse(Refusal.CHECK_FAILED, f"a plain read carries no payload; value {value:#x}, verify_cpu {req.verify_cpu!r}", req, confirm)
            return
        verify = req.verify_cpu
        if isinstance(verify, bool) or not isinstance(verify, int):
            self._refuse(Refusal.CHECK_FAILED, f"a plain write needs a verify_cpu (an int), got {verify!r}", req, confirm)
        cpu = int(req.cpu)
        if verify == cpu:
            self._refuse(Refusal.CHECK_FAILED, f"verify_cpu {verify} is the written cpu: the read-back must also look at another CPU", req, confirm)
        if not self._policy.cpu_allowed(verify):
            self._refuse(Refusal.CPU_NOT_ALLOWED, f"verify_cpu {verify} is not in the policy CPU set", req, confirm)
        if self._policy.core_kind(verify) == self._policy.core_kind(cpu):
            self._refuse(
                Refusal.CHECK_FAILED,
                f"verify_cpu {verify} is a {self._policy.core_kind(verify)} CPU like cpu {cpu}: the scope of this MSR is UNVERIFIED, so the "
                "read-back must be on a CPU of the other core kind",
                req,
                confirm,
            )
        reader = self._policy.match_msr(req.msr, "read")
        if reader is None or not reader.covers_cpu(cpu) or not reader.covers_cpu(verify):
            self._refuse(
                Refusal.CHECK_FAILED,
                f"the read-back needs a msr_allow read entry for {int(req.msr):#x} covering cpu {cpu} and verify_cpu {verify}",
                req,
                confirm,
            )
        check = MSR_CHECKS.get(entry.check) if entry.check is not None else None
        if check is None or baseline is None:
            self._refuse(Refusal.CHECK_FAILED, f"write entry {entry.name!r} has no evaluable check", req, confirm)
        assert check is not None and baseline is not None
        try:
            context = build_msr_context(self._policy, int(req.msr), platform_info=self._platform_info())
            ok, detail = check(req, baseline, entry.params, context)
        except Exception as exc:
            ok, detail = False, f"check {entry.check!r} raised {type(exc).__name__}: {exc}"
        if ok is not True:
            self._refuse(Refusal.CHECK_FAILED, f"{entry.check}: {detail}", req, confirm)

    def _platform_info(self) -> int | None:
        """The ``0xCE`` word read this session (any CPU): the one with the highest base ratio ``[15:8]`` (the strictest floor), or ``None``."""
        words = [v for (_, msr), v in self._msr_baselines.items() if msr == 0xCE]
        return max(words, key=lambda w: (w >> 8) & 0xFF, default=None)

    def _send_and_verify_msr(self, req: MsrRequest, confirm: Confirmation, baseline: int | None) -> MsrResult:
        assert self._msr_port is not None
        cpu, msr, value = int(req.cpu), int(req.msr), int(req.value)
        port = self._msr_port
        if baseline is None:  # a read
            try:
                word = _require_u64(port.rdmsr(cpu, msr), f"rdmsr cpu {cpu} msr {msr:#x}")
            except BaseException as exc:
                self._record_quietly("error", req, confirm, detail=f"{type(exc).__name__}: {exc}", stage="rdmsr")
                raise
            self._msr_baselines[(cpu, msr)] = word
            self._record("result", req, confirm, value=word)
            gate_after = self._run_health("after")
            if gate_after is not None and not gate_after[0]:
                self._trip(f"health gate (after) says no: {gate_after[1]}", req, confirm)
            return MsrResult(req, word, None, None)
        verify_cpu = int(req.verify_cpu)  # type: ignore[arg-type]  # validated by _check_msr_request
        try:
            port.wrmsr(cpu, msr, value)
        except BaseException as exc:
            self._msr_journal.append(_MsrExecuted(req, baseline))  # outcome unknown: keep it revertible
            self._record_quietly("error", req, confirm, detail=f"{type(exc).__name__}: {exc}", stage="wrmsr")
            raise
        self._msr_journal.append(_MsrExecuted(req, baseline))
        self._record("result", req, confirm, baseline=baseline)
        seen: list[int] = []
        for where in (cpu, verify_cpu):
            try:
                seen.append(_require_u64(port.rdmsr(where, msr), f"read-back cpu {where} msr {msr:#x}"))
            except BaseException as exc:
                self._record_quietly("error", req, confirm, detail=f"{type(exc).__name__}: {exc}", stage="readback", readback_cpu=where)
                raise
            self._record("readback", req, confirm, value=seen[-1], verified=seen[-1] == value, readback_cpu=where, expected=f"0x{value:016X}")
            if seen[-1] != value:
                break  # the first mismatch trips; nothing is read further
        verified = len(seen) == 2 and all(w == value for w in seen)
        if not verified:
            where = cpu if len(seen) == 1 else verify_cpu
            self._trip(
                f"read-back of plain msr {msr:#x} on cpu {where} gave {seen[-1]:#018x}, expected {value:#018x}"
                + ("" if where == cpu else f" (written on cpu {cpu}: the MSR is not package-scoped, or the write did not take)"),
                req,
                confirm,
            )
        gate_after = self._run_health("after")
        if gate_after is not None and not gate_after[0]:
            self._trip(f"health gate (after) says no: {gate_after[1]}", req, confirm)
        return MsrResult(req, seen[0], seen[1] if len(seen) == 2 else None, verified)

    def _env_flag_set(self) -> bool:
        try:
            return self._env.get(ENV_FLAG) == "1"
        except Exception:
            return False

    def _confirmation_problem(self, confirm: object) -> str | None:
        if not isinstance(confirm, Confirmation):
            return "no Confirmation given"
        token, operator = confirm.token, confirm.operator
        if not isinstance(token, str) or not isinstance(operator, str):
            return "confirmation token and operator must be strings"
        if not operator.strip():
            return "confirmation operator is empty"
        if not hmac.compare_digest(token.encode("utf-8", "surrogatepass"), self._token):
            return "confirmation token does not match this session's token"
        return None

    def _check_request(self, req: Request, entry: AllowEntry, baseline: int | None, confirm: Confirmation) -> None:
        """CHECK_FAILED: generic well-formedness, then the entry's named predicate (writes only)."""
        data = req.data
        if isinstance(data, bool) or not isinstance(data, int) or not 0 <= data <= _U32_MAX:
            self._refuse(Refusal.CHECK_FAILED, f"data {data!r} is not a u32", req, confirm)
        if not isinstance(req.purpose, str) or not req.purpose.strip():
            self._refuse(Refusal.CHECK_FAILED, "purpose must be a non-blank string (it goes into the audit log)", req, confirm)
        if entry.kind == "read":
            if data != 0:
                self._refuse(Refusal.CHECK_FAILED, f"a read carries no payload; data {data:#x} != 0", req, confirm)
            return
        check = CHECKS.get(entry.check) if entry.check is not None else None
        if check is None or baseline is None:
            self._refuse(Refusal.CHECK_FAILED, f"write entry {entry.name!r} has no evaluable check", req, confirm)
        assert check is not None and baseline is not None
        try:
            ok, detail = check(req, baseline, entry.params, self._context(req))
        except Exception as exc:
            ok, detail = False, f"check {entry.check!r} raised {type(exc).__name__}: {exc}"
        if ok is not True:
            self._refuse(Refusal.CHECK_FAILED, f"{entry.check}: {detail}", req, confirm)

    def _context(self, req: Request) -> CheckContext:
        """What this session knows about ``req.cpu`` for the named checks (``cpu`` is an int: ``cpu_allowed`` ran first)."""
        cpu = int(req.cpu)
        return build_context(
            self._policy,
            cpu,
            favored_word=self._baselines.get((int(Cmd.FAVORED_CORE_READ), cpu, 0, 0)),
            fused_word=self._baselines.get((int(Cmd.VOLTAGE_FLOOR_READ), cpu, 0x11, 0)),
            vf_word_sent=self._last_vf_word_sent(cpu),
            max_voltage_word=self._baselines.get((int(Cmd.MAXIMUM_VOLTAGE_LIMIT_READ), cpu, 0, 0)),
        )

    def _last_vf_word_sent(self, cpu: int) -> int | None:
        """The data of the last 0x11 (P1=0, P2=0) this session sent to ``cpu`` that completed rc 0 (or whose outcome is unknown)."""
        for e in reversed(self._journal):
            r = e.request
            if int(r.cmd) == int(Cmd.VOLTAGE_FREQ_WRITE) and int(r.cpu) == cpu and (int(r.p1), int(r.p2)) == (0, 0):
                return r.data
        return None

    def _journal_entry(self, req: Request, baseline: int) -> _Executed:
        cpu, cmd = int(req.cpu), int(req.cmd)
        stamp = self._stamps.get((_READ_TWIN[cmd], cpu, int(req.p1), int(req.p2)), 0)
        prior: int | None = None
        if cmd == int(Cmd.VOLTAGE_FREQ_WRITE):
            for e in reversed(self._journal):  # D-016: the state this write leaves behind is what undoing the *next* write restores
                r = e.request
                if (int(r.cmd), int(r.cpu), int(r.p1), int(r.p2)) == (cmd, cpu, int(req.p1), int(req.p2)) and e.stamp == stamp:
                    prior = r.data
                    break
        return _Executed(
            req,
            baseline,
            favored=self._baselines.get((int(Cmd.FAVORED_CORE_READ), cpu, 0, 0)),
            fused=self._baselines.get((int(Cmd.VOLTAGE_FLOOR_READ), cpu, 0x11, 0)),
            stamp=stamp,
            prior=prior,
        )

    def _check_dwell(self, req: Request, confirm: Confirmation) -> None:
        """DWELL: refuse (never sleep) if the previous executed operation finished less than ``min_dwell_s`` ago."""
        if self._last_op_at is None or self._policy.min_dwell_s <= 0:
            return
        try:
            elapsed = self._clock() - self._last_op_at
        except Exception as exc:
            self._refuse(Refusal.DWELL, f"clock unavailable: {exc!r}", req, confirm)
        if not elapsed >= self._policy.min_dwell_s:  # `not >=` also refuses NaN
            self._refuse_dwell(
                f"{elapsed:.3f}s since the previous operation; minimum dwell is {self._policy.min_dwell_s:g}s "
                "(check kernel/RAS/PCI health, then retry later -- the guard does not wait)",
                self._policy.min_dwell_s - elapsed,
                req,
                confirm,
            )

    def _current_boot_id(self) -> str | None:
        """This boot's id for the audit log; anything unusable becomes ``None`` (an unknown boot, still capped)."""
        try:
            value = self._boot_id_fn()
        except Exception:
            return None
        if not isinstance(value, str):
            return None
        return value.strip() or None

    def _audit_state(self, req: Request, confirm: object) -> AuditState:
        """Read the audit log or refuse ``AUDIT_UNAVAILABLE``: cross-process limits that cannot be evaluated are not waived."""
        try:
            state = read_state(self._audit.path)
            if not isinstance(state, AuditState):
                raise TypeError(f"read_state returned {type(state).__name__}")
            return state
        except (AuditUnreadable, OSError) as exc:
            self._refuse(Refusal.AUDIT_UNAVAILABLE, f"cannot evaluate the cross-process limits from the audit log: {exc}", req, confirm)
        except Exception as exc:
            self._refuse(
                Refusal.AUDIT_UNAVAILABLE, f"audit log state unavailable: {type(exc).__name__}: {exc}", req, confirm
            )

    def _check_boot_limit(self, state: AuditState, boot_id: str | None, req: Request, confirm: object) -> None:
        """BOOT_LIMIT: ``attempt`` records of this boot (reads included, read-backs excluded) vs ``max_operations_per_boot``."""
        done = state.attempts_by_boot.get(boot_id, 0)
        if done >= self._policy.max_operations_per_boot:
            which = "an unknown boot (boot id unavailable)" if boot_id is None else f"boot {boot_id}"
            self._refuse(
                Refusal.BOOT_LIMIT,
                f"{done} mailbox operation(s) already recorded for {which} in {self._audit.path}; per-boot limit is "
                f"{self._policy.max_operations_per_boot} (SAFETY.md 'Mailbox-session discipline'; a reboot starts a new count, "
                "or the human raises max_operations_per_boot in a reviewed policy)",
                req,
                confirm,
            )

    def _check_audit_dwell(self, state: AuditState, req: Request, confirm: object) -> None:
        """DWELL across processes: refuse (never sleep) if the latest ``attempt`` in the audit log -- from any process -- is
        younger than ``min_dwell_s``, or lies in the future (clock went backwards: the elapsed time cannot be trusted)."""
        last = state.last_attempt_wall
        if last is None:
            return
        dwell = self._policy.min_dwell_s
        try:
            now = self._wall_clock()
            if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
                raise ValueError(f"wall clock returned {now!r}")
        except Exception as exc:
            self._refuse(Refusal.DWELL, f"wall clock unavailable: {exc!r}", req, confirm)
        elapsed = now - last
        if elapsed < -_FUTURE_SLACK_S:
            self._refuse(
                Refusal.DWELL,
                f"previous mailbox operation at {_when(last)} is {-elapsed:.1f}s in the future (the wall clock went backwards?); "
                "the dwell cannot be evaluated, so the request is refused",
                req,
                confirm,
            )
        if dwell > 0 and not elapsed >= dwell:  # `not >=` also refuses NaN
            remaining = dwell - max(elapsed, 0.0)
            self._refuse_dwell(
                f"previous mailbox operation at {_when(last)}, possibly another process ({max(elapsed, 0.0):.1f}s ago); minimum "
                f"dwell is {dwell:g}s -- wait about {math.ceil(remaining)}s, check kernel/RAS/PCI health, then retry "
                "(the guard does not wait)",
                remaining,
                req,
                confirm,
            )

    def _run_health(self, phase: str) -> tuple[bool, str] | None:
        """``None`` when no gate is configured; otherwise ``(ok, detail)``. A gate that raises or answers oddly is *not ok*."""
        if self._health_gate is None:
            return None
        try:
            ok, detail = self._health_gate(phase)
            if not isinstance(ok, bool):
                raise TypeError(f"gate returned ok={ok!r}, not a bool")
            return ok, str(detail)
        except Exception as exc:
            return False, f"health gate ({phase}) could not be evaluated: {type(exc).__name__}: {exc}"

    # ---------------------------------------------------------------- lock file
    def _take_lock(self, req: Request, confirm: Confirmation) -> None:
        """LOCKED: exclusive ``O_CREAT|O_EXCL`` lock file holding our pid (portable, no ``fcntl``). An existing file is
        never removed here -- it may belong to a live process; the human clears a stale one by hand."""
        try:
            fd = os.open(
                self._lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
        except FileExistsError:
            self._refuse(Refusal.LOCKED, f"lock file {self._lock_path} exists ({self._lock_holder()})", req, confirm)
        except FileNotFoundError:
            self._refuse(Refusal.LOCKED, f"cannot create lock file {self._lock_path}: its directory {self._lock_path.parent} does not exist", req, confirm)
        except OSError as exc:
            self._refuse(Refusal.LOCKED, f"cannot create lock file {self._lock_path}: {exc!r}", req, confirm)
        try:
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except OSError as exc:
            os.close(fd)
            try:
                self._unlink_lock()  # we created this file a moment ago; do not leave a half-made lock behind
            except OSError:
                pass
            self._refuse(Refusal.LOCKED, f"cannot write lock file {self._lock_path}: {exc!r}", req, confirm)
        os.close(fd)

    def _lock_holder(self) -> str:
        """Who holds the lock, for the LOCKED message. The lock directory may be writable by others (``/run/lock``), so what sits at the path is
        untrusted: opened ``O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC`` (a symlink fails, a FIFO does not block), ``fstat``-ed on the descriptor (regular
        file, one link, else never read) and only 32 printable ASCII bytes of it ever reach a message."""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self._lock_path, flags)
        except OSError:
            return "holder unknown"
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                return "holder unknown: the lock path is not a plain regular file, so it was not read"
            raw = os.read(fd, 32)
        except OSError:
            return "holder unknown"
        finally:
            os.close(fd)
        text = "".join(c for c in raw.decode("ascii", "replace").strip() if " " <= c <= "~")
        return "held by pid " + text

    def _unlink_lock(self) -> None:
        try:
            os.unlink(self._lock_path)
        except FileNotFoundError:
            pass

    def _release_lock(self, req: Request, confirm: Confirmation) -> None:
        try:
            self._unlink_lock()
        except OSError as exc:  # a stale lock would refuse every later call anyway; make it loud and stop this guard
            self._trip(f"could not remove lock file {self._lock_path}: {exc!r}", req, confirm)
            self._record_quietly("error", req, confirm, detail=f"lock release failed: {exc!r}", stage="lock")

    # ---------------------------------------------------------------- send
    def _send_and_verify(self, req: Request, confirm: Confirmation, baseline: int | None, twin: int | None) -> Result:
        cpu, cmd, p1, p2 = int(req.cpu), int(req.cmd), int(req.p1), int(req.p2)
        write = twin is not None
        try:
            response = _require_response(self._transport.transact(cpu, cmd, p1, p2, req.data))
        except BaseException as exc:
            if write:
                assert baseline is not None
                self._journal.append(self._journal_entry(req, baseline))  # outcome unknown: keep it revertible
            self._record_quietly("error", req, confirm, detail=f"{type(exc).__name__}: {exc}", stage="transact")
            raise
        if write and response.rc == 0:
            assert baseline is not None
            self._journal.append(self._journal_entry(req, baseline))
        self._record("result", req, confirm, response=response, baseline=baseline)
        if not write:
            if response.rc == 0:
                self._baselines[(cmd, cpu, p1, p2)] = response.data
                self._reads_done += 1
                self._stamps[(cmd, cpu, p1, p2)] = self._reads_done
            readback, verified = None, None
        elif response.rc != 0:
            readback, verified = None, None  # refused by pcode: nothing to verify, rc is in the Result and the audit log
        else:
            assert twin is not None
            twin_req = Request(cpu, twin, p1, p2, 0, f"read-back of: {req.purpose}")
            try:
                readback = _require_response(self._transport.transact(cpu, twin, p1, p2, 0))
            except BaseException as exc:
                self._record_quietly("error", twin_req, confirm, detail=f"{type(exc).__name__}: {exc}", stage="readback")
                raise
            verified = readback.rc == 0 and _READBACK_MATCH.get(cmd, _same_word)(req.data, readback.data)
            self._record(
                "readback", twin_req, confirm, response=readback, verified=verified, expected=f"0x{req.data:08X}"
            )
            if not verified:
                self._trip(
                    f"read-back of cmd {twin:#04x} cpu {cpu} gave rc {readback.rc} data {readback.data:#010x}, "
                    f"expected {req.data:#010x}",
                    req,
                    confirm,
                )
        gate_after = self._run_health("after")
        if gate_after is not None and not gate_after[0]:
            self._trip(f"health gate (after) says no: {gate_after[1]}", req, confirm)
        return Result(req, response, readback, verified)

    # ---------------------------------------------------------------- audit / trip
    def _record(self, event: str, req: Request, confirm: object, **fields: object) -> None:
        """Audit or raise: callers rely on a failed write of the record meaning *do not proceed*."""
        self._audit.append(event, ts=self._wall_clock(), request=req, operator=_operator_of(confirm), **fields)  # type: ignore[arg-type]

    def _record_quietly(self, event: str, req: Request, confirm: object, **fields: object) -> None:
        try:
            self._record(event, req, confirm, **fields)
        except Exception:
            pass  # already failing; never let the audit problem mask the original exception

    def _refuse_dwell(self, detail: str, remaining: float, req: Request, confirm: object) -> NoReturn:
        self._refuse(Refusal.DWELL, detail, req, confirm, retry_after_s=remaining if math.isfinite(remaining) else None)

    def _refuse(
        self, reason: Refusal, detail: str, req: Request, confirm: object, *, retry_after_s: float | None = None
    ) -> NoReturn:
        try:
            self._record("refused", req, confirm, reason=reason.value, detail=detail)
        except Exception as exc:
            detail = f"{detail} [audit write failed: {type(exc).__name__}: {exc}]"
        raise GuardRefused(reason, detail, retry_after_s)

    def _trip(self, why: str, req: Request, confirm: object) -> None:
        first = not self._tripped
        self._tripped = True
        if first:
            self._trip_reason = why
            self._record_quietly("trip", req, confirm, reason="TRIPPED", detail=why)
