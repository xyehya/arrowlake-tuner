"""G-111 ops (contract 9): ``vf.measure`` (long, state ``measuring``), ``curves.list/get/update/delete``, ``mem.read``. **No mailbox, no MSR write.**

``vf.measure`` runs the T-026/T-027 engine (:func:`arltuner.vfdump.measure_group`: cap search, at-target filter, ``MISMATCH``) for the selected tuning units:
pin the unit's CPUs through cpufreq ``scaling_min_freq`` = ``scaling_max_freq`` (G-132, R-001: profile-proof; governor and EPP are not touched), load **all CPUs of the unit** with pinned busy loops, settle, sample MSR ``0x198`` (requested VID =
``[47:32]/8192``, running ratio ``[15:8]``) through a **read-only** MSR port, keep the samples at the target ratio. The only things written are cpufreq sysfs
values (min and max), and every one of them is **journaled before the first change** (``/run/arltuner/measure-journal.json``, root 0600) and restored on success, cancel,
error and at the next helper start if the journal is stale (the helper died mid-run). The load generators carry their own deadline, so a dead helper cannot
leave them spinning.

Everything with side effects is behind :class:`MeasureWiring` / :class:`RunEnv` (cpufreq files, MSR read, load, sleep, store, memory reader), so the tests and
``--sim`` run on fake sysfs and the :class:`SimMeasureIO` model.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from arltuner import vfdump
from arltuner.curves import CurveStore, StoreError, fit_record, is_curve_id, is_unit_id, new_curve_id, summary, unit_kind, unit_order, validate_name
from arltuner.curves.record import RECORD_BUDGET
from arltuner.guard import AuditLog
from arltuner.guard.policy import fused_top_ratio
from arltuner.mailbox.vf import VfWord
from arltuner.topology import Topology

from .cpu_online import refuse_if_cpu_offline
from .ops import OpContext, OpSpec, _is_int, _no_args, _only, _trust, helper_topology
from .protocol import BAD_REQUEST, CANCELLED, INTERNAL, REFUSED, OpError
from .state import State
from .trust import TrustError, check_trusted_path, open_checked

__all__ = [
    "HelperMeasureIO", "MeasureWiring", "RunEnv", "SimCpufreq", "SimMeasureIO", "SimModel", "CpufreqFiles", "device_wiring", "measure_ops", "recover_journal", "sim_wiring",
]

#: Names that live in :mod:`arltuner.helper.measure_sim` (the simulated reference CPU, D-036) and are re-exported lazily, so no runtime path imports the fixture.
_SIM_NAMES = frozenset({"STOCK_VID", "SimCpufreq", "SimMeasureIO", "SimModel", "sim_memory"})


def __getattr__(name: str) -> Any:
    if name in _SIM_NAMES:
        from . import measure_sim

        return getattr(measure_sim, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# ---- measurement constants (SPEC-v3 section 3; the T-027 defaults are 2.5 s settle / 30 samples, the helper's run is shorter on purpose) -------------------------
#: G-132: the cap search of one point that no cap reaches takes about this long at most (it samples a few caps, each ``settle + samples x interval``).
SEARCH_BUDGET_S = 10.0
SETTLE_S = 0.3
SAMPLES = 10
SAMPLE_INTERVAL_S = 0.1
MIN_VALID = 5  # of SAMPLES: half must run at the target ratio (the CLI needs 5 of 30; a knob to tune on the machine, T-027 saw the ratio waver under load)
POINT_OVERHEAD_S = 0.1  # per attempt: cap writes + process start (only for the first ETA, before any point has been timed)
EPP_ATTR = "energy_performance_preference"
CAP_ATTR = "scaling_max_freq"
MAX_ATTR = "cpuinfo_max_freq"
MIN_ATTR = "scaling_min_freq"
GOV_ATTR = "scaling_governor"
#: What the measure run changes (journaled, restored): G-132 (R-001) pins ``scaling_min_freq`` = ``scaling_max_freq``; profile, governor and EPP are never written. The stress run
#: (``ops_stress``) journals ``(CAP_ATTR, EPP_ATTR)``. A journal written by an older helper (cap + EPP) is still restored, so all three are accepted from a journal file.
TOUCHED = (CAP_ATTR, MIN_ATTR)
JOURNAL_ATTRS = (CAP_ATTR, MIN_ATTR, EPP_ATTR)
CONTEXT = (MIN_ATTR, GOV_ATTR)  # recorded as context when the run does not touch it
NO_TURBO_PATH = "intel_pstate/no_turbo"
BASE_ATTR = "base_frequency"
TURBO_OFF_NOTE = "Turbo is off (power-saver profile?): ratios above base cannot be reached \u2014 switch to Balanced or Performance"
SPINNER = (
    "import os,time\n"
    "os.sched_setaffinity(0,{{{cpu}}})\n"
    "t=time.monotonic()+{seconds}\n"
    "x=1\n"
    "while time.monotonic()<t:\n"
    " for _ in range(20000):\n"
    "  x=(x*1103515245+12345)&0xFFFFFFFF\n"
)
PYTHON = "/usr/bin/python3"
SIM_TIME_SCALE = 1 / 12  # --sim: a quick run takes about ten seconds instead of two minutes


# =============================================================================================================================================
# cpufreq files
# =============================================================================================================================================
class CpufreqFiles:
    """Named cpufreq attributes of a CPU under ``root`` (``/sys/devices/system/cpu``): ``cpu<N>/cpufreq/<attr>``. ``read`` gives ``None`` for a file that is not there."""

    def __init__(self, root: Path | str = "/sys/devices/system/cpu") -> None:
        self.root = Path(root)

    def path(self, cpu: int, attr: str) -> Path:
        return self.root / f"cpu{cpu}" / "cpufreq" / attr

    def read(self, cpu: int, attr: str) -> str | None:
        try:
            return self.path(cpu, attr).read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return None

    def write(self, cpu: int, attr: str, value: str) -> None:
        self.path(cpu, attr).write_text(str(value), encoding="ascii")

    def read_no_turbo(self) -> bool | None:
        """``intel_pstate/no_turbo`` (G-132): ``True`` when turbo is off (power-saver profile), ``None`` when the file is not there or unreadable."""
        try:
            return (self.root / NO_TURBO_PATH).read_text(encoding="ascii").strip() == "1"
        except OSError:
            return None


# =============================================================================================================================================
# the real load generator and IO
# =============================================================================================================================================
class _Spinners:
    def __init__(self, procs: list[Any]) -> None:
        self._procs = procs

    def stop(self) -> None:
        for p in self._procs:
            try:
                if p.poll() is None:
                    p.terminate()
            except OSError:
                pass
        for p in self._procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    p.kill()
                    p.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            except OSError:
                pass


class HelperMeasureIO(vfdump.SysfsMeasureIO):
    """:class:`~arltuner.vfdump.SysfsMeasureIO` with the helper's load generator: one ``python3 -I -c`` busy loop per CPU of the unit, pinned with
    ``sched_setaffinity``, fixed environment, cwd ``/``, no shell, ``close_fds``, and a self-deadline of ``seconds`` (so a helper that dies cannot orphan them). As root
    the interpreter is resolved (``/usr/bin/python3`` is a symlink on Fedora) and must be root-owned and not group/world-writable (SPEC 4.4) before every spawn."""

    def __init__(
        self,
        root: Path = Path("/sys/devices/system/cpu"),
        popen: Callable[..., Any] = subprocess.Popen,
        hwmon_root: Path = Path("/sys/class/hwmon"),
        *,
        python: str = PYTHON,
        trust_check: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(root=root, popen=popen, which=lambda name: None, hwmon_root=hwmon_root)
        self._python, self._trust_check = python, trust_check

    def _exe(self) -> str:
        exe = os.path.realpath(self._python)
        if self._trust_check is not None:
            self._trust_check(exe)
        return exe

    def check(self) -> str | None:
        try:
            self._exe()
        except (TrustError, OSError) as exc:
            return f"the load generator ({self._python}) is not usable: {exc}"
        return None

    def start_load(self, cpus: Sequence[int], seconds: float) -> _Spinners:
        exe = self._exe()
        procs: list[Any] = []
        try:
            for cpu in cpus:
                code = SPINNER.format(cpu=int(cpu), seconds=repr(float(max(1.0, seconds))))
                procs.append(self._popen(
                    [exe, "-I", "-c", code], cwd="/", env={"LC_ALL": "C"}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    close_fds=True, shell=False,
                ))
        except BaseException:
            _Spinners(procs).stop()
            raise
        return _Spinners(procs)


def intermittent_ratios(points: Sequence[int], favored: bool, all_core_ratio: int | None) -> tuple[int, ...]:
    """Ratios of a unit that only a few active cores allow (F3): those of a favored P-core above the all-core bucket of the P turbo table (live ``0x1AD``). Unknown
    table or an unfavored unit: none."""
    if not favored or all_core_ratio is None:
        return ()
    return tuple(r for r in points if r > all_core_ratio)


# =============================================================================================================================================
# wiring
# =============================================================================================================================================
@dataclass
class RunEnv:
    """What one ``vf.measure`` run uses: the IO, the ``0x198`` reader (read-only), the sleep, and a ``close`` for whatever the reader holds."""

    io: Any
    read_perf: Callable[[int], int]
    sleep: Callable[[float], object] = time.sleep
    close: Callable[[], None] = lambda: None
    #: plain ``rdmsr(cpu, msr)`` of the turbo tables ``0x1AD`` / ``0x650`` (G-118); ``None`` = ``io.read_msr`` when it has one, else the tables are not read.
    rdmsr: Callable[[int, int], int] | None = None


@dataclass
class MeasureWiring:
    cpufreq: Any  # CpufreqFiles | SimCpufreq
    make_run: Callable[[], RunEnv]
    store: CurveStore
    journal_path: Path
    mem_fn: Callable[[], dict[str, Any]]
    euid: int = field(default_factory=lambda: getattr(os, "geteuid", lambda: 0)())
    kernel_fn: Callable[[], str] = lambda: os.uname().release if hasattr(os, "uname") else ""
    wall_clock: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    settle_s: float = SETTLE_S
    samples: int = SAMPLES
    interval_s: float = SAMPLE_INTERVAL_S
    prepare: Callable[[], None] = lambda: None  # called before a run writes anything: --sim creates its scratch state directory here, lazily (a killed sim helper leaves nothing)


def sim_wiring(state_dir: Path | str, *, time_scale: float = SIM_TIME_SCALE, topology: Topology | None = None) -> MeasureWiring:
    """``--sim``: the model of the simulated part, an in-memory cpufreq, the store and the journal under ``state_dir`` (never ``/var/lib``)."""
    from .measure_sim import SimCpufreq, SimMeasureIO, SimModel, sim_memory

    state = Path(state_dir)
    cpufreq = SimCpufreq(SimModel(topology=topology))
    io = SimMeasureIO(cpufreq, cpufreq.model)
    return MeasureWiring(
        cpufreq=cpufreq,
        make_run=lambda: RunEnv(io, io.read_perf, lambda s: time.sleep(s * time_scale)),
        store=CurveStore(state / "curves"),
        journal_path=state / "measure-journal.json",
        mem_fn=sim_memory,
        kernel_fn=lambda: "sim",
        prepare=lambda: state.mkdir(mode=0o700, parents=True, exist_ok=True),
    )


def device_wiring(*, lstat_fn: Callable[[str], Any] = os.lstat) -> MeasureWiring:
    """The root helper: installed paths only (``/var/lib/arltuner/curves``, ``/run/arltuner/measure-journal.json``), a read-only ``/dev/cpu/N/msr`` port for
    ``0x198`` (through :class:`~arltuner.telemetry.TelemetryMsr`: rdmsr of allowlisted telemetry MSRs only), the real ``mem.read``."""
    from arltuner.helper.config import INSTALLED_LOCK_DIR, INSTALLED_STATE_DIR
    from arltuner.msr import DevCpuMsrPort
    from arltuner.telemetry import TelemetryMsr
    from arltuner.telemetry.mem import read_memory
    from arltuner.telemetry.msrio import MSR_PERF_STATUS, TURBO_TABLE_MSRS

    def trust_check(path: str) -> None:
        check_trusted_path(path, want="file", lstat_fn=lstat_fn)

    def make_run() -> RunEnv:
        port = DevCpuMsrPort(writable=False)
        reader = TelemetryMsr(port, TURBO_TABLE_MSRS)  # read-only; the turbo tables 0x1AD / 0x650 on top of the telemetry set (D-026 rule: rdmsr only, never a write)
        return RunEnv(HelperMeasureIO(trust_check=trust_check), lambda cpu: reader.rdmsr(cpu, MSR_PERF_STATUS), time.sleep, port.close, reader.rdmsr)

    return MeasureWiring(
        cpufreq=CpufreqFiles(),
        make_run=make_run,
        store=CurveStore(INSTALLED_STATE_DIR / "curves", enforce_trust=True, lstat_fn=lstat_fn),
        journal_path=INSTALLED_LOCK_DIR / "measure-journal.json",
        mem_fn=read_memory,
    )


# =============================================================================================================================================
# the audit trail of a cpufreq run (G-235)
# =============================================================================================================================================
class CpufreqAudit:
    """G-235: the audit trail of a cpufreq run (``vf.measure`` / ``stress.start`` / the recovery of a stale journal) is **summary records**, not one per write: ONE fsync'd
    ``runtime_attempt`` (:meth:`begin`) before the first write, listing every file the run will touch with its original value (what the journal holds) and the planned caps; ONE
    ``result`` at the end (:meth:`result`, also on cancel / error): restored ok or not, duration, points. A write or restore that FAILS gets its own ``error`` record. None of them
    is a mailbox ``attempt`` or a dwell, so the per-boot budget is untouched. The log is opened (so proven writable) at construction, **before** the run changes anything, and
    :meth:`begin` refuses the run when the start record cannot be written; after that a record that cannot be written is counted and reported once -- it never stops a write, because
    the restore must always go through."""

    def __init__(self, helper: Any, op: str) -> None:
        cfg = helper.cfg
        _trust(helper, cfg.audit_path, want="file", must_exist=False)
        try:
            os.close(open_checked(cfg.audit_path, write=True, create=True, mode=0o600, euid=cfg.euid_fn(), what="audit log"))
            self._log = AuditLog(cfg.audit_path)
        except (TrustError, OSError) as exc:
            raise OpError(REFUSED, f"the audit log {cfg.audit_path} cannot be written ({exc}); nothing was changed", {"check": "AUDIT_UNAVAILABLE"}) from None
        self._cfg, self._helper, self.op = cfg, helper, op
        self._reset()
        self._lost = 0
        self._why = ""

    def _reset(self) -> None:
        self.writes = 0
        self.file_count = 0
        self.failures: list[dict[str, str]] = []

    def _boot(self) -> str | None:
        try:
            boot = self._cfg.boot_id_fn()
        except Exception:
            return None
        return boot.strip() or None if isinstance(boot, str) else None

    def _emit(self, event: str, op: str | None, **fields: Any) -> None:
        self._log.append(event, ts=self._cfg.wall_clock(), operator=self._cfg.operator, kind="cpufreq", boot_id=self._boot(), op=op or self.op, **fields)

    def _append(self, event: str, op: str | None = None, **fields: Any) -> None:
        try:
            self._emit(event, op, **fields)
        except (OSError, ValueError) as exc:
            self._lost += 1
            self._why = str(exc)

    def begin(self, entries: Sequence[dict[str, Any]], *, op: str | None = None, strict: bool = True, **planned: Any) -> None:
        """The start record, before the first write: every journaled file with its value now (``entries``) and the plan (``planned``). ``strict``: an unwritable log is a refusal
        (nothing has changed yet); the recovery of a stale journal is not strict -- the restore goes through whatever the log says."""
        files = [{"path": f"cpu{e['cpu']}/cpufreq/{e['attr']}", "value": e["value"]} for e in entries]
        self.file_count = len(files)
        try:
            self._emit("runtime_attempt", op, domain="cpufreq", key="run", files=files, file_count=len(files), **planned,
                       detail=f"{op or self.op}: {len(files)} cpufreq file(s) journaled; the caps and the restore follow")
        except (OSError, ValueError) as exc:
            if strict:
                raise OpError(REFUSED, f"the audit log {self._cfg.audit_path} cannot be written ({exc}); nothing was changed", {"check": "AUDIT_UNAVAILABLE"}) from None
            self._lost += 1
            self._why = str(exc)

    def sent(self) -> None:
        self.writes += 1

    def failed(self, cpu: int, attr: str, value: Any, exc: BaseException) -> None:
        """A write that failed (a cap, a pin, or a restore): its own ``error`` record."""
        reason = str((exc.strerror if isinstance(exc, OSError) and exc.strerror else None) or f"{type(exc).__name__}: {exc}")
        path = f"cpu{cpu}/cpufreq/{attr}"
        self.failures.append({"path": path, "written": str(value), "reason": reason[:120]})
        self._append("error", domain="cpufreq", key=attr, path=path, written=str(value), reason=reason[:200], detail=f"cpufreq write cpu{cpu} {attr} -> {value} FAILED: {reason}")

    def result(self, *, restored: bool, op: str | None = None, **fields: Any) -> str | None:
        """The closing record (op, outcome, ids, counts); returns the note to report when this or an earlier record could not be written. Resets the counters for the next stage."""
        name, n = op or self.op, self.file_count
        text = f"{name}: {n} files, " + ("restored ok" if restored else f"NOT restored ({len(self.failures)} failed write(s))")
        self._append("result", op, writes=self.writes, file_count=n, restored=restored, failures=self.failures[:20], detail=text, **fields)
        self._reset()
        lost, self._lost = self._lost, 0
        return f"{lost} audit record(s) of this run could not be written ({self._why})" if lost else None

    def cpufreq(self, files: Any) -> Any:
        return _AuditedCpufreq(files, self)

    def io(self, io: Any) -> Any:
        return _AuditedIO(io, self)


class _AuditedCpufreq:
    """``CpufreqFiles`` / ``SimCpufreq`` with ``write`` counted and a failed write recorded; everything else is the wrapped object."""

    def __init__(self, files: Any, audit: CpufreqAudit) -> None:
        self._files, self._audit = files, audit

    def __getattr__(self, name: str) -> Any:
        return getattr(self._files, name)

    def write(self, cpu: int, attr: str, value: str) -> None:
        self._audit.sent()
        try:
            self._files.write(cpu, attr, value)
        except Exception as exc:
            self._audit.failed(cpu, attr, value, exc)
            raise


class _AuditedIO:
    """The measure IO with ``write_cap`` / ``write_min`` (what :func:`~arltuner.vfdump.set_cap` and ``restore_caps`` call) counted and a failure recorded."""

    def __init__(self, io: Any, audit: CpufreqAudit) -> None:
        self._io, self._audit = io, audit

    def __getattr__(self, name: str) -> Any:
        return getattr(self._io, name)

    def _write(self, send: Callable[[int, int], None], cpu: int, attr: str, khz: int) -> None:
        self._audit.sent()
        try:
            send(cpu, khz)
        except Exception as exc:
            self._audit.failed(cpu, attr, khz, exc)
            raise

    def write_cap(self, cpu: int, khz: int) -> None:
        self._write(self._io.write_cap, cpu, CAP_ATTR, khz)

    def write_min(self, cpu: int, khz: int) -> None:
        self._write(self._io.write_min, cpu, MIN_ATTR, khz)


def audit_file_change(helper: Any, op: str, **fields: Any) -> None:
    """G-235: the ``result`` record of an op that changes a file under the helper's state directory (a curve renamed / deleted). ``OSError`` / ``OpError`` when the log cannot be written."""
    cfg = helper.cfg
    _trust(helper, cfg.audit_path, want="file", must_exist=False)
    try:
        os.close(open_checked(cfg.audit_path, write=True, create=True, mode=0o600, euid=cfg.euid_fn(), what="audit log"))
        AuditLog(cfg.audit_path).append("result", ts=cfg.wall_clock(), operator=cfg.operator, op=op, **fields)
    except (TrustError, OSError, ValueError) as exc:
        raise OpError(INTERNAL, f"{op} was done but could not be recorded in the audit log: {exc}", {"check": "AUDIT_UNAVAILABLE"}) from None


# =============================================================================================================================================
# the journal
# =============================================================================================================================================
def cpu_ranges(cpus: Sequence[int]) -> str:
    """``[0, 1, 2, 3, 8, 12, 13]`` -> ``"0\u20133, 8, 12\u201313"`` (runs of three or more compacted with an en dash)."""
    ordered = sorted(set(cpus))
    parts: list[str] = []
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        if j - i >= 2:
            parts.append(f"{ordered[i]}\u2013{ordered[j]}")
        else:
            parts.extend(str(c) for c in ordered[i:j + 1])
        i = j + 1
    return ", ".join(parts)


def _cpus_text(cpus: Sequence[int]) -> str:
    return ("cpu " if len(set(cpus)) == 1 else "cpus ") + cpu_ranges(cpus)


def epp_pinned_cpus(w: MeasureWiring, cpus: Sequence[int]) -> list[int]:
    """G-131: the CPUs whose governor is ``performance``. With intel_pstate in active mode the kernel pins EPP to ``performance`` there and refuses a write with EBUSY; that is the
    state the run wants anyway, so such a CPU's EPP is neither written nor journaled."""
    return [cpu for cpu in cpus if w.cpufreq.read(cpu, GOV_ATTR) == "performance"]


def _write_journal(
    w: MeasureWiring, cpus: Sequence[int], boot_id: str | None, skip_epp: Sequence[int] = (), touched: Sequence[str] = (CAP_ATTR, EPP_ATTR),
) -> list[dict[str, Any]]:
    """Read every value the run will touch (``touched``: the measure run passes ``TOUCHED``, the stress run its default cap + EPP) and write the journal (temp + fsync + rename)
    **before** the first change. Returns the entries. ``skip_epp``: CPUs whose EPP is not touched by the run (:func:`epp_pinned_cpus`) and so is not journaled."""
    entries: list[dict[str, Any]] = []
    context: dict[str, dict[str, str]] = {}
    for cpu in cpus:
        for attr in touched:
            if attr == EPP_ATTR and cpu in skip_epp:
                continue
            value = w.cpufreq.read(cpu, attr)
            if value is not None:
                entry: dict[str, Any] = {"cpu": cpu, "attr": attr, "value": value}
                if attr == CAP_ATTR and value == w.cpufreq.read(cpu, MAX_ATTR):
                    entry["at_max"] = True  # G-126: the user had no own cap; restore to the *current* cpuinfo max (an OC ratio apply may have raised it)
                entries.append(entry)
        context[str(cpu)] = {a: v for a in CONTEXT if a not in touched and (v := w.cpufreq.read(cpu, a)) is not None}
    if not any(e["attr"] == CAP_ATTR for e in entries):
        raise OpError(REFUSED, "cpufreq scaling_max_freq is not available for the selected CPUs (is a cpufreq driver loaded?)", {"check": "NO_CPUFREQ"})
    doc = {"version": 1, "pid": os.getpid(), "boot_id": boot_id, "created": w.wall_clock(), "entries": entries, "context": context}
    data = json.dumps(doc, separators=(",", ":")).encode("ascii")
    path = w.journal_path
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        fd = open_checked(tmp, write=True, create=True, mode=0o600, euid=w.euid, what="measure journal")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except TrustError as exc:
        raise OpError(REFUSED, f"cannot write the measurement journal: {exc}", {"check": "TRUST_PATH"}) from None
    except OSError as exc:
        raise OpError(REFUSED, f"cannot write the measurement journal {path}: {exc.strerror or exc}", {"check": "JOURNAL_UNAVAILABLE"}) from None
    return entries


def _restore(w: MeasureWiring, entries: Sequence[dict[str, Any]]) -> list[str]:
    """Write every journaled value back (only where the current value differs). A CPU's max and min go back as a **pair in the order the kernel accepts** (G-132: it refuses
    ``min > max``: the new max at or above the current min -> max first, else min first), then EPP. Returns the failures, never raises. A failure that hits several CPUs the
    same way is **one** line (G-131: never a flood of identical errors); a single failure keeps the per-CPU wording."""
    failed: dict[tuple[str, str], list[tuple[int, str]]] = {}
    by_cpu: dict[int, dict[str, dict[str, Any]]] = {}
    for e in entries:
        by_cpu.setdefault(e["cpu"], {})[e["attr"]] = e

    def number(text: str | None) -> int | None:
        return int(text) if text is not None and re.fullmatch(r"[0-9]{1,10}", text) else None

    for cpu in sorted(by_cpu):
        items = by_cpu[cpu]
        order = [CAP_ATTR, MIN_ATTR, GOV_ATTR, EPP_ATTR]
        if CAP_ATTR in items and MIN_ATTR in items:
            new_max = number(items[CAP_ATTR]["value"])
            if items[CAP_ATTR].get("at_max") is True and number(w.cpufreq.read(cpu, MAX_ATTR)) is not None:
                new_max = number(w.cpufreq.read(cpu, MAX_ATTR))
            cur_min = number(w.cpufreq.read(cpu, MIN_ATTR))
            if new_max is not None and cur_min is not None and new_max < cur_min:
                order = [MIN_ATTR, CAP_ATTR, GOV_ATTR, EPP_ATTR]
        for attr in order:
            e = items.get(attr)
            if e is None:
                continue
            value = e["value"]
            try:
                if attr == CAP_ATTR and e.get("at_max") is True:  # G-126: the journaled cap was the cpuinfo max: follow it if it moved meanwhile
                    now_max = w.cpufreq.read(cpu, MAX_ATTR)
                    if now_max is not None and re.fullmatch(r"[0-9]{1,10}", now_max):
                        value = now_max
                if w.cpufreq.read(cpu, attr) == value:
                    continue
                w.cpufreq.write(cpu, attr, value)
            except Exception as exc:
                reason = (exc.strerror if isinstance(exc, OSError) and exc.strerror else None) or str(exc)
                failed.setdefault((attr, f"{type(exc).__name__}: {reason}"), []).append((cpu, value))
    problems: list[str] = []
    for (attr, why), items in failed.items():
        if len(items) == 1:
            cpu, value = items[0]
            problems.append(f"could NOT restore {attr} of cpu {cpu} to {value!r}: {why} (restore it by hand)")
        else:
            problems.append(f"could NOT restore {attr} of {_cpus_text([c for c, _ in items])} to the journaled values: {why} (restore it by hand)")
    return problems


def _remove_journal(w: MeasureWiring) -> None:
    try:
        os.unlink(w.journal_path)
    except FileNotFoundError:
        pass


def _valid_entries(doc: Any) -> list[dict[str, Any]]:
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        raise ValueError("not a measurement journal")
    out = []
    for e in doc["entries"]:
        if (not isinstance(e, dict) or not _is_int(e.get("cpu")) or not 0 <= e["cpu"] < 1024 or e.get("attr") not in JOURNAL_ATTRS
                or not isinstance(e.get("value"), str) or len(e["value"]) > 64 or not re.fullmatch(r"[A-Za-z0-9_ .-]*", e["value"])):
            raise ValueError(f"bad journal entry {e!r}")
        item: dict[str, Any] = {"cpu": e["cpu"], "attr": e["attr"], "value": e["value"]}
        if e.get("at_max") is True and e["attr"] == CAP_ATTR:
            item["at_max"] = True
        out.append(item)
    return out


def recover_journal(w: MeasureWiring, log: Callable[[str], None], audit: CpufreqAudit | None = None, during: str | None = None) -> bool:
    """At helper start: a journal on disk is a measurement that never finished. Restore it, then remove it (only when everything was restored). Returns True if
    a journal was found. The journal is root's own file under the root-owned run directory; it is opened ``O_NOFOLLOW`` and ``fstat``-checked, and only the
    attributes a run ever touches are accepted from it. G-235: with an ``audit`` (its cpufreq wrapper is what ``w`` writes through) the restore is one ``runtime_attempt`` (the files
    and the values they go back to) and one ``result``; ``during``: the op that found the journal (``None`` at helper start)."""
    try:
        fd = open_checked(w.journal_path, write=False, create=False, euid=w.euid, what="measure journal")
    except FileNotFoundError:
        return False
    except TrustError as exc:
        log(f"measure journal {w.journal_path} is not trusted ({exc}); it was NOT applied and NOT removed")
        return True
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(1 << 20)
    try:
        entries = _valid_entries(json.loads(raw.decode("utf-8")))
    except (ValueError, UnicodeDecodeError) as exc:
        log(f"measure journal {w.journal_path} is unusable ({exc}); it was NOT applied and NOT removed")
        return True
    if audit is not None:
        audit.begin(entries, op="journal.recover", strict=False, during=during, journal=str(w.journal_path))
    problems = _restore(w, entries)
    if audit is not None:
        note = audit.result(op="journal.recover", restored=not problems, during=during, outcome="restored" if not problems else "kept")
        if note:
            log(note)
    for p in problems:
        log(p)
    if problems:
        log("measure journal kept: not everything could be restored")
    else:
        _remove_journal(w)
        log(f"stale measure journal applied: {len(entries)} cpufreq value(s) restored, journal removed")
    return True


# =============================================================================================================================================
# vf.measure
# =============================================================================================================================================
class _Cancelled(Exception):
    pass


def _validate_vf_measure(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"mode", "units", "name", "baseline"})
    mode = args.get("mode", "quick")
    if mode not in ("quick", "dense"):
        raise OpError(BAD_REQUEST, "'mode' must be \"quick\" or \"dense\"")
    units = args.get("units")
    if units is not None and (not isinstance(units, list) or not 1 <= len(units) <= 256 or not all(is_unit_id(u) for u in units) or len(set(units)) != len(units)):
        raise OpError(BAD_REQUEST, "'units' must be a non-empty list of distinct unit ids (P<n> / EC<n>) of this system")
    name = args.get("name")
    if name is not None:
        try:
            name = validate_name(name)
        except ValueError as exc:
            raise OpError(BAD_REQUEST, str(exc)) from None
    baseline = args.get("baseline", False)
    if not isinstance(baseline, bool):
        raise OpError(BAD_REQUEST, "'baseline' must be a boolean")
    return {"mode": mode, "units": None if units is None else sorted(units, key=unit_order), "name": name, "baseline": baseline}


def _wiring(helper: Any) -> MeasureWiring:
    w = getattr(helper, "measure_wiring", None)
    if w is None:
        raise OpError(INTERNAL, "this helper has no measurement support configured")
    return w


def _store_error(exc: StoreError) -> OpError:
    return OpError(exc.code, exc.message, exc.detail)


def _max_ratio(io: Any, topo: Topology, unit: str) -> int:
    """The unit's ``cpuinfo_max_freq`` (the lowest of its CPUs) in 100 MHz bins. **Last resort only**: an E-core reports one bin more than its table
    (intel_pstate N -> N-1 mapping), see :func:`_unit_maxima`."""
    return min(io.read_max_khz(cpu) for cpu in topo.unit_cpus(unit)) // 100_000


def _turbo_tables(helper: Any, run: RunEnv, topo: Topology, problems: list[str]) -> tuple[dict[str, list[int]] | None, dict[int, int]]:
    """``({"P": [8 ratios], "E": [8 ratios]} | None, {cpu: favored ratio})``. The tables come from a live plain ``rdmsr`` of ``0x1AD`` / ``0x650`` on the first P CPU (read-only,
    no mailbox), else from the last ``tune.read`` of this helper session; the favored ratios (``0x1C``, a mailbox read the measurement never does) only from that
    ``tune.read`` (``helper._tune_state``)."""
    from arltuner.guard.policy import turbo_bytes

    favored: dict[int, int] = {}
    cached: dict[str, list[int]] | None = None
    st = getattr(helper, "_tune_state", None)
    if st is not None:
        with st.lock:
            turbo, tables = st.turbo, st.tables
        if isinstance(turbo, dict) and isinstance(turbo.get("favored"), dict):
            favored = {int(c): int(r) for c, r in turbo["favored"].items() if isinstance(r, int) and r > 0}
        if isinstance(tables, dict) and tables.get("p") and tables.get("e"):
            cached = {"P": [int(r) for r in tables["p"]], "E": [int(r) for r in tables["e"]]}
    rdmsr = run.rdmsr or getattr(run.io, "read_msr", None)
    live: dict[str, list[int]] | None = None
    if rdmsr is not None:
        try:
            cpu0 = topo.cpus_of_kind("P")[0]
            live = {"P": list(turbo_bytes(rdmsr(cpu0, 0x1AD))), "E": list(turbo_bytes(rdmsr(cpu0, 0x650)))}
        except Exception as exc:
            problems.append(f"the turbo tables (0x1AD / 0x650) could not be read ({type(exc).__name__}: {exc})")
    tables_out = live or cached
    if tables_out is None or not all(tables_out.values()) or min(max(tables_out["P"]), max(tables_out["E"])) <= 0:
        problems.append("turbo tables not available: the maximum ratio of each unit comes from cpuinfo_max_freq (an E-cluster may be offered one ratio too many, 49x)")
        return None, favored
    return tables_out, favored


def _unit_maxima(io: Any, topo: Topology, units: Sequence[str], tables: dict[str, list[int]] | None, favored: dict[int, int]) -> dict[str, int]:
    """Max ratio per unit. **E** = the highest bucket of ``0x650`` (``cpuinfo_max_freq`` of an E-core says one bin more, ``[MEASURED lnx 2026-10-11]``).
    **P** = the core's favored ratio from ``tune.read`` (``0x1C`` low byte), else ``cpuinfo_max_freq`` (correct for P-cores); never above the top bucket of
    ``0x1AD``. Without tables: ``cpuinfo_max_freq``."""
    out: dict[str, int] = {}
    for unit in units:
        cpuinfo = _max_ratio(io, topo, unit)
        if unit_kind(unit) == "E":
            out[unit] = max(tables["E"]) if tables else cpuinfo
            continue
        top = favored.get(topo.unit_cpus(unit)[0]) or cpuinfo
        out[unit] = min(top, max(tables["P"])) if tables else top
    return out


def _kept_points(helper: Any, topo: Topology, unit: str) -> tuple[int, ...]:
    """The fused V/F point ratios of ``unit`` from this boot's kept read (``helper.last_words`` of its first CPU: the ratios of the point words, without the OC point when
    the fused ``0x09`` word of this session says which ratio is the top fused one); empty when there is no read."""
    words = getattr(helper, "last_words", None)
    ws = words.get(topo.unit_cpus(unit)[0]) if isinstance(words, dict) else None
    if not ws or len(ws) < 2:
        return ()
    ratios = [VfWord.from_u32(int(w) & 0xFFFFFFFF).ratio for w in ws[1:]]
    top = fused_top_ratio((getattr(helper, "fused_words", None) or {}).get(topo.unit_cpus(unit)[0]))
    ratios = [r for r in ratios if r and (top is None or r <= top)]
    return tuple(ratios) if ratios and all(b > a for a, b in zip(ratios, ratios[1:])) else ()


def _plan_ratios(top: int, points: Sequence[int], mode: str) -> tuple[tuple[int, ...], str]:
    """``(ratios, plan)``. Quick = the unit's own fused point ratios from the kept read of this boot (``plan`` "kept read") plus every ratio above the top point up to the
    unit's max turbo ``top`` (F9); without a kept read: every 5th ratio from 8 to ``top`` plus ``top`` itself (``plan`` "generic"). Dense = every ratio 8..``top``
    (``plan`` "dense")."""
    top = max(top, 8)
    if mode == "dense":
        return tuple(range(8, top + 1)), "dense"
    if points:
        return tuple(points) + tuple(range(points[-1] + 1, top + 1)), "kept read"
    return tuple(sorted({*range(8, top + 1, 5), top})), "generic"


def _classify(m: vfdump.RatioMeasurement, max_ratio: int, intermittent: Sequence[int]) -> tuple[str, str | None]:
    """``(status, reason)`` of a measured point (F3/F9). ``unreachable`` + ``max_turbo`` (the ratio is above ``cpuinfo_max_freq``), ``user_cap`` (G-132: the user's own
    ``scaling_max_freq`` is below it), ``active_cores`` (a favored core's 56/57x never got ``min_valid`` samples even with the longer sampling), ``cap_granularity`` (G-132, was
    ``cpufreq_granularity``: only stray samples at the target, the caps land on its neighbours, E 23x / P 52x). A target that was hit now and then but too rarely stays
    ``mismatch``."""
    if m.status == "ok":
        return "ok", None
    if m.status == "skipped":
        return "unreachable", "user_cap" if m.why == "user_cap" else "max_turbo"
    if m.ratio in intermittent and not (m.seen_ratios and m.seen_ratios[-1] > m.ratio):  # still below min_valid after the long sampling (never ran above)
        return "unreachable", "active_cores"
    if m.samples_kept <= max(1, m.samples_total // 5):  # a stray sample or two (the ratio wavers by a bin) is not hitting the target
        return "unreachable", "max_turbo" if m.ratio > max_ratio else "cap_granularity"
    return "mismatch", None


def _neighbours(ratio: int, seen: Sequence[int]) -> list[int | None] | None:
    """``[A, B]``: the nearest ratios below / above ``ratio`` that the caps tried did run (``None`` for a missing side); ``None`` when neither side was seen."""
    below = [r for r in seen if r < ratio]
    above = [r for r in seen if r > ratio]
    if not below and not above:
        return None
    return [max(below) if below else None, min(above) if above else None]


def _fused_point(points: Sequence[int], ratio: int, oc_ratio: int | None) -> int | str | None:
    """1-based index of ``ratio`` in the unit's fused point table (``points``, from the kept read), else ``"oc"`` for the OC ratio, else ``None`` (an in-between ratio).
    Where the OC ratio is also a fused point (a core whose OC ratio is its top point) the fused index wins."""
    fused = tuple(points)
    if ratio in fused:
        return fused.index(ratio) + 1
    return "oc" if oc_ratio is not None and ratio == oc_ratio else None


def _oc_ratio(helper: Any, topo: Topology, unit: str, top: int, points: Sequence[int]) -> int | None:
    """The unit's OC ratio: the ratio of the OC word of its first CPU in ``helper.last_words`` when non-zero, else its max ratio when that is above the top fused point."""
    words = getattr(helper, "last_words", None)
    ws = words.get(topo.unit_cpus(unit)[0]) if isinstance(words, dict) else None
    if ws:
        try:
            ratio = int(ws[0]) & 0xFF
        except (TypeError, ValueError):
            ratio = 0
        if ratio:
            return ratio
    return top if points and top > points[-1] else None


def _point(m: vfdump.RatioMeasurement, max_ratio: int = 1000, intermittent: Sequence[int] = ()) -> dict[str, Any]:
    status, reason = _classify(m, max_ratio, intermittent)
    ok = status == "ok"
    point = {
        "ratio": m.ratio, "mhz": m.ratio * 100,
        "vid_v": round(m.vid_mean, 4) if ok and m.vid_mean is not None else None,
        "vid_min": round(m.vid_min, 4) if ok and m.vid_min is not None else None,
        "vid_max": round(m.vid_max, 4) if ok and m.vid_max is not None else None,
        "kept": m.samples_kept, "total": m.samples_total, "status": status,
    }
    if reason is not None:
        point["reason"] = reason  # contract 9 addendum: only on unreachable
        if reason == "cap_granularity" and (near := _neighbours(m.ratio, m.seen_ratios)) is not None:
            point["neighbours"] = near
    return point


_AUDIT_LINE_MAX = 1024 * 1024  # a longer line is not a record this helper's guard wrote: nothing can be proven from it


def audit_shows_tuning(helper: Any) -> bool:
    """G-122: has the audit log (``cfg.audit_path``) an ``attempt`` of a ``0x11`` V/F write (the OC word or a point) under the **current** ``boot_id``?
    ``attempt`` is written before the request is sent, so a write that died midway counts (the same "may have written" rule as G-120's ``tuned_this_boot``).
    Called once when the helper starts (``Helper.__init__``): a restart then keeps ``stock`` tagging honest instead of forgetting an earlier apply of this boot.
    No audit log (or an empty one) = nothing was written = ``False``. A log that cannot be read or trusted, or whose lines it cannot parse, = ``True`` (stock
    cannot be proven). A current boot id that is unknown counts any ``0x11`` attempt at all, whatever its ``boot_id``."""
    cfg = helper.cfg
    path = cfg.audit_path
    try:
        boot = cfg.boot_id_fn()
    except Exception:
        boot = None
    boot_id = boot.strip() or None if isinstance(boot, str) else None
    try:
        _trust(helper, path, want="file", must_exist=False)
        fd = open_checked(path, write=False, create=False, euid=cfg.euid_fn(), what="audit log")
    except FileNotFoundError:
        return False
    except Exception:
        return True
    try:
        with os.fdopen(fd, "rb") as handle:
            while True:
                raw = handle.readline(_AUDIT_LINE_MAX + 1)
                if not raw:
                    return False
                partial = not raw.endswith(b"\n")
                text = raw.strip()
                if not text:
                    continue
                if len(raw) > _AUDIT_LINE_MAX:
                    return True
                try:
                    record = json.loads(text.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    if partial:
                        continue  # a write cut short at the end of the file: ignored, as the guard's read_state does
                    return True
                if not isinstance(record, dict):
                    return True
                if record.get("event") != "attempt" or "cmd" not in record:
                    continue
                try:
                    is_vf_write = int(str(record["cmd"]), 16) == 0x11
                except ValueError:
                    return True  # an attempt whose command cannot be read: it may have been a write
                if is_vf_write and (boot_id is None or record.get("boot_id") == boot_id):
                    return True
    except OSError:
        return True


def _tuning_state(helper: Any, topo: Topology) -> dict[str, Any]:
    """From the words G-112's ``tune.read`` / ``vf.dump`` read this helper session (``helper.last_words``: cpu -> list of u32 words), else ``unknown``. ``kind`` is
    ``stock`` (G-118, contract 9) when the words cover the first CPU of every unit (a complete read), **every** OC word is a neutral one (D-036: no OC point -- ratio
    at or below the core's top fused ratio from this session's ``0x09`` read, V target 0, adaptive; a core whose fused word was not read cannot be proven neutral),
    every word has offset 0 (G-120: an OC ratio or voltage raised in the OC word is not stock), and no ``tune.apply`` wrote anything this boot
    (``helper.tuned_this_boot``: a restored state is not proven stock; G-122: set at helper start from the audit log's ``0x11`` attempts of this boot, so a
    restart keeps it), else ``words``. The words travel with ``words`` only."""
    words = getattr(helper, "last_words", None)
    if not isinstance(words, dict) or not words:
        return {"kind": "unknown"}
    state: dict[str, Any] = {"kind": "words", "words": {str(cpu): [f"0x{int(w) & 0xFFFFFFFF:08x}" for w in ws] for cpu, ws in sorted(words.items())}}
    try:
        fused = getattr(helper, "fused_words", None) or {}
        complete = all(words.get(topo.unit_cpus(u)[0]) for u in topo.unit_ids())

        def neutral(cpu: int, word: int) -> bool:
            top = fused_top_ratio(fused.get(cpu))
            w = VfWord.from_u32(word & 0xFFFFFFFF)
            return top is not None and not w.static and w.ratio <= top and w.v_target_raw == 0

        cold = all(neutral(int(cpu), int(ws[0])) for cpu, ws in words.items() if ws)
        zero_offsets = all(VfWord.from_u32(int(w) & 0xFFFFFFFF).offset_raw == 0 for ws in words.values() for w in ws)
        if complete and cold and zero_offsets and not getattr(helper, "tuned_this_boot", False):
            state = {"kind": "stock"}  # contract 9: ``words`` only accompany kind "words"
    except (TypeError, ValueError, KeyError):
        pass
    return state


def _khz(text: str | None) -> int | None:
    return int(text) if text is not None and re.fullmatch(r"[0-9]{1,10}", text) else None


#: Reasons of an unmeasured V/F point that may be estimated from its neighbours (a point above the unit's reach -- ``max_turbo``, ``turbo_off``, ``user_cap`` -- never is).
INTERPOLABLE = ("cap_granularity", "cpufreq_granularity", "active_cores")


def interpolate_points(points: list[dict[str, Any]]) -> None:
    """G-132 item 8 (operator: V/F frequencies between points are interpolated by the CPU -- 54x measured exactly at the midpoint of 53x and 55x): a **V/F point** (``fused_point``
    set) that is ``unreachable`` for an :data:`INTERPOLABLE` reason and has a measured (``ok``) V/F point on **both** sides gets ``vid_v`` = the straight line between the
    nearest measured V/F points A and B, ``status "interpolated"``, ``from [A, B]`` (``exact: true`` when both neighbours have the same voltage: monotonicity makes it exact).
    The value is clamped into ``[V(A), V(B)]`` (the monotonic rule). No neighbour on one side => the point stays unreachable. Never ``ok``."""
    anchors = [p for p in points if p.get("fused_point") is not None and p["status"] == "ok" and p.get("vid_v") is not None]
    for p in points:
        if p.get("fused_point") is None or p["status"] != "unreachable" or p.get("reason") not in INTERPOLABLE:
            continue
        below = [a for a in anchors if a["ratio"] < p["ratio"]]
        above = [a for a in anchors if a["ratio"] > p["ratio"]]
        if not below or not above:
            continue
        lo, hi = max(below, key=lambda a: a["ratio"]), min(above, key=lambda a: a["ratio"])
        v_lo, v_hi = lo["vid_v"], hi["vid_v"]
        v = v_lo + (v_hi - v_lo) * (p["ratio"] - lo["ratio"]) / (hi["ratio"] - lo["ratio"])
        v = min(max(v, min(v_lo, v_hi)), max(v_lo, v_hi))
        p["status"] = "interpolated"
        p["vid_v"] = round(v, 4)
        p["from"] = [lo["ratio"], hi["ratio"]]
        if v_lo == v_hi:
            p["exact"] = True


def _retryable(point: dict[str, Any]) -> bool:
    return point["status"] == "mismatch" or (point["status"] == "unreachable" and point.get("reason") == "active_cores")


def _better(new: dict[str, Any], old: dict[str, Any]) -> bool:
    """The retry replaces the first attempt when it is ``ok`` and the first was not, or neither is ``ok`` and it kept more samples at the target."""
    if (new["status"] == "ok") != (old["status"] == "ok"):
        return new["status"] == "ok"
    return new["status"] != "ok" and new["kept"] > old["kept"]


def op_vf_measure(helper: Any, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    refuse_if_cpu_offline(helper, "vf.measure")
    w = _wiring(helper)
    topo = helper_topology(helper)
    mode, name, want_baseline = args["mode"], args["name"], args["baseline"]
    all_units = list(topo.unit_ids())
    units = all_units if args["units"] is None else args["units"]
    unknown = [u for u in units if u not in all_units]
    if unknown:
        raise OpError(BAD_REQUEST, f"'units' names {', '.join(unknown)}, which this system does not have (units: {', '.join(all_units)})")
    cancel = ctx.cancel
    try:
        w.prepare()
        run = w.make_run()
    except Exception as exc:
        raise OpError(REFUSED, f"the measurement cannot start: {type(exc).__name__}: {exc}", {"check": "MEASURE_UNAVAILABLE"}) from None
    problems: list[str] = []
    cap_notes: list[str] = []  # the engine's own restore failures: dropped, the journal restore at the end retries them and reports each failure once (aggregated)
    record: dict[str, Any] | None = None
    error: BaseException | None = None
    entries: list[dict[str, Any]] = []
    journaled = False
    cancelled = False
    audit: CpufreqAudit | None = None
    started_at = w.monotonic()
    try:
        why_not = run.io.check()
        if why_not:
            raise OpError(REFUSED, why_not, {"check": "LOAD_GENERATOR"})
        if cancel.is_set():
            raise _Cancelled()
        cpus = [cpu for u in units for cpu in topo.unit_cpus(u)]
        tables, favored = _turbo_tables(helper, run, topo, problems)
        try:
            top_of = _unit_maxima(run.io, topo, units, tables, favored)  # reads cpuinfo_max_freq (also when the tables are known) before anything else
        except (OSError, ValueError) as exc:
            raise OpError(REFUSED, f"cpufreq is not available for the selected CPUs: {exc}", {"check": "NO_CPUFREQ"}) from None
        kept = {u: _kept_points(helper, topo, u) for u in units}
        planned = {u: _plan_ratios(top_of[u], kept[u], mode) for u in units}
        plan = {u: planned[u][0] for u in units}
        sources = {planned[u][1] for u in units}
        plan_source = sources.pop() if len(sources) == 1 else "generic"  # one word for the run; mixed sources (a unit without a kept read) say so conservatively
        oc_of = {u: _oc_ratio(helper, topo, u, top_of[u], kept[u]) for u in units}
        all_core = min(tables["P"]) if tables else None  # the P table is non-increasing: its last bucket is the all-core ratio
        favored_units = {u for u in units if topo.by_cpu(topo.unit_cpus(u)[0]).favored}
        intermittent = {u: intermittent_ratios(plan[u], u in favored_units, all_core) for u in units}
        total = sum(len(r) for r in plan.values())
        # G-132 (R-001): turbo off (the power-saver profile) => nothing above the base ratio can run; those points are not attempted, they are ``unreachable`` / ``turbo_off``.
        no_turbo = False
        reader = getattr(w.cpufreq, "read_no_turbo", None)
        if reader is not None:
            try:
                no_turbo = reader() is True
            except Exception:
                no_turbo = False
        base_of: dict[str, int] = {}
        if no_turbo:
            problems.insert(0, TURBO_OFF_NOTE)
            for u in units:
                cpu0 = topo.unit_cpus(u)[0]
                khz = _khz(w.cpufreq.read(cpu0, BASE_ATTR))
                if khz is None:  # with turbo off cpuinfo_max_freq is the base (R-001 section 1)
                    khz = _khz(w.cpufreq.read(cpu0, MAX_ATTR))
                if khz is None:
                    khz = run.io.read_max_khz(cpu0)
                base_of[u] = khz // 100_000
        try:
            boot = helper.cfg.boot_id_fn()
        except Exception:
            boot = None
        now = w.wall_clock()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        record = {
            "id": new_curve_id(now), "name": name or f"{mode.capitalize()} {stamp[:16].replace('T', ' ')}", "created": stamp,
            "boot_id": boot.strip() if isinstance(boot, str) and boot.strip() else None, "mode": mode, "plan": plan_source, "complete": False, "kernel": w.kernel_fn(),
            "tuning_state": _tuning_state(helper, topo), "temps": {"min_c": None, "max_c": None}, "units": {}, "baseline": False,
        }
        audit = CpufreqAudit(helper, "vf.measure")  # G-235: proven writable before anything is changed
        w = replace(w, cpufreq=audit.cpufreq(w.cpufreq))
        run.io = audit.io(run.io)
        recover_journal(w, helper.cfg.log, audit, during="vf.measure")  # G-141: a stale journal (a run that died) is applied first -- the values read below must be the user's, never a dead run's caps
        if Path(w.journal_path).exists():
            raise OpError(REFUSED, "a stale cpufreq journal could not be restored; fix cpufreq by hand (see the helper log) before a measurement", {"check": "STALE_JOURNAL"})
        entries = _write_journal(w, cpus, record["boot_id"], touched=TOUCHED)  # before the first change
        journaled = True
        audit.begin(entries, mode=mode, units=list(units), cpus=cpu_ranges(cpus), points=total)  # G-235: the one start record, before the first cpufreq write

        def sleep(seconds: float) -> None:
            if cancel.is_set():
                raise _Cancelled()
            run.sleep(seconds)
            if cancel.is_set():
                raise _Cancelled()

        def read_perf(cpu: int) -> int:
            if cancel.is_set():
                raise _Cancelled()
            return run.read_perf(cpu)

        done = 0
        started = w.monotonic()
        per_point_s = w.settle_s + (w.samples - 1) * w.interval_s + POINT_OVERHEAD_S

        def eta() -> int:
            if done == 0:
                return round((total - done) * per_point_s)
            return round((w.monotonic() - started) / done * (total - done))

        ctx.emit({"event": "progress", "op_id": ctx.req_id, "step": 0, "of": total, "unit": None, "ratio": None, "eta_s": eta()})
        first_seen: dict[tuple[str, int], tuple[int, ...]] = {}

        def add_point(unit: str, entry: dict[str, Any], point: dict[str, Any], ratio: int) -> None:
            nonlocal done
            label = _fused_point(kept[unit], ratio, oc_of[unit])
            if label is not None:  # omitted when null: a dense run of 12 units must still fit one response line
                point["fused_point"] = label
            entry["points"].append(point)
            done += 1
            ctx.emit({"event": "vf.point", "op_id": ctx.req_id, "unit": unit, "ratio": ratio, "mhz": point["mhz"], "vid_v": point["vid_v"], "status": point["status"], **({"reason": point["reason"]} if "reason" in point else {})})
            ctx.emit({"event": "progress", "op_id": ctx.req_id, "step": done, "of": total, "unit": unit, "ratio": ratio, "eta_s": eta()})

        for unit in units:
            cpus_u = topo.unit_cpus(unit)
            entry = record["units"].setdefault(unit, {"cpus": list(cpus_u), "points": []})
            above = [r for r in plan[unit] if no_turbo and r > base_of[unit]]
            to_measure = tuple(r for r in plan[unit] if r not in above)

            def on_result(m: vfdump.RatioMeasurement, unit: str = unit, entry: dict[str, Any] = entry) -> None:
                point = _point(m, top_of[unit], intermittent[unit])
                first_seen[(unit, m.ratio)] = m.seen_ratios
                seen = [t for t in (m.temp_min_c, m.temp_max_c, record["temps"]["min_c"], record["temps"]["max_c"]) if t is not None]
                if seen:
                    record["temps"] = {"min_c": round(min(seen), 1), "max_c": round(max(seen), 1)}
                add_point(unit, entry, point, m.ratio)

            if to_measure:
                group = vfdump.MeasureGroup(unit, unit_kind(unit), tuple(cpus_u), cpus_u[0], to_measure, tuple(r for r in intermittent[unit] if r in to_measure))
                vfdump.measure_group(
                    run.io, group, read_perf, sleep, cap_notes, settle_s=w.settle_s, samples=w.samples, interval_s=w.interval_s, min_valid=MIN_VALID, on_result=on_result,
                )
            for ratio in above:  # not attempted
                add_point(unit, entry, {"ratio": ratio, "mhz": ratio * 100, "vid_v": None, "vid_min": None, "vid_max": None, "kept": 0, "total": 0,
                                        "status": "unreachable", "reason": "turbo_off"}, ratio)
        # Retry pass (G-118): a point that ended mismatch / unreachable (active_cores) gets one more attempt with the long sampling window, one unit at a time,
        # every other unit idle; the better result is the one reported (its kept / total).
        retry = [(u, p["ratio"]) for u in units for p in record["units"][u]["points"] if _retryable(p)]
        total += len(retry)
        for unit, ratio in retry:
            cpus_u = topo.unit_cpus(unit)
            ctx.emit({"event": "progress", "op_id": ctx.req_id, "step": done, "of": total, "unit": unit, "ratio": ratio, "eta_s": eta()})
            second: list[vfdump.RatioMeasurement] = []
            group = vfdump.MeasureGroup(unit, unit_kind(unit), tuple(cpus_u), cpus_u[0], (ratio,), (ratio,))  # intermittent: the long window (40 x 50 ms, early stop)
            vfdump.measure_group(run.io, group, read_perf, sleep, cap_notes, settle_s=w.settle_s, samples=w.samples, interval_s=w.interval_s, min_valid=MIN_VALID, on_result=second.append)
            done += 1
            pts = record["units"][unit]["points"]
            at = next(i for i, p in enumerate(pts) if p["ratio"] == ratio)
            old = pts[at]
            old["retried"] = True
            if second:
                new = _point(second[0], top_of[unit], intermittent[unit])
                label = _fused_point(kept[unit], ratio, oc_of[unit])
                if label is not None:
                    new["fused_point"] = label
                new["retried"] = True
                if _better(new, old):
                    pts[at] = new
                    ctx.emit({"event": "vf.point", "op_id": ctx.req_id, "unit": unit, "ratio": ratio, "mhz": new["mhz"], "vid_v": new["vid_v"], "status": new["status"], "retry": True,
                              **({"reason": new["reason"]} if "reason" in new else {})})
            ctx.emit({"event": "progress", "op_id": ctx.req_id, "step": done, "of": total, "unit": unit, "ratio": ratio, "eta_s": eta()})
        # Cap search (G-131): a point that ended unreachable / cap_granularity (the caps land on its neighbours: the kernel converts them to HWP perf units) gets one more
        # attempt with caps between the neighbours; the cap that ran the target is recorded as ``cap_khz``. Still not hit: it stays unreachable, with the nearest ratios the caps
        # did run (``neighbours``, G-132). The search is bounded (``SEARCH_BUDGET_S``) so a point no cap reaches costs about ten seconds.
        search = [(u, p["ratio"]) for u in units for p in record["units"][u]["points"] if p["status"] == "unreachable" and p.get("reason") == "cap_granularity"]
        total += len(search)
        for unit, ratio in search:
            cpus_u = topo.unit_cpus(unit)
            ctx.emit({"event": "progress", "op_id": ctx.req_id, "step": done, "of": total, "unit": unit, "ratio": ratio, "eta_s": eta()})
            group = vfdump.MeasureGroup(unit, unit_kind(unit), tuple(cpus_u), cpus_u[0], (ratio,), ())
            seen_search: set[int] = set()
            found = vfdump.search_cap(run.io, group, ratio, read_perf, sleep, cap_notes, settle_s=w.settle_s, samples=w.samples, interval_s=w.interval_s, min_valid=MIN_VALID,
                                      seen_out=seen_search, budget_s=SEARCH_BUDGET_S, clock=w.monotonic)
            done += 1
            pts = record["units"][unit]["points"]
            at = next(i for i, p in enumerate(pts) if p["ratio"] == ratio)
            pts[at]["retried"] = True
            if found is not None:
                new = _point(found, top_of[unit], intermittent[unit])
                label = _fused_point(kept[unit], ratio, oc_of[unit])
                if label is not None:
                    new["fused_point"] = label
                new["retried"] = True
                if found.cap_khz is not None:
                    new["cap_khz"] = found.cap_khz
                pts[at] = new
                ctx.emit({"event": "vf.point", "op_id": ctx.req_id, "unit": unit, "ratio": ratio, "mhz": new["mhz"], "vid_v": new["vid_v"], "status": new["status"], "retry": True,
                          **({"reason": new["reason"]} if "reason" in new else {})})
            elif (near := _neighbours(ratio, sorted(set(first_seen.get((unit, ratio), ())) | seen_search))) is not None:
                pts[at]["neighbours"] = near
            ctx.emit({"event": "progress", "op_id": ctx.req_id, "step": done, "of": total, "unit": unit, "ratio": ratio, "eta_s": eta()})
        for unit in units:  # G-132 item 8: a V/F point no cap could run, between measured V/F points, gets the straight line between them
            interpolate_points(record["units"][unit]["points"])
        record["complete"] = True
        record["baseline"] = want_baseline  # only a complete record becomes the baseline (store.save unsets the old one)
    except _Cancelled:
        cancelled = True
    except BaseException as exc:  # incl. OpError: the finally restores, then it is re-raised below
        error = exc
    finally:
        if journaled:
            problems += _restore(w, entries)
            if not any("could NOT restore" in p for p in problems):
                _remove_journal(w)
        try:
            run.close()
        except Exception as exc:
            problems.append(f"closing the MSR reader failed: {type(exc).__name__}: {exc}")
    return _finish(w, record, cancelled, error, problems, ctx, audit, w.monotonic() - started_at, units)


def _capped(problems: list[str], limit: int = 10) -> list[str]:
    """At most ``limit`` lines travel in the response; a longer list says how many were left out (G-131: never a silent cut)."""
    if len(problems) <= limit:
        return list(problems)
    return problems[:limit - 1] + [f"... and {len(problems) - limit + 1} more notes not shown"]


def _finish(
    w: MeasureWiring, record: dict[str, Any] | None, cancelled: bool, error: BaseException | None, problems: list[str], ctx: OpContext,
    audit: CpufreqAudit | None = None, duration_s: float = 0.0, units: Sequence[str] = (),
) -> dict[str, Any]:
    stored: dict[str, Any] | None = None
    store_problem: str | None = None
    has_points = record is not None and any(u["points"] for u in record["units"].values())
    if record is not None and has_points:
        try:
            stored = w.store.save(record)
        except StoreError as exc:
            store_problem = exc.message
    restore_bad = [p for p in problems if "could NOT restore" in p]
    if audit is not None:  # G-235: the run's closing record (also when it was cancelled or failed: the trail shows the caps came back); the file written is the stored curve
        outcome = "cancelled" if cancelled else "error" if error is not None else "complete"
        note = audit.result(
            restored=not restore_bad, outcome=outcome, curve_id=None if stored is None else stored["id"], units=list(units), points=sum(len(u["points"]) for u in record["units"].values()) if record is not None else 0,
            duration_s=round(duration_s, 1), **({"error": f"{type(error).__name__}: {error}"[:200]} if error is not None else {}),
        )
        if note:
            problems.append(note)
    detail: dict[str, Any] = {}
    if stored is not None:
        detail["curve_id"] = stored["id"]
        detail["partial"] = fit_record(stored, RECORD_BUDGET - 4096)  # the message and the problems share the line
    if problems:
        detail["problems"] = _capped(problems)
    note = ""
    if restore_bad:
        note += " " + " ".join(restore_bad)
    if store_problem:
        note += f" The partial record could not be stored: {store_problem}."
    if isinstance(error, OpError) and not restore_bad:
        if detail:
            error.detail = {**(error.detail or {}), **detail}
        raise error
    if error is not None:
        raise OpError(INTERNAL, f"vf.measure failed: {type(error).__name__}: {error}.{note}", detail or None)
    if cancelled:
        raise OpError(CANCELLED, "vf.measure was cancelled; cpufreq was restored, the points measured so far are stored" + (f" as {stored['id']}" if stored else "") + "." + note, detail or None)
    assert stored is not None or store_problem
    if store_problem or restore_bad:
        raise OpError(INTERNAL, f"the measurement finished but:{note}", detail or None)
    assert stored is not None
    extra = {"problems": _capped(problems)} if problems else {}
    return {"curve_id": stored["id"], "record": fit_record(stored, RECORD_BUDGET - len(json.dumps(extra))), "plan": stored.get("plan"), **extra}


# =============================================================================================================================================
# curves.* and mem.read
# =============================================================================================================================================
def _id_arg(args: dict[str, Any]) -> str:
    curve_id = args.get("id")
    if not is_curve_id(curve_id):
        raise OpError(BAD_REQUEST, "'id' must be a curve id (YYYYMMDDTHHMMSSZ-xxxx)")
    return curve_id


def _validate_curves_get(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"id", "units"})
    units = args.get("units")
    if units is not None and (not isinstance(units, list) or not units or not all(is_unit_id(u) for u in units)):
        raise OpError(BAD_REQUEST, "'units' must be a non-empty list of unit names")
    return {"id": _id_arg(args), "units": units}


def _validate_curves_update(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"id", "name", "baseline"})
    if "name" not in args and "baseline" not in args:
        raise OpError(BAD_REQUEST, "curves.update needs 'name' and/or 'baseline'")
    if "baseline" in args and not isinstance(args["baseline"], bool):
        raise OpError(BAD_REQUEST, "'baseline' must be a boolean")
    out: dict[str, Any] = {"id": _id_arg(args)}
    if "name" in args:
        try:
            out["name"] = validate_name(args["name"])
        except ValueError as exc:
            raise OpError(BAD_REQUEST, str(exc)) from None
    if "baseline" in args:
        out["baseline"] = args["baseline"]
    return out


def _validate_curves_delete(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"id"})
    return {"id": _id_arg(args)}


def op_curves_list(helper: Any, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    store = _wiring(helper).store
    try:
        rows = store.summaries()
    except StoreError as exc:
        raise _store_error(exc) from None
    for problem in store.problems:
        helper.cfg.log(f"curves.list skipped {problem}")
    reader = getattr(_wiring(helper).cpufreq, "read_no_turbo", None)
    try:
        no_turbo = reader() if reader is not None else None  # G-132: the Curves page warns before a run when turbo is off (power-saver profile)
    except Exception:
        no_turbo = None
    return {"curves": rows, "no_turbo": no_turbo if isinstance(no_turbo, bool) else None}


def op_curves_get(helper: Any, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        record = _wiring(helper).store.get(args["id"])
    except StoreError as exc:
        raise _store_error(exc) from None
    if args["units"] is not None:
        record = {**record, "units": {u: v for u, v in record["units"].items() if u in args["units"]}}
    return fit_record(record)


def op_curves_update(helper: Any, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        record = _wiring(helper).store.update(args["id"], name=args.get("name"), baseline=args.get("baseline"))
    except StoreError as exc:
        raise _store_error(exc) from None
    audit_file_change(helper, "curves.update", curve_id=args["id"], name=record.get("name"), is_baseline=record.get("baseline"))  # G-235
    return summary(record)


def op_curves_delete(helper: Any, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        _wiring(helper).store.delete(args["id"])
    except StoreError as exc:
        raise _store_error(exc) from None
    audit_file_change(helper, "curves.delete", curve_id=args["id"])  # G-235
    return {"deleted": args["id"]}


def op_mem_read(helper: Any, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    return _wiring(helper).mem_fn()


def measure_ops() -> list[OpSpec]:
    """The contract 9 ops of this module, for ``builtin_ops``. ``vf.measure`` is long (state ``measuring``) but not ``write``: no mailbox, no preflight write gate."""
    return [
        OpSpec("vf.measure", op_vf_measure, _validate_vf_measure, long=True, state=State.MEASURING),
        OpSpec("curves.list", op_curves_list, _no_args, slow=True),
        OpSpec("curves.get", op_curves_get, _validate_curves_get, slow=True),
        OpSpec("curves.update", op_curves_update, _validate_curves_update, slow=True),
        OpSpec("curves.delete", op_curves_delete, _validate_curves_delete, slow=True),
        OpSpec("mem.read", op_mem_read, _no_args, slow=True),
    ]
