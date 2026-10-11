"""G-116 ops (contract 9 addendum): ``stress.start`` (long, state ``stressing``), ``stress.stop``, ``stress.list``. **No mailbox, no MSR write.**

``stress.start`` runs ``stress-ng`` (cpu / cache screens) or ``stressapptest`` (memory screen) **as the invoking user** (``PKEXEC_UID``; dropped by ``Popen``'s own
``user`` / ``group`` / ``extra_groups`` so the setgroups / setgid / setuid happen in the child before exec, in C, with no Python between fork and exec: the task
text names ``preexec_fn``, which the Python docs call unsafe in a threaded process and the helper is threaded), in their own session / process group, with a
fixed environment and absolute, trust-checked executables. It samples the telemetry snapshot every ``progress_s`` (achieved MHz / ratio per unit, temperature,
package power), counts new MCE / hardware-error kernel lines and brackets the run with the health snapshot. An optional ratio cap goes through the **measure
journal** (``scaling_max_freq`` + EPP, journaled before the first change, restored in every exit path, stale journals recovered first and by the helper at start).

Everything with side effects is behind :class:`StressWiring` (popen, executables, trust check, invoker, telemetry, kernel log, health, meminfo, store, clocks), so
the tests and ``--sim`` run on fake binaries / the simulator model. The result never says "pass" (D-019): ``errors`` and ``health`` only.
"""
from __future__ import annotations

import os
import re
import signal
import statistics
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arltuner import vfdump
from arltuner.curves import UNITS, StoreError, unit_cpus
from arltuner.stress import (
    CHILD_ENV, CPU_METHODS, DURATION_RANGE, MEMORY_MB_MIN, STRESS_NG, STRESSAPPTEST, JournalLog, KernelLogWatch, KmsgLog, NullLog, StressappScanner,
    StressNgScanner, StressStore, clean_line, count_bad, cpu_command, memory_command,
)
from arltuner.stress.parse import MAX_ERROR_LINES

from .ops import OpContext, OpSpec, _is_int, _no_args, _only
from .ops_measure import CAP_ATTR, EPP_ATTR, _remove_journal, _restore, _write_journal, recover_journal
from .protocol import BAD_REQUEST, INTERNAL, REFUSED, OpError
from .state import State
from .trust import TrustError, check_trusted_path

__all__ = ["Invoker", "InvokerError", "SimStress", "StressWiring", "default_invoker", "device_wiring", "sim_wiring", "stress_ops"]

#: SPEC section 9.2: EPP pinned to performance for the run (the measure run uses balance_performance; this is its own value). Restored from the journal.
STRESS_EPP = "performance"
RATIO_CAP_RANGE = (8, 60)
MEM_HEADROOM_MB = 2048  # memory_mb <= MemAvailable - 2048 (the screen must not push the machine into swap / the OOM killer)
SETTLE_S = 3.0  # samples of the first seconds (ramp-up) are not part of the achieved figures
GRACE_S = 20.0  # past duration + this the helper stops a child that did not end by its own timeout
TERM_WAIT_S = 5.0
NOTE_LINES = 6  # helper notes at the head of ``error_lines``
SIM_TIME_SCALE = 1 / 12
_UNIT_NAMES = list(UNITS)
_ALL_CPUS = tuple(sorted(c for u in UNITS for c in unit_cpus(u)))
_UID_RE = re.compile(r"[0-9]{1,10}\Z")


# =============================================================================================================================================
# who the children run as
# =============================================================================================================================================
class InvokerError(Exception):
    pass


@dataclass(frozen=True)
class Invoker:
    uid: int
    gid: int
    user: str


def default_invoker(environ: dict[str, str] | None = None, getpwuid: Callable[[int], Any] | None = None) -> Invoker:
    """The user who started the GUI: ``PKEXEC_UID`` set by pkexec, never root, known to the passwd database. Anything else is :class:`InvokerError`
    (a stress child is never started as root, and never as a guessed user)."""
    env = os.environ if environ is None else environ
    raw = env.get("PKEXEC_UID")
    if raw is None or not _UID_RE.match(raw):
        raise InvokerError("PKEXEC_UID is not set to a numeric user id (the helper was not started through pkexec)")
    uid = int(raw)
    if uid == 0:
        raise InvokerError("the invoking user is root; stress children are never run as root")
    try:
        if getpwuid is None:
            import pwd

            getpwuid = pwd.getpwuid
        pw = getpwuid(uid)
    except (KeyError, ImportError, OSError):
        raise InvokerError(f"uid {uid} is not in the passwd database") from None
    if pw.pw_uid != uid or pw.pw_gid is None:
        raise InvokerError(f"the passwd entry of uid {uid} is not usable")
    return Invoker(uid, int(pw.pw_gid), str(pw.pw_name))


# =============================================================================================================================================
# wiring
# =============================================================================================================================================
@dataclass
class StressWiring:
    store: StressStore
    popen: Callable[..., Any] = subprocess.Popen
    stress_ng: str = STRESS_NG
    stressapptest: str = STRESSAPPTEST
    trust_check: Callable[[str], None] | None = None  # raises TrustError; None = no check (tests, --sim)
    invoker_fn: Callable[[], Invoker] = default_invoker
    sample_fn: Callable[[Any], dict[str, Any] | None] = lambda helper: None  # helper -> a telemetry snapshot (Sampler.collect shape) or None
    klog_factory: Callable[[], KernelLogWatch] = lambda: KernelLogWatch([NullLog()])
    health_fn: Callable[[], Any] = lambda: None  # -> arltuner.health.HealthSnapshot (raises HealthError / OSError when it cannot)
    meminfo_fn: Callable[[], int] = lambda: 0  # MemAvailable in MiB
    killpg: Callable[[int, int], object] = getattr(os, "killpg", lambda pid, sig: None)
    wall_clock: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic  # elapsed time of the run (the simulator runs it faster than real time)
    progress_s: float = 2.0  # real seconds between progress events / telemetry samples
    settle_s: float = SETTLE_S
    grace_s: float = GRACE_S
    term_wait_s: float = TERM_WAIT_S
    prepare: Callable[[], None] = lambda: None  # --sim creates its scratch directory here, lazily


def read_mem_available_mb(path: str = "/proc/meminfo") -> int:
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _telemetry_snapshot(helper: Any) -> dict[str, Any] | None:
    """The running telemetry sampler's newest snapshot (the dashboard keeps one running), else ``None``: no figures are invented."""
    tele = getattr(helper, "_tele", None)
    sampler = getattr(tele, "sampler", None)
    snap = getattr(sampler, "snapshot", None)
    if snap is None:
        return None
    try:
        value = snap()
    except Exception:
        return None
    return value if isinstance(value, dict) and isinstance(value.get("cores"), dict) else None


def device_wiring(helper: Any) -> StressWiring:
    """The root helper: installed paths only (``/var/lib/arltuner/stress``), ``/usr/bin`` tools trust-checked before every spawn, ``/dev/kmsg`` then
    ``journalctl -k``, the helper's own health snapshot."""
    from arltuner.health import take_snapshot

    from .config import INSTALLED_STATE_DIR

    lstat_fn = helper.cfg.lstat_fn

    def trust(path: str) -> None:
        check_trusted_path(path, want="file", lstat_fn=lstat_fn)

    return StressWiring(
        store=StressStore(INSTALLED_STATE_DIR / "stress", enforce_trust=helper.cfg.enforce_trust, lstat_fn=lstat_fn),
        trust_check=trust,
        sample_fn=_telemetry_snapshot,
        klog_factory=lambda: KernelLogWatch([KmsgLog(), JournalLog(trust_check=trust)]),
        health_fn=lambda: take_snapshot(Path("/")),
        meminfo_fn=read_mem_available_mb,
    )


# =============================================================================================================================================
# the simulator
# =============================================================================================================================================
class _SimChild:
    """A stand-in for the ``Popen`` of a stress tool: lives ``duration_s`` of (scaled) run time, prints a plausible success summary, stops when told."""

    def __init__(self, backend: "SimStress", pid: int, kind: str, duration_s: int, cpus: tuple[int, ...]) -> None:
        self.backend, self.pid, self.kind, self.duration_s, self.cpus = backend, pid, kind, duration_s, cpus
        self.returncode: int | None = None
        self._t0 = backend.monotonic()
        self._stopped = threading.Event()
        self.stdout = self._lines()
        backend.add_load(cpus)

    def poll(self) -> int | None:
        if self.returncode is None:
            if self._stopped.is_set():
                self._end(-signal.SIGTERM)
            elif self.backend.monotonic() - self._t0 >= self.duration_s:
                self._end(0)
        return self.returncode

    def _end(self, rc: int) -> None:
        self.returncode = rc
        self.backend.drop_load(self.cpus)

    def _lines(self):  # type: ignore[no-untyped-def]
        if self.kind == "memory":
            yield "Log: Commandline - stressapptest (simulated)"
        else:
            yield f"stress-ng: info:  [{self.pid}] dispatching hogs: {len(self.cpus)} cpu (simulated)"
        while self.poll() is None:
            time.sleep(0.02)
        if self.returncode == 0:
            if self.kind == "memory":
                yield "Stats: Found 0 hardware incidents"
                yield "Status: PASS - please verify no corrected errors"
            else:
                yield f"stress-ng: info:  [{self.pid}] failed: 0"
                yield f"stress-ng: info:  [{self.pid}] successful run completed in {self.duration_s}.00s"

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("sim-stress", timeout or 0)
            time.sleep(0.01)
        return self.returncode  # type: ignore[return-value]

    def stop(self) -> None:
        self._stopped.set()


class SimStress:
    """``--sim``: no process, no tool. Children are :class:`_SimChild`, the telemetry is the measure model's ``0x198`` ratio for the cap currently set."""

    def __init__(self, cpufreq: Any, model: Any, monotonic: Callable[[], float]) -> None:
        self.cpufreq, self.model, self.monotonic = cpufreq, model, monotonic
        self.lock = threading.Lock()
        self.loaded: set[int] = set()
        self.children: dict[int, _SimChild] = {}
        self._pid = 10_000_000

    def add_load(self, cpus: tuple[int, ...]) -> None:
        with self.lock:
            self.loaded.update(cpus)

    def drop_load(self, cpus: tuple[int, ...]) -> None:
        with self.lock:
            self.loaded.difference_update(cpus)

    def popen(self, cmd: list[str], **kwargs: Any) -> _SimChild:
        kind = "memory" if os.path.basename(cmd[0]) == "stressapptest" else "cpu"
        if kind == "memory":
            duration, cpus = int(cmd[cmd.index("-s") + 1]), _ALL_CPUS
        else:
            raw = cmd[cmd.index("--timeout") + 1]
            duration, cpus = int(raw.rstrip("s")), tuple(vfdump.parse_cpu_list(cmd[cmd.index("--taskset") + 1]))
        with self.lock:
            self._pid += 1
            pid = self._pid
        child = _SimChild(self, pid, kind, duration, cpus)
        self.children[pid] = child
        return child

    def killpg(self, pid: int, sig: int) -> None:
        child = self.children.get(pid)
        if child is None:
            raise ProcessLookupError(pid)
        child.stop()

    def sample(self, helper: Any) -> dict[str, Any]:
        def reading(value: float, unit: str) -> dict[str, Any]:
            return {"value": value, "unit": unit, "source": "sim", "status": "ok"}

        cores: dict[int, Any] = {}
        top = 8
        with self.lock:
            loaded = set(self.loaded)
        for cpu in _ALL_CPUS:
            ratio = 8
            if cpu in loaded:
                ratio = self.model.settled(self.model.unit_of(cpu), int(self.cpufreq.read(cpu, CAP_ATTR) or 0))
                top = max(top, ratio)
            cores[cpu] = {"mhz": reading(float(ratio * 100), "MHz"), "ratio": reading(float(ratio), "x")}
        return {
            "ts": time.time(), "cores": cores, "temps": {"Package": {**reading(round(34 + 30 * min(1.0, (top - 8) / 49), 1), "C"), "name": "Package", "cpu": None}},
            "power": {"package": reading(round(20 + 3.2 * (top - 8), 1) if loaded else 20.0, "W")},
        }


def sim_wiring(measure_wiring: Any, *, time_scale: float = SIM_TIME_SCALE) -> StressWiring:
    """``--sim``: the model of the measure wiring (its in-memory cpufreq), the store next to its journal (the scratch state directory, never ``/var/lib``)."""
    from arltuner.health import HealthSnapshot

    state = Path(measure_wiring.journal_path).parent
    backend = SimStress(measure_wiring.cpufreq, measure_wiring.cpufreq.model, lambda: time.monotonic() / time_scale)
    quiet = HealthSnapshot(taken_at=0.0, taint=0, taint_bits=(), pci=frozenset(), modules=frozenset(), thermal_mc=None, machinecheck=None)
    return StressWiring(
        store=StressStore(state / "stress"),
        popen=backend.popen,
        invoker_fn=lambda: Invoker(1000, 1000, "sim"),
        sample_fn=backend.sample,
        health_fn=lambda: quiet,
        meminfo_fn=lambda: 32768,
        killpg=backend.killpg,
        monotonic=backend.monotonic,
        progress_s=1.0,
        settle_s=0.0,
        prepare=lambda: state.mkdir(mode=0o700, parents=True, exist_ok=True),
    )


_wiring_lock = threading.Lock()


def _wiring(helper: Any) -> StressWiring:
    """``helper.stress_wiring`` when set (tests), else built once from what the helper has: the simulator model with ``--sim``, the installed paths otherwise."""
    with _wiring_lock:
        w = getattr(helper, "stress_wiring", None)
        if w is None:
            if helper.cfg.simulator:
                mw = getattr(helper, "measure_wiring", None)
                if mw is None or not hasattr(mw.cpufreq, "model"):
                    raise OpError(INTERNAL, "this helper has no stress support configured")
                w = sim_wiring(mw)
            else:
                w = device_wiring(helper)
            helper.stress_wiring = w
        return w


# =============================================================================================================================================
# argument validation
# =============================================================================================================================================
def _validate_stress_start(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"kind", "method", "units", "ratio_cap", "duration_s", "memory_mb"})
    kind = args.get("kind")
    if kind not in ("cpu", "memory"):
        raise OpError(BAD_REQUEST, "'kind' must be \"cpu\" or \"memory\"")
    lo, hi = DURATION_RANGE
    duration = args.get("duration_s")
    if not _is_int(duration) or not lo <= duration <= hi:
        raise OpError(BAD_REQUEST, f"'duration_s' must be an integer {lo}..{hi}")
    cap = args.get("ratio_cap")
    if cap is not None and (not _is_int(cap) or not RATIO_CAP_RANGE[0] <= cap <= RATIO_CAP_RANGE[1]):
        raise OpError(BAD_REQUEST, f"'ratio_cap' must be null or an integer {RATIO_CAP_RANGE[0]}..{RATIO_CAP_RANGE[1]}")
    method, units, memory_mb = args.get("method"), args.get("units"), args.get("memory_mb")
    if kind == "memory":
        if method is not None or units is not None:
            raise OpError(BAD_REQUEST, "'method' and 'units' belong to kind \"cpu\"")
        if memory_mb is not None and (not _is_int(memory_mb) or memory_mb < MEMORY_MB_MIN):
            raise OpError(BAD_REQUEST, f"'memory_mb' must be an integer >= {MEMORY_MB_MIN}")
        units = list(_UNIT_NAMES)
    else:
        if memory_mb is not None:
            raise OpError(BAD_REQUEST, "'memory_mb' belongs to kind \"memory\"")
        method = "integer" if method is None else method
        if method not in CPU_METHODS:
            raise OpError(BAD_REQUEST, f"'method' must be one of {', '.join(CPU_METHODS)}")
        if units is None:
            units = list(_UNIT_NAMES)
        elif not isinstance(units, list) or not 1 <= len(units) <= len(UNITS) or not all(isinstance(u, str) and u in UNITS for u in units) or len(set(units)) != len(units):
            raise OpError(BAD_REQUEST, f"'units' must be a non-empty list of distinct units from {', '.join(_UNIT_NAMES)}")
        units = [u for u in _UNIT_NAMES if u in units]
    return {"kind": kind, "method": method, "units": units, "ratio_cap": cap, "duration_s": duration, "memory_mb": memory_mb}


# =============================================================================================================================================
# telemetry aggregation
# =============================================================================================================================================
def _num(reading: Any) -> float | None:
    if isinstance(reading, dict) and reading.get("status") == "ok":
        value = reading.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value:
            return float(value)
    return None


class _Collector:
    def __init__(self, units: list[str]) -> None:
        self.units = units
        self.mhz: dict[str, list[float]] = {u: [] for u in units}
        self.ratios: dict[str, list[int]] = {u: [] for u in units}
        self.max_temp: float | None = None
        self.max_w: float | None = None
        self._last_ts: Any = object()

    def add(self, snap: dict[str, Any] | None, settled: bool) -> None:
        if not snap or not isinstance(snap.get("cores"), dict):
            return
        ts = snap.get("ts")
        if ts is not None and ts == self._last_ts:
            return  # the same sample twice
        self._last_ts = ts
        cores = snap["cores"]
        if settled:
            for unit in self.units:
                for cpu in unit_cpus(unit):
                    core = cores.get(cpu) or cores.get(str(cpu))
                    if not isinstance(core, dict):
                        continue
                    mhz, ratio = _num(core.get("mhz")), _num(core.get("ratio"))
                    if mhz is not None:
                        self.mhz[unit].append(mhz)
                    if ratio is not None:
                        self.ratios[unit].append(int(round(ratio)))
        temps = snap.get("temps")
        if isinstance(temps, dict):
            for reading in temps.values():
                t = _num(reading)
                if t is not None and (self.max_temp is None or t > self.max_temp):
                    self.max_temp = t
        power = snap.get("power")
        w = _num(power.get("package")) if isinstance(power, dict) else None
        if w is not None and (self.max_w is None or w > self.max_w):
            self.max_w = w

    def achieved(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for unit in self.units:
            if not self.mhz[unit] and not self.ratios[unit]:
                continue
            out[unit] = {
                "mhz_mean": round(statistics.fmean(self.mhz[unit]), 1) if self.mhz[unit] else None,
                "ratio_mode": statistics.mode(self.ratios[unit]) if self.ratios[unit] else None,
            }
        return out


def _cap_valid(cap: int | None, achieved: dict[str, dict[str, Any]], unit_max: dict[str, int]) -> bool | None:
    """SPEC section 9.2: the run is valid when every unit settled within one bin of the cap (or of what the unit can reach, when that is lower)."""
    if cap is None:
        return None
    seen = [(u, a["ratio_mode"]) for u, a in achieved.items() if a.get("ratio_mode") is not None]
    if not seen:
        return None
    return all(abs(mode - min(cap, unit_max.get(u, cap))) <= 1 for u, mode in seen)


# =============================================================================================================================================
# stress.start
# =============================================================================================================================================
def _trusted_exe(w: StressWiring, path: str, package: str) -> str:
    real = os.path.realpath(path)
    if not os.path.isfile(real):
        raise OpError(REFUSED, f"{path} is not installed (install the {package} package)", {"check": "TOOL_MISSING"})
    if w.trust_check is not None:
        try:
            w.trust_check(real)
        except TrustError as exc:
            raise OpError(REFUSED, f"untrusted program (SPEC 4.4): {exc}", {"check": "TRUST_PATH"}) from None
    return real


def _health(w: StressWiring) -> Any:
    try:
        return w.health_fn()
    except Exception:
        return None


def _stop_child(w: StressWiring, proc: Any, reader: threading.Thread | None) -> None:
    """TERM the child's whole process group, KILL it if it does not go (or if something in the group still holds the output pipe); reap."""
    if proc.poll() is None:
        try:
            w.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            pass
        try:
            proc.wait(timeout=w.term_wait_s)
        except subprocess.TimeoutExpired:
            try:
                w.killpg(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:
                pass
            try:
                proc.wait(timeout=TERM_WAIT_S)
            except subprocess.TimeoutExpired:
                pass
    if reader is not None:
        reader.join(1.0)
        if reader.is_alive():  # a grandchild outlived the leader and keeps the pipe: the group still exists, so its id is still ours
            try:
                w.killpg(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:
                pass
            reader.join(3.0)
    stream = getattr(proc, "stdout", None)
    if stream is not None and (reader is None or not reader.is_alive()):
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def op_stress_start(helper: Any, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    w = _wiring(helper)
    cancel = ctx.cancel
    kind, method, units, cap = args["kind"], args["method"], args["units"], args["ratio_cap"]
    duration = args["duration_s"]
    cpus = sorted({c for u in units for c in unit_cpus(u)})
    # ---- everything that can refuse, before anything is changed
    try:
        w.prepare()
        invoker = w.invoker_fn()
    except InvokerError as exc:
        raise OpError(REFUSED, f"the stress child cannot be started: {exc}", {"check": "NO_INVOKER"}) from None
    memory_mb = args["memory_mb"]
    if kind == "memory":
        avail = w.meminfo_fn()
        top = avail - MEM_HEADROOM_MB
        if top < MEMORY_MB_MIN:
            raise OpError(REFUSED, f"only {avail} MiB of memory are available; the screen needs at least {MEMORY_MB_MIN + MEM_HEADROOM_MB} MiB free", {"check": "MEM_RANGE"})
        if memory_mb is None:
            memory_mb = max(MEMORY_MB_MIN, top * 3 // 4)
        elif memory_mb > top:
            raise OpError(BAD_REQUEST, f"'memory_mb' must be {MEMORY_MB_MIN}..{top} (MemAvailable {avail} MiB minus {MEM_HEADROOM_MB} MiB)", {"check": "MEM_RANGE"})
    mw = getattr(helper, "measure_wiring", None)
    if cap is not None and mw is None:
        raise OpError(REFUSED, "a ratio cap needs the cpufreq journal of the measurement support, which this helper does not have", {"check": "NO_CPUFREQ"})
    exe = _trusted_exe(w, w.stress_ng if kind == "cpu" else w.stressapptest, "stress-ng" if kind == "cpu" else "stressapptest")
    cmd = cpu_command(exe, method, cpus, duration) if kind == "cpu" else memory_command(exe, duration, memory_mb)
    if cap is not None:
        recover_journal(mw, helper.cfg.log)  # a stale journal (a run that died) is applied first; if it cannot be, nothing new is started
        if Path(mw.journal_path).exists():
            raise OpError(REFUSED, "a stale cpufreq journal could not be restored; fix cpufreq by hand (see the helper log) before a capped run", {"check": "STALE_JOURNAL"})
    unit_max: dict[str, int] = {}
    if cap is not None:
        for u in units:
            raw = mw.cpufreq.read(unit_cpus(u)[0], "cpuinfo_max_freq")
            if raw and raw.isdigit():
                unit_max[u] = int(raw) // 100_000
    scanner = StressNgScanner() if kind == "cpu" else StressappScanner()
    collector = _Collector(units)
    watch = w.klog_factory()
    notes: list[str] = []
    started_wall = w.wall_clock()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_wall))
    watch.begin()
    before = _health(w)

    entries: list[dict[str, Any]] = []
    journaled = False
    proc: Any = None
    reader: threading.Thread | None = None
    cancelled = timed_out = False
    error: BaseException | None = None
    elapsed = 0.0
    restore_problems: list[str] = []
    t0 = w.monotonic()
    try:
        if cap is not None:
            try:
                boot = helper.cfg.boot_id_fn()
            except Exception:
                boot = None
            entries = _write_journal(mw, cpus, boot.strip() if isinstance(boot, str) and boot.strip() else None)  # before the first change
            journaled = True
            for cpu in cpus:
                try:
                    mw.cpufreq.write(cpu, CAP_ATTR, str(cap * 100_000))
                except OSError as exc:
                    raise OpError(REFUSED, f"cannot cap cpu {cpu} (scaling_max_freq): {exc.strerror or exc}", {"check": "CAP_WRITE"}) from None
                if any(e["cpu"] == cpu and e["attr"] == EPP_ATTR for e in entries):
                    try:
                        mw.cpufreq.write(cpu, EPP_ATTR, STRESS_EPP)
                    except OSError as exc:
                        notes.append(f"helper: energy_performance_preference of cpu {cpu} was not set ({exc.strerror or exc}); the run continues")
        if cancel.is_set():
            cancelled = True
        else:
            exe = _trusted_exe(w, exe, "stress-ng" if kind == "cpu" else "stressapptest")  # again, right before the spawn
            cmd[0] = exe
            try:
                proc = w.popen(
                    cmd, cwd="/", env=dict(CHILD_ENV), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True, shell=False,
                    start_new_session=True, user=invoker.uid, group=invoker.gid, extra_groups=[], text=True, encoding="utf-8", errors="replace", bufsize=1,
                )
            except OSError as exc:
                raise OpError(REFUSED, f"{os.path.basename(exe)} could not be started: {exc.strerror or exc}", {"check": "SPAWN"}) from None
            t0 = w.monotonic()

            def drain(stream: Any) -> None:
                try:
                    for line in stream:
                        scanner.feed(line)
                except (OSError, ValueError):
                    pass

            reader = threading.Thread(target=drain, args=(proc.stdout,), name="stress-output", daemon=True)
            reader.start()
            next_tick = 0.0
            while True:
                if cancel.is_set():
                    cancelled = True
                    break
                if proc.poll() is not None:
                    break
                elapsed = w.monotonic() - t0
                if elapsed > duration + w.grace_s:
                    timed_out = True
                    break
                now = time.monotonic()
                if now >= next_tick:
                    next_tick = now + w.progress_s
                    try:
                        collector.add(w.sample_fn(helper), elapsed >= w.settle_s)
                    except Exception as exc:  # telemetry trouble must not end the screen
                        notes.append(f"helper: telemetry sample failed: {type(exc).__name__}: {exc}")
                    ctx.emit({"event": "progress", "op_id": ctx.req_id, "elapsed_s": round(elapsed, 1), "of_s": duration, "errors": scanner.errors})
                cancel.wait(min(0.05, w.progress_s))
    except BaseException as exc:  # incl. OpError: the finally cleans up, then it is re-raised below
        error = exc
    finally:
        if proc is not None:
            try:
                _stop_child(w, proc, reader)
            except BaseException as exc:
                notes.append(f"helper: stopping the stress process failed: {type(exc).__name__}: {exc}")
        elapsed = max(elapsed, w.monotonic() - t0) if proc is not None else elapsed
        if journaled:
            restore_problems = _restore(mw, entries)
            if not restore_problems:
                _remove_journal(mw)
    if error is not None:
        if isinstance(error, OpError) and not restore_problems:
            raise error
        if isinstance(error, OpError):
            raise OpError(error.code, f"{error.message} {' '.join(restore_problems)}", error.detail)
        raise OpError(INTERNAL, f"stress.start failed: {type(error).__name__}: {error}. {' '.join(restore_problems)}".strip()) from error
    if proc is not None:
        try:
            collector.add(w.sample_fn(helper), True)
        except Exception:
            pass
    ended_wall = w.wall_clock()
    notes += [f"helper: {p}" for p in restore_problems]

    # ---- the verdict of the tool, the health bracket
    rc = proc.returncode if proc is not None else None
    verdict = scanner.finish(rc)
    if proc is not None and not cancelled and not verdict.ok and verdict.errors == 0:
        notes.append(f"helper: the stress program ended abnormally (exit code {rc})" if not timed_out else "helper: the stress program did not end by its own timeout and was stopped")
    klog_lines = watch.new_lines()
    mce_new = count_bad(klog_lines)
    clean = True
    if not watch.available:
        clean = False
        notes.append(f"helper: the kernel log could not be read ({watch.why}); new MCE lines are not established")
    if mce_new:
        clean = False
        notes.append(f"helper: {mce_new} new MCE / hardware-error kernel line(s)")
        notes += ["kernel: " + clean_line(line) for line in [x for x in klog_lines if x][:3] if count_bad([line])]
    after = _health(w)
    if before is None or after is None:
        clean = False
        notes.append("helper: the health snapshot could not be taken; health is not established")
    else:
        try:
            from arltuner.health import diff

            delta = diff(before, after)
            if not delta.clean:
                clean = False
                notes.append("helper: health " + delta.summary)
        except Exception as exc:
            clean = False
            notes.append(f"helper: health compare failed: {type(exc).__name__}: {exc}")

    achieved = collector.achieved()
    stopped_by = "cancel" if cancelled else ("error" if (verdict.errors or not verdict.ok or timed_out) else "time")
    result: dict[str, Any] = {
        "kind": kind, "started": stamp, "ended": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ended_wall)), "duration_s": duration,
        "elapsed_s": round(elapsed, 1), "stopped_by": stopped_by, "errors": verdict.errors,
        "error_lines": ([clean_line(n) for n in notes[:NOTE_LINES]] + verdict.error_lines)[:MAX_ERROR_LINES],
        "achieved": achieved, "cap_valid": _cap_valid(cap, achieved, unit_max),
        "max_temp_c": None if collector.max_temp is None else round(collector.max_temp, 1),
        "max_pkg_w": None if collector.max_w is None else round(collector.max_w, 1),
        "mce_new": mce_new, "health": "clean" if clean else "delta",
    }
    record = {**result, "method": method, "units": units, "ratio_cap": cap, "memory_mb": memory_mb, "command": cmd[1:], "returncode": rc}
    try:
        saved = w.store.save(record, now=started_wall)
        path: str | None = str(w.store.path_of(saved["id"]))
        rid = saved["id"]
    except StoreError as exc:
        rid, path = None, None
        result["error_lines"] = (result["error_lines"] + [clean_line(f"helper: the result could not be stored: {exc.message}")])[:MAX_ERROR_LINES]
    if restore_problems:  # the run's cpufreq changes are still in force: not a success, whatever the screen found
        raise OpError(INTERNAL, "the stress run ended but " + " ".join(restore_problems), {"check": "RESTORE_FAILED"})
    return {"id": rid, **result, "record_path": path}


# =============================================================================================================================================
# stress.stop, stress.list
# =============================================================================================================================================
def op_stress_stop(helper: Any, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    running = helper.machine.running
    if running is None or running.op != "stress.start":
        return {"cancelled": False, "state": helper.machine.state.value}
    return helper.cancel_running(running.op_id)


def op_stress_list(helper: Any, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    store = _wiring(helper).store
    try:
        rows = store.summaries()
    except StoreError as exc:
        raise OpError(exc.code, exc.message, exc.detail) from None
    for problem in store.problems:
        helper.cfg.log(f"stress.list skipped {problem}")
    return {"results": rows}


def stress_ops() -> list[OpSpec]:
    """The contract 9 (G-116) ops, for ``builtin_ops``. ``stress.start`` is long (state ``stressing``) but not ``write``: no mailbox, no preflight write gate."""
    return [
        OpSpec("stress.start", op_stress_start, _validate_stress_start, long=True, state=State.STRESSING),
        OpSpec("stress.stop", op_stress_stop, _no_args),
        OpSpec("stress.list", op_stress_list, _no_args, slow=True),
    ]
