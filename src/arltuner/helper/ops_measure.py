"""G-111 ops (contract 9): ``vf.measure`` (long, state ``measuring``), ``curves.list/get/update/delete``, ``mem.read``. **No mailbox, no MSR write.**

``vf.measure`` runs the T-026/T-027 engine (:func:`arltuner.vfdump.measure_group`: cap search, at-target filter, ``MISMATCH``) for the selected tuning units:
cap the unit's CPUs through cpufreq ``scaling_max_freq``, load **all CPUs of the unit** with pinned busy loops, settle, sample MSR ``0x198`` (requested VID =
``[47:32]/8192``, running ratio ``[15:8]``) through a **read-only** MSR port, keep the samples at the target ratio. The only things written are cpufreq sysfs
values, and every one of them is **journaled before the first change** (``/run/arltuner/measure-journal.json``, root 0600) and restored on success, cancel,
error and at the next helper start if the journal is stale (the helper died mid-run). The load generators carry their own deadline, so a dead helper cannot
leave them spinning.

Everything with side effects is behind :class:`MeasureWiring` / :class:`RunEnv` (cpufreq files, MSR read, load, sleep, store, memory reader), so the tests and
``--sim`` run on fake sysfs and the :class:`SimMeasureIO` model.
"""
from __future__ import annotations

import json
import os
import random
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arltuner import vfdump
from arltuner.curves import UNITS, CurveStore, StoreError, fit_record, is_curve_id, new_curve_id, summary, unit_cpus, unit_kind, validate_name
from arltuner.curves.record import QUICK_RATIOS, RECORD_BUDGET
from arltuner.msr.measured_baseline import VF_DOMAIN0
from arltuner.platform import default_platform
from arltuner.mailbox.vf import VfWord
from arltuner.vfmodel import complete_baseline, effective_curve, voltage_at

from .ops import OpContext, OpSpec, _is_int, _no_args, _only
from .protocol import BAD_REQUEST, CANCELLED, INTERNAL, REFUSED, OpError
from .state import State
from .trust import TrustError, check_trusted_path, open_checked

__all__ = [
    "HelperMeasureIO", "MeasureWiring", "RunEnv", "SimCpufreq", "SimMeasureIO", "SimModel", "CpufreqFiles", "device_wiring", "measure_ops", "recover_journal", "sim_wiring",
]

# ---- measurement constants (SPEC-v3 section 3; the T-027 defaults are 2.5 s settle / 30 samples, the helper's run is shorter on purpose) -------------------------
SETTLE_S = 0.3
SAMPLES = 10
SAMPLE_INTERVAL_S = 0.1
MIN_VALID = 5  # of SAMPLES: half must run at the target ratio (the CLI needs 5 of 30; a knob to tune on the machine, T-027 saw the ratio waver under load)
POINT_OVERHEAD_S = 0.1  # per attempt: cap writes + process start (only for the first ETA, before any point has been timed)
EPP_VALUE = "balance_performance"
EPP_ATTR = "energy_performance_preference"
CAP_ATTR = "scaling_max_freq"
MIN_ATTR = "scaling_min_freq"
GOV_ATTR = "scaling_governor"
#: What the run changes (journaled, restored); min freq and governor are only recorded as context, never touched, never restored.
TOUCHED = (CAP_ATTR, EPP_ATTR)
CONTEXT = (MIN_ATTR, GOV_ATTR)
_ATTR_ORDER = {a: i for i, a in enumerate((CAP_ATTR, MIN_ATTR, GOV_ATTR, EPP_ATTR))}  # restore: max first (a min above a still-lowered max would be EINVAL)
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
_NAME_UNITS = list(UNITS)


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


class SimCpufreq:
    """In-memory stand-in for :class:`CpufreqFiles` (the ``--sim`` helper): same ``read`` / ``write`` / ``path``-less surface."""

    def __init__(self, model: "SimModel | None" = None) -> None:
        self.model = model or SimModel()
        self.values: dict[tuple[int, str], str] = {}
        self.lock = threading.Lock()
        for cpu in range(24):
            self.values[(cpu, CAP_ATTR)] = str(self.model.max_khz(cpu))
            self.values[(cpu, "cpuinfo_max_freq")] = str(self.model.max_khz(cpu))
            self.values[(cpu, MIN_ATTR)] = "800000"
            self.values[(cpu, GOV_ATTR)] = "powersave"
            self.values[(cpu, EPP_ATTR)] = "balance_power"

    def read(self, cpu: int, attr: str) -> str | None:
        with self.lock:
            return self.values.get((cpu, attr))

    def write(self, cpu: int, attr: str, value: str) -> None:
        with self.lock:
            if (cpu, attr) not in self.values:
                raise FileNotFoundError(attr)
            self.values[(cpu, attr)] = str(value)


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


# =============================================================================================================================================
# the simulator model
# =============================================================================================================================================
#: First-run STOCK V/F curve, requested VID in volts per fused point ``[MEASURED lnx 2026-10-11 18:17 UTC]`` (``tuning-results.md`` "Stock V/F curve, measured";
#: ``None`` = never settled: P0/P3 57x intermittent, E 23x not pinnable, non-favored P 56/57x unreachable). P0 57x 1.4288 V and P3 57x 1.3964 V are the
#: orchestrator's longer single-core samples of the same day (fused ``0x09``: 1.4258 / 1.3945). The simulator's ``vid`` is this curve through
#: :mod:`arltuner.vfmodel` (offsets, F4 clamp, interpolation), not an invented formula.
STOCK_VID: dict[str, dict[int, float | None]] = {
    "P0": {8: 0.693, 20: 0.721, 30: 0.811, 48: 1.041, 53: 1.216, 55: 1.299, 56: 1.386, 57: 1.4288},
    "P1": {8: 0.691, 20: 0.723, 30: 0.806, 48: 1.041, 53: 1.221, 55: 1.306},
    "P2": {8: 0.691, 20: 0.721, 30: 0.821, 48: 1.073, 53: 1.271, 55: 1.366},
    "P3": {8: 0.691, 20: 0.721, 30: 0.821, 48: 1.061, 53: 1.221, 55: 1.299, 56: 1.359, 57: 1.3964},
    "P4": {8: 0.688, 20: 0.713, 30: 0.804, 48: 1.033, 53: 1.226, 55: 1.319},
    "P5": {8: 0.690, 20: 0.721, 30: 0.811, 48: 1.054, 53: 1.261, 55: 1.359},
    "P6": {8: 0.691, 20: 0.721, 30: 0.805, 48: 1.033, 53: 1.249, 55: 1.346},
    "P7": {8: 0.690, 20: 0.719, 30: 0.811, 48: 1.041, 53: 1.271, 55: 1.376},
    "EC0": {8: 0.775, 15: 0.775, 30: 0.775, 40: 0.970, 46: 1.157},
    "EC1": {8: 0.775, 15: 0.775, 30: 0.775, 40: 0.970, 46: 1.137},
    "EC2": {8: 0.775, 15: 0.775, 30: 0.775, 40: 0.963, 46: 1.137},
    "EC3": {8: 0.775, 15: 0.775, 30: 0.775, 40: 0.963, 46: 1.137},
}
_FAVORED_UNITS = frozenset(f"P{i}" for i in default_platform().topology.favored_p)  # platforms/arl-s-285k.toml: P0, P3
_P_ALL_CORE_RATIO = (default_platform().turbo_defaults[0x1AD] >> 56) & 0xFF  # 55: the 0x1AD bucket for 8 active cores [MEASURED lnx 2026-09-29]


def intermittent_ratios(unit: str, points: Sequence[int]) -> tuple[int, ...]:
    """Ratios of ``unit`` that only a few active cores allow (F3): the favored P-cores above the all-core bucket (56, 57x)."""
    return tuple(r for r in points if unit in _FAVORED_UNITS and r > _P_ALL_CORE_RATIO)


class SimModel:
    """A 285K for the simulator: ``vid(unit, ratio)`` = the measured stock curve (:data:`STOCK_VID`) through the F4 clamp / interpolation of
    :mod:`arltuner.vfmodel` (``offsets_mv[unit] = {point ratio: mV}`` moves it), what ratio a cap settles at, jitter. Deterministic (seeded per unit / ratio / sample).

    Reach (F3): favored P-cores 57x, other P-cores 55x (``cpuinfo_max_freq`` 5.5 GHz, ``[MEASURED lnx 2026-10-11]``), E-clusters 48x (turbo table ``0x650``) while
    their ``cpuinfo_max_freq`` says 4.9 GHz (intel_pstate N -> N-1 mapping, ``[MEASURED lnx 2026-10-11 19:06 UTC]``): the 49x it implies is not real (G-118). **A favored core only holds 56/57x while at most one other CPU is busy** (measured: a pinned
    spinner on CPU 0 spent 31 of 40 samples at 57x, the rest at 55/51/56x as soon as a 3rd core woke): about 70 % of the samples at 57x when quiet, none when
    ``busy_others >= 2``. cpufreq granularity (F9): E 23x and P 52x are not pinnable (the caps land on the neighbours). E-cores also settle one bin low when the cap is in
    16..29 (the intel_pstate quirk of T-027, so the cap search retries ``ratio + 1``)."""

    P_REACH_FAVORED, P_REACH, E_REACH = 57, 55, 48
    E_CPUINFO = 49  # cpuinfo_max_freq / 100 MHz of an E-core [MEASURED lnx 2026-10-11 19:06 UTC]
    E_QUIRK = range(16, 30)
    QUIET_SHARE = 0.7  # share of samples a favored core spends at 56/57x when no other core is busy (measured 31/40 at 57x on CPU 0)

    def __init__(self, seed: int = 285, background_busy: int = 0) -> None:
        self.seed = seed
        self.background_busy = background_busy  # busy CPUs outside the run (a desktop session, the helper): >= 2 takes the favored 57x away
        spread = random.Random(seed)
        self.spread = {u: round(spread.uniform(-0.025, 0.025), 4) for u in UNITS}
        self.offsets_mv: dict[str, dict[int, float]] = {}
        self._base: dict[str, dict[int, float | None]] = {}

    @staticmethod
    def unit_of(cpu: int) -> str:
        return f"P{cpu}" if cpu < 8 else f"EC{(cpu - 8) // 4}"

    def max_khz(self, cpu: int) -> int:
        if cpu >= 8:
            return self.E_CPUINFO * 100_000
        return (self.P_REACH_FAVORED if cpu in (0, 3) else self.P_REACH) * 100_000

    def reach(self, unit: str) -> int:
        return self.E_REACH if unit.startswith("E") else (self.P_REACH_FAVORED if unit in _FAVORED_UNITS else self.P_REACH)

    def points(self, unit: str) -> tuple[int, ...]:
        return QUICK_RATIOS[unit_kind(unit)]

    def _baseline(self, unit: str) -> dict[int, float | None]:
        if unit not in self._base:
            fused_top = None
            if unit.startswith("P"):  # the unmeasured 57x of a non-favored core: its fused 0x09 voltage (F7)
                word = default_platform().fused_point[int(unit[1:])]
                fused_top = ((word >> 8) & 0xFFF) / 1024
            self._base[unit] = complete_baseline(self.points(unit), STOCK_VID[unit], fused_top)
        return self._base[unit]

    def vid(self, unit: str, ratio: int) -> float:
        curve = effective_curve(self.points(unit), self._baseline(unit), self.offsets_mv.get(unit, {}))
        v = voltage_at(curve, ratio)
        assert v is not None
        return v

    def temp_c(self, unit: str, ratio: int) -> float:
        return round(34 + 30 * min(1.0, (ratio - 8) / 49) + self.spread[unit] * 60, 1)

    def settled(self, unit: str, cap_khz: int) -> int:
        """The ratio the unit settles at under load with ``scaling_max_freq = cap_khz`` (quiet system)."""
        cap_ratio = cap_khz // 100_000
        if unit.startswith("E") and cap_ratio in self.E_QUIRK and cap_ratio != 24:  # 24 is not low: with 23 it makes 23x unpinnable
            cap_ratio -= 1
        if unit.startswith("P") and cap_ratio == 52:  # P 52x is not pinnable either: the cap lands on 51 (cap 53 gives 53)
            cap_ratio = 51
        return max(8, min(cap_ratio, self.reach(unit)))

    def word(self, cpu: int, cap_khz: int, loaded: bool, n: int, busy_others: int = 0) -> int:
        """The ``0x198`` word of sample ``n``: running ratio ``[15:8]`` (one bin low now and then, as the real ratio wavers; a favored core at 56/57x
        falls to 55x/51x for most samples when other cores are busy), VID ``[47:32]`` in 1/8192 V."""
        unit = self.unit_of(cpu)
        ratio = self.settled(unit, cap_khz) if loaded else 8
        rng = random.Random(f"{self.seed}/{unit}/{ratio}/{n}")
        if loaded and unit in _FAVORED_UNITS and ratio > _P_ALL_CORE_RATIO:
            share = 0.0 if busy_others + self.background_busy >= 2 else self.QUIET_SHARE
            if rng.random() >= share:
                ratio = _P_ALL_CORE_RATIO if rng.random() < 0.8 else 51
        elif rng.random() < 0.08 and ratio > 8:
            ratio -= 1
        vid = self.vid(unit, ratio) + rng.uniform(-0.0015, 0.0015)
        return (round(vid * 8192) << 32) | (ratio << 8)


class _SimLoad:
    def __init__(self, io: "SimMeasureIO", cpus: Sequence[int]) -> None:
        self._io, self._cpus = io, tuple(cpus)
        with io.lock:
            io.loaded.update(self._cpus)

    def stop(self) -> None:
        with self._io.lock:
            self._io.loaded.difference_update(self._cpus)


class SimMeasureIO:
    """:class:`~arltuner.vfdump.MeasureIO` over a cpufreq store (:class:`SimCpufreq` or a fake-sysfs :class:`CpufreqFiles`) and the :class:`SimModel` instead of
    hardware: ``read_perf`` gives the model's ``0x198`` word for the cap currently set and whether the CPU is loaded."""

    def __init__(self, cpufreq: Any, model: SimModel | None = None) -> None:
        self.cpufreq, self.model = cpufreq, model or SimModel()
        self.loaded: set[int] = set()
        self.lock = threading.Lock()
        self._n: dict[int, int] = {}

    def check(self) -> str | None:
        return None

    def read_cap(self, cpu: int) -> int:
        return int(self.cpufreq.read(cpu, CAP_ATTR))

    def write_cap(self, cpu: int, khz: int) -> None:
        self.cpufreq.write(cpu, CAP_ATTR, str(int(khz)))

    def read_max_khz(self, cpu: int) -> int:
        return self.model.max_khz(cpu)

    def start_load(self, cpus: Sequence[int], seconds: float) -> _SimLoad:
        return _SimLoad(self, cpus)

    def read_temp_c(self, cpu: int) -> float | None:
        unit = self.model.unit_of(cpu)
        ratio = self.model.settled(unit, self.read_cap(cpu)) if cpu in self.loaded else 8
        return self.model.temp_c(unit, ratio)

    def read_msr(self, cpu: int, msr: int) -> int:
        """Plain ``rdmsr`` of the turbo ratio tables (``0x1AD`` P, ``0x650`` E): the stock values of the platform file ``[MEASURED lnx 2026-09-29]``."""
        if msr not in (0x1AD, 0x650):
            raise OSError(5, f"MSR {msr:#x} is not simulated")
        return default_platform().turbo_defaults[msr]

    def read_perf(self, cpu: int) -> int:
        unit_cpus_ = set(unit_cpus(self.model.unit_of(cpu)))
        with self.lock:
            n = self._n[cpu] = self._n.get(cpu, 0) + 1
            loaded = cpu in self.loaded
            busy_others = len(self.loaded - unit_cpus_)
        return self.model.word(cpu, self.read_cap(cpu), loaded, n, busy_others)


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


def sim_memory() -> dict[str, Any]:
    """The ``--sim`` ``mem.read``: the knowledge fixture (``memory-mchbar.md``, the reference machine's BIOS DDR5-7200) with two obviously simulated DIMMs."""
    ta = {"tCL": 36, "tRCD": 44, "tRP": 44, "tRAS": 96, "tRC": 140, "tRFC": 574, "tRFCpb": 467, "tREFI": 32767, "tFAW": 32, "tRRD_L": 12, "tRRD_S": 8, "tCWL": 34,
          "tRTP": 23, "tWR": 110, "tWTP": 152, "tRCDW": 44, "tRPab": 44, "tREFSBRD": 40}
    turn = {"RDRD": (18, 8, 16, 16), "RDWR": (23, 23, 23, 23), "WRRD": (68, 48, 13, 13), "WRWR": (72, 8, 18, 18)}

    def dimm(n: int, temp: float) -> dict[str, Any]:
        return {"locator": f"DIMM_{'AB'[n]}1", "size": 16384, "part": "SIM-DDR5-7200", "manufacturer": "SIMULATED", "speed": 7200, "configured_speed": 7200, "voltage": 1.4,
                "rank": 1, "temp_c": {"value": temp, "unit": "C", "source": "sim", "status": "ok"}}

    return {
        "available": True, "mts": 7200, "qclk_mhz": 3600.0, "gear": 2, "cmd_rate": "2T", "bclk_mhz": 100.0, "timings": ta,
        "turnaround": {k: dict(zip(("SG", "DG", "SD", "DD"), v)) for k, v in turn.items()},
        "instances_ok": 6, "cross_check": {"agree": True, "mhz_a": 3600.0, "mhz_b": 3600.0}, "max_bandwidth_mbs": 115200,
        "dimms": [dimm(0, 38.5), dimm(1, 39.0)],
    }


def sim_wiring(state_dir: Path | str, *, time_scale: float = SIM_TIME_SCALE) -> MeasureWiring:
    """``--sim``: the model, an in-memory cpufreq, the store and the journal under ``state_dir`` (never ``/var/lib``)."""
    state = Path(state_dir)
    cpufreq = SimCpufreq()
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
# the journal
# =============================================================================================================================================
def _write_journal(w: MeasureWiring, cpus: Sequence[int], boot_id: str | None) -> list[dict[str, Any]]:
    """Read every value the run will touch and write the journal (temp + fsync + rename) **before** the first change. Returns the entries."""
    entries: list[dict[str, Any]] = []
    context: dict[str, dict[str, str]] = {}
    for cpu in cpus:
        for attr in TOUCHED:
            value = w.cpufreq.read(cpu, attr)
            if value is not None:
                entries.append({"cpu": cpu, "attr": attr, "value": value})
        context[str(cpu)] = {a: v for a in CONTEXT if (v := w.cpufreq.read(cpu, a)) is not None}
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
    """Write every journaled value back (max before min before governor before EPP; only where the current value differs). Returns the failures, never raises."""
    problems: list[str] = []
    for e in sorted(entries, key=lambda e: (e["cpu"], _ATTR_ORDER.get(e["attr"], 9))):
        cpu, attr, value = e["cpu"], e["attr"], e["value"]
        try:
            if w.cpufreq.read(cpu, attr) == value:
                continue
            w.cpufreq.write(cpu, attr, value)
        except Exception as exc:
            problems.append(f"could NOT restore {attr} of cpu {cpu} to {value!r}: {type(exc).__name__}: {exc} (restore it by hand)")
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
        if (not isinstance(e, dict) or not _is_int(e.get("cpu")) or not 0 <= e["cpu"] < 1024 or e.get("attr") not in TOUCHED
                or not isinstance(e.get("value"), str) or len(e["value"]) > 64 or not re.fullmatch(r"[A-Za-z0-9_ .-]*", e["value"])):
            raise ValueError(f"bad journal entry {e!r}")
        out.append({"cpu": e["cpu"], "attr": e["attr"], "value": e["value"]})
    return out


def recover_journal(w: MeasureWiring, log: Callable[[str], None]) -> bool:
    """At helper start: a journal on disk is a measurement that never finished. Restore it, then remove it (only when everything was restored). Returns True if
    a journal was found. The journal is root's own file under the root-owned run directory; it is opened ``O_NOFOLLOW`` and ``fstat``-checked, and only the
    two attributes the run ever touches are accepted from it."""
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
    problems = _restore(w, entries)
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
    if units is None:
        units = _NAME_UNITS
    if not isinstance(units, list) or not 1 <= len(units) <= len(UNITS) or not all(isinstance(u, str) and u in UNITS for u in units) or len(set(units)) != len(units):
        raise OpError(BAD_REQUEST, f"'units' must be a non-empty list of distinct units from {', '.join(_NAME_UNITS)}")
    name = args.get("name")
    if name is not None:
        try:
            name = validate_name(name)
        except ValueError as exc:
            raise OpError(BAD_REQUEST, str(exc)) from None
    baseline = args.get("baseline", False)
    if not isinstance(baseline, bool):
        raise OpError(BAD_REQUEST, "'baseline' must be a boolean")
    return {"mode": mode, "units": [u for u in _NAME_UNITS if u in units], "name": name, "baseline": baseline}


def _wiring(helper: Any) -> MeasureWiring:
    w = getattr(helper, "measure_wiring", None)
    if w is None:
        raise OpError(INTERNAL, "this helper has no measurement support configured")
    return w


def _store_error(exc: StoreError) -> OpError:
    return OpError(exc.code, exc.message, exc.detail)


def _max_ratio(io: Any, unit: str) -> int:
    """The unit's ``cpuinfo_max_freq`` (the lowest of its CPUs) in 100 MHz bins. **Last resort only**: an E-core reports 4.9 GHz while its table stops at 48x
    (intel_pstate N -> N-1 mapping), see :func:`_unit_maxima`."""
    return min(io.read_max_khz(cpu) for cpu in unit_cpus(unit)) // 100_000


def _turbo_tables(helper: Any, run: RunEnv, problems: list[str]) -> tuple[dict[str, list[int]] | None, dict[int, int]]:
    """``({"P": [8 ratios], "E": [8 ratios]} | None, {cpu: favored ratio})``. The tables come from a live plain ``rdmsr`` of ``0x1AD`` / ``0x650`` on CPU 0 (read-only,
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
            live = {"P": list(turbo_bytes(rdmsr(0, 0x1AD))), "E": list(turbo_bytes(rdmsr(0, 0x650)))}
        except Exception as exc:
            problems.append(f"the turbo tables (0x1AD / 0x650) could not be read ({type(exc).__name__}: {exc})")
    tables_out = live or cached
    if tables_out is None or not all(tables_out.values()) or min(max(tables_out["P"]), max(tables_out["E"])) <= 0:
        problems.append("turbo tables not available: the maximum ratio of each unit comes from cpuinfo_max_freq (an E-cluster may be offered one ratio too many, 49x)")
        return None, favored
    return tables_out, favored


def _unit_maxima(io: Any, units: Sequence[str], tables: dict[str, list[int]] | None, favored: dict[int, int]) -> dict[str, int]:
    """Max ratio per unit. **E** = the highest bucket of ``0x650`` (48 on the 285K; ``cpuinfo_max_freq`` says 4.9 GHz, ``[MEASURED lnx 2026-10-11 19:06 UTC]``).
    **P** = the core's favored ratio from ``tune.read`` (``0x1C`` low byte: 57 for P0/P3, 55 for the others), else ``cpuinfo_max_freq`` (5.7 / 5.5 GHz, correct for
    P-cores); never above the top bucket of ``0x1AD``. Without tables: ``cpuinfo_max_freq``."""
    out: dict[str, int] = {}
    for unit in units:
        cpuinfo = _max_ratio(io, unit)
        if unit_kind(unit) == "E":
            out[unit] = max(tables["E"]) if tables else cpuinfo
            continue
        top = favored.get(unit_cpus(unit)[0]) or cpuinfo
        out[unit] = min(top, max(tables["P"])) if tables else top
    return out


def _plan_ratios(top: int, unit: str, mode: str) -> tuple[int, ...]:
    """Quick = the fused point ratios (F1) plus every ratio above the top point up to the unit's max turbo ``top`` (F9: E 47, 48x); dense = every ratio 8..``top``."""
    top = max(top, 8)
    if mode == "quick":
        points = QUICK_RATIOS[unit_kind(unit)]
        return points + tuple(range(points[-1] + 1, top + 1))
    return tuple(range(8, top + 1))


def _classify(m: vfdump.RatioMeasurement, max_ratio: int, intermittent: Sequence[int]) -> tuple[str, str | None]:
    """``(status, reason)`` of a measured point (F3/F9). ``unreachable`` + ``max_turbo`` (the ratio is above ``cpuinfo_max_freq``), ``active_cores`` (a favored core's
    56/57x never got ``min_valid`` samples even with the longer sampling), ``cpufreq_granularity`` (only stray samples at the target: the caps land on its neighbours,
    E 23x / P 52x). A target that was hit now and then but too rarely stays ``mismatch``."""
    if m.status == "ok":
        return "ok", None
    if m.status == "skipped":
        return "unreachable", "max_turbo"
    if m.ratio in intermittent and not (m.seen_ratios and m.seen_ratios[-1] > m.ratio):  # still below min_valid after the long sampling (never ran above)
        return "unreachable", "active_cores"
    if m.samples_kept <= max(1, m.samples_total // 5):  # a stray sample or two (the ratio wavers by a bin) is not hitting the target
        return "unreachable", "max_turbo" if m.ratio > max_ratio else "cpufreq_granularity"
    return "mismatch", None


def _fused_point(unit: str, ratio: int, oc_ratio: int | None) -> int | str | None:
    """1-based index of ``ratio`` in the unit's fused point table (the Quick ratios), else ``"oc"`` for the OC ratio, else ``None`` (an in-between ratio). Where the OC
    ratio is also a fused point (P0: 57x is point 8 and the OC word's ratio) the fused index wins."""
    fused = QUICK_RATIOS[unit_kind(unit)]
    if ratio in fused:
        return fused.index(ratio) + 1
    return "oc" if oc_ratio is not None and ratio == oc_ratio else None


def _oc_ratio(helper: Any, unit: str, top: int) -> int | None:
    """The unit's OC ratio: the ratio of the OC word of its first CPU in ``helper.last_words`` when non-zero, else its max ratio when that is above the top fused point."""
    words = getattr(helper, "last_words", None)
    ws = words.get(unit_cpus(unit)[0]) if isinstance(words, dict) else None
    if ws:
        try:
            ratio = int(ws[0]) & 0xFF
        except (TypeError, ValueError):
            ratio = 0
        if ratio:
            return ratio
    return top if top > QUICK_RATIOS[unit_kind(unit)][-1] else None


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
    return point


#: G-120 fix 4: the cold-boot OC words (``0x10 P1=0 P2=0``): P0 = ``0x39`` (OC ratio 57 = the top fused ratio, no OC point), every other core ``0x0``
#: ``[MEASURED win 2026-10-01]`` (S-03d) and ``[MEASURED lnx 2026-10-11]`` (first tune.read of the boot; the platform file's fused point of P0 is 57x).
_COLD_OC_WORD = dict(VF_DOMAIN0)


def _tuning_state(helper: Any) -> dict[str, Any]:
    """From the words G-112's ``tune.read`` / ``vf.dump`` read this helper session (``helper.last_words``: cpu -> list of u32 words), else ``unknown``. ``kind`` is
    ``stock`` (G-118, contract 9) when the words cover the first CPU of all 12 units (a complete read), **every** OC word is the cold-boot one (P0 ``0x39``, all others
    ``0x0``) and every word has offset 0 (G-120: an OC ratio or voltage raised in the OC word is not stock), and no ``tune.apply`` wrote anything this boot
    (``helper.tuned_this_boot``: a restored state is not proven stock), else ``words``. The words travel with ``words`` only."""
    words = getattr(helper, "last_words", None)
    if not isinstance(words, dict) or not words:
        return {"kind": "unknown"}
    state: dict[str, Any] = {"kind": "words", "words": {str(cpu): [f"0x{int(w) & 0xFFFFFFFF:08x}" for w in ws] for cpu, ws in sorted(words.items())}}
    try:
        complete = all(words.get(unit_cpus(u)[0]) for u in UNITS)
        cold = all(int(ws[0]) & 0xFFFFFFFF == _COLD_OC_WORD.get(int(cpu)) for cpu, ws in words.items() if ws)
        zero_offsets = all(VfWord.from_u32(int(w) & 0xFFFFFFFF).offset_raw == 0 for ws in words.values() for w in ws)
        if complete and cold and zero_offsets and not getattr(helper, "tuned_this_boot", False):
            state = {"kind": "stock"}  # contract 9: ``words`` only accompany kind "words"
    except (TypeError, ValueError):
        pass
    return state


def _retryable(point: dict[str, Any]) -> bool:
    return point["status"] == "mismatch" or (point["status"] == "unreachable" and point.get("reason") == "active_cores")


def _better(new: dict[str, Any], old: dict[str, Any]) -> bool:
    """The retry replaces the first attempt when it is ``ok`` and the first was not, or neither is ``ok`` and it kept more samples at the target."""
    if (new["status"] == "ok") != (old["status"] == "ok"):
        return new["status"] == "ok"
    return new["status"] != "ok" and new["kept"] > old["kept"]


def op_vf_measure(helper: Any, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    w = _wiring(helper)
    mode, units, name, want_baseline = args["mode"], args["units"], args["name"], args["baseline"]
    cancel = ctx.cancel
    try:
        w.prepare()
        run = w.make_run()
    except Exception as exc:
        raise OpError(REFUSED, f"the measurement cannot start: {type(exc).__name__}: {exc}", {"check": "MEASURE_UNAVAILABLE"}) from None
    problems: list[str] = []
    record: dict[str, Any] | None = None
    error: BaseException | None = None
    entries: list[dict[str, Any]] = []
    journaled = False
    cancelled = False
    try:
        why_not = run.io.check()
        if why_not:
            raise OpError(REFUSED, why_not, {"check": "LOAD_GENERATOR"})
        if cancel.is_set():
            raise _Cancelled()
        cpus = [cpu for u in units for cpu in unit_cpus(u)]
        tables, favored = _turbo_tables(helper, run, problems)
        try:
            top_of = _unit_maxima(run.io, units, tables, favored)  # reads cpuinfo_max_freq (also when the tables are known) before anything else
        except (OSError, ValueError) as exc:
            raise OpError(REFUSED, f"cpufreq is not available for the selected CPUs: {exc}", {"check": "NO_CPUFREQ"}) from None
        plan = {u: _plan_ratios(top_of[u], u, mode) for u in units}
        oc_of = {u: _oc_ratio(helper, u, top_of[u]) for u in units}
        total = sum(len(r) for r in plan.values())
        try:
            boot = helper.cfg.boot_id_fn()
        except Exception:
            boot = None
        now = w.wall_clock()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        record = {
            "id": new_curve_id(now), "name": name or f"{mode.capitalize()} {stamp[:16].replace('T', ' ')}", "created": stamp,
            "boot_id": boot.strip() if isinstance(boot, str) and boot.strip() else None, "mode": mode, "complete": False, "kernel": w.kernel_fn(),
            "tuning_state": _tuning_state(helper), "temps": {"min_c": None, "max_c": None}, "units": {}, "baseline": False,
        }
        entries = _write_journal(w, cpus, record["boot_id"])  # before the first change
        journaled = True
        for cpu in cpus:  # EPP for the run (governor unchanged); restored from the journal
            if any(e["cpu"] == cpu and e["attr"] == EPP_ATTR for e in entries):
                try:
                    w.cpufreq.write(cpu, EPP_ATTR, EPP_VALUE)
                except OSError as exc:
                    problems.append(f"energy_performance_preference of cpu {cpu} was not set ({exc.strerror or exc}); the run continues")

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
        for unit in units:
            cpus_u = unit_cpus(unit)
            entry = record["units"].setdefault(unit, {"cpus": list(cpus_u), "points": []})

            def on_result(m: vfdump.RatioMeasurement, unit: str = unit, entry: dict[str, Any] = entry) -> None:
                nonlocal done
                point = _point(m, top_of[unit], intermittent_ratios(unit, plan[unit]))
                label = _fused_point(unit, m.ratio, oc_of[unit])
                if label is not None:  # omitted when null: a dense run of 12 units must still fit one response line
                    point["fused_point"] = label
                entry["points"].append(point)
                done += 1
                seen = [t for t in (m.temp_min_c, m.temp_max_c, record["temps"]["min_c"], record["temps"]["max_c"]) if t is not None]
                if seen:
                    record["temps"] = {"min_c": round(min(seen), 1), "max_c": round(max(seen), 1)}
                ctx.emit({"event": "vf.point", "op_id": ctx.req_id, "unit": unit, "ratio": m.ratio, "mhz": point["mhz"], "vid_v": point["vid_v"], "status": point["status"], **({"reason": point["reason"]} if "reason" in point else {})})
                ctx.emit({"event": "progress", "op_id": ctx.req_id, "step": done, "of": total, "unit": unit, "ratio": m.ratio, "eta_s": eta()})

            group = vfdump.MeasureGroup(unit, unit_kind(unit), tuple(cpus_u), cpus_u[0], tuple(plan[unit]), intermittent_ratios(unit, plan[unit]))
            vfdump.measure_group(
                run.io, group, read_perf, sleep, problems, settle_s=w.settle_s, samples=w.samples, interval_s=w.interval_s, min_valid=MIN_VALID, on_result=on_result,
            )
        # Retry pass (G-118): a point that ended mismatch / unreachable (active_cores) gets one more attempt with the long sampling window, one unit at a time,
        # every other unit idle; the better result is the one reported (its kept / total).
        retry = [(u, p["ratio"]) for u in units for p in record["units"][u]["points"] if _retryable(p)]
        total += len(retry)
        for unit, ratio in retry:
            cpus_u = unit_cpus(unit)
            ctx.emit({"event": "progress", "op_id": ctx.req_id, "step": done, "of": total, "unit": unit, "ratio": ratio, "eta_s": eta()})
            second: list[vfdump.RatioMeasurement] = []
            group = vfdump.MeasureGroup(unit, unit_kind(unit), tuple(cpus_u), cpus_u[0], (ratio,), (ratio,))  # intermittent: the long window (40 x 50 ms, early stop)
            vfdump.measure_group(run.io, group, read_perf, sleep, problems, settle_s=w.settle_s, samples=w.samples, interval_s=w.interval_s, min_valid=MIN_VALID, on_result=second.append)
            done += 1
            pts = record["units"][unit]["points"]
            at = next(i for i, p in enumerate(pts) if p["ratio"] == ratio)
            old = pts[at]
            old["retried"] = True
            if second:
                new = _point(second[0], top_of[unit], intermittent_ratios(unit, plan[unit]))
                label = _fused_point(unit, ratio, oc_of[unit])
                if label is not None:
                    new["fused_point"] = label
                new["retried"] = True
                if _better(new, old):
                    pts[at] = new
                    ctx.emit({"event": "vf.point", "op_id": ctx.req_id, "unit": unit, "ratio": ratio, "mhz": new["mhz"], "vid_v": new["vid_v"], "status": new["status"], "retry": True,
                              **({"reason": new["reason"]} if "reason" in new else {})})
            ctx.emit({"event": "progress", "op_id": ctx.req_id, "step": done, "of": total, "unit": unit, "ratio": ratio, "eta_s": eta()})
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
    return _finish(w, record, cancelled, error, problems, ctx)


def _finish(w: MeasureWiring, record: dict[str, Any] | None, cancelled: bool, error: BaseException | None, problems: list[str], ctx: OpContext) -> dict[str, Any]:
    stored: dict[str, Any] | None = None
    store_problem: str | None = None
    has_points = record is not None and any(u["points"] for u in record["units"].values())
    if record is not None and has_points:
        try:
            stored = w.store.save(record)
        except StoreError as exc:
            store_problem = exc.message
    restore_bad = [p for p in problems if "could NOT restore" in p]
    detail: dict[str, Any] = {}
    if stored is not None:
        detail["curve_id"] = stored["id"]
        detail["partial"] = fit_record(stored, RECORD_BUDGET - 4096)  # the message and the problems share the line
    if problems:
        detail["problems"] = problems[:10]
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
    extra = {"problems": problems[:10]} if problems else {}
    return {"curve_id": stored["id"], "record": fit_record(stored, RECORD_BUDGET - len(json.dumps(extra))), **extra}


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
    if units is not None and (not isinstance(units, list) or not units or not all(isinstance(u, str) and u in UNITS for u in units)):
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
    return {"curves": rows}


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
    return summary(record)


def op_curves_delete(helper: Any, ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        _wiring(helper).store.delete(args["id"])
    except StoreError as exc:
        raise _store_error(exc) from None
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
