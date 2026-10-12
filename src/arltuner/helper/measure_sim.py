"""The ``--sim`` side of the measurement ops (D-036): a SIMULATED REFERENCE CPU, a fixture for the simulator and the tests, **never** used on hardware.

``SimModel`` turns the first-run stock V/F curve of one reference part (``STOCK_VID``) into what the measurement reads (requested VID per cap, settled ratio,
jitter); ``SimCpufreq`` / ``SimMeasureIO`` are in-memory stand-ins for cpufreq sysfs and the load generator; ``sim_memory`` is a made-up ``mem.read``. All of it takes
the topology it simulates (the reference CPU, or a ``synthetic`` 6P+8E ...), so the same code paths run on any shape. ``ops_measure`` imports this module lazily.
"""
from __future__ import annotations

import random
import threading
from collections.abc import Sequence
from typing import Any

from arltuner.msr.sim import PLAIN_MSR_DEFAULTS, POINT_COUNTS_ARL_S, POINT_RATIOS_ARL_S, SimConfig
from arltuner.topology import Topology
from arltuner.topology.reference import arl_s_285k
from arltuner.vfmodel import complete_baseline, effective_curve, voltage_at

from .ops_measure import CAP_ATTR, EPP_ATTR, GOV_ATTR, MIN_ATTR

__all__ = ["STOCK_VID", "SimCpufreq", "SimMeasureIO", "SimModel", "sim_memory"]

#: First-run STOCK V/F curve of the simulated reference CPU, requested VID in volts per fused point (``None`` = never settled: the favored P-cores' top point
#: intermittent, E 23x not pinnable, non-favored P-cores' top two points unreachable). The simulator's ``vid`` is this curve through :mod:`arltuner.vfmodel`
#: (offsets, F4 clamp, interpolation), not an invented formula. A simulated part with more units than the reference reuses the curves cyclically.
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
_REFERENCE_P_POINTS = tuple(POINT_RATIOS_ARL_S[("P", n)] for n in range(1, POINT_COUNTS_ARL_S["P"] + 1))
_REFERENCE_E_POINTS = tuple(POINT_RATIOS_ARL_S[("E", n)] for n in range(1, POINT_COUNTS_ARL_S["E"] + 1))


class SimCpufreq:
    """In-memory stand-in for :class:`~arltuner.helper.ops_measure.CpufreqFiles` (the ``--sim`` helper): same ``read`` / ``write`` surface."""

    def __init__(self, model: SimModel | None = None) -> None:
        self.model = model or SimModel()
        self.values: dict[tuple[int, str], str] = {}
        self.lock = threading.Lock()
        for core in self.model.topology.cores:
            cpu = core.cpu
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


class SimModel:
    """A simulated part for the simulator: ``vid(unit, ratio)`` = the stock curve (:data:`STOCK_VID`) through the F4 clamp / interpolation of :mod:`arltuner.vfmodel`
    (``offsets_mv[unit] = {point ratio: mV}`` moves it), what ratio a cap settles at, jitter. Deterministic (seeded per unit / ratio / sample).

    Reach (F3): P-cores top out at the ``cpuinfo_max_freq`` ratio the topology gives them (the favored ones higher), E-clusters at 48x (the E turbo table) while their
    ``cpuinfo_max_freq`` says one bin more (the intel_pstate N -> N-1 mapping). **A favored core only holds a ratio above the all-core bucket while at most one other
    CPU is busy**: about 70 % of the samples when quiet, none when ``busy_others >= 2``. cpufreq granularity (F9): E 23x and P 52x are not pinnable (the caps land on the
    neighbours). E-cores also settle one bin low when the cap is in 16..29 (the intel_pstate quirk, so the cap search retries ``ratio + 1``)."""

    E_REACH = 48
    E_CPUINFO = 49
    E_QUIRK = range(16, 30)
    #: G-131: the kernel converts an E-core cap to HWP perf units, ``perf = round(khz / scale)`` with ``scale = cpuinfo_max_freq / highest_perf`` (hybrid scaling factor), so a
    #: cap lands on a perf step, not on the exact value written; ``scaling_max_freq`` reads back the step's frequency. ``highest_perf`` 69 = HWP highest of an E-core at 49x
    #: ``[MEASURED lnx 2026-10-01]`` (docs/knowledge/tuning-results.md). Every perf step that a multiple of 100 MHz selects runs the ratio the table-driven rule below gives for
    #: that multiple; a step that none selects (E 23x: step 33, between the caps 2300 and 2400 MHz) runs the ratio its own frequency truncates to. **A fixture of the cap
    #: search, [UNVERIFIED] as a model of the silicon**: the hardware is the arbiter of which ratios the search can reach.
    E_HIGHEST_PERF = 69
    QUIET_SHARE = 0.7  # share of samples a favored core spends above the all-core ratio when no other core is busy

    def __init__(self, seed: int = 285, background_busy: int = 0, topology: Topology | None = None) -> None:
        self.seed = seed
        self.topology = topology if topology is not None else arl_s_285k()
        self.background_busy = background_busy  # busy CPUs outside the run (a desktop session, the helper): >= 2 takes the favored top ratio away
        units = self.topology.unit_ids()
        spread = random.Random(seed)
        self.spread = {u: round(spread.uniform(-0.025, 0.025), 4) for u in units}
        self.offsets_mv: dict[str, dict[int, float]] = {}
        self._base: dict[str, dict[int, float | None]] = {}
        self.favored_units = frozenset(u for u in units if u.startswith("P") and self.topology.by_cpu(self.topology.unit_cpus(u)[0]).favored)
        self.all_core_ratio = (PLAIN_MSR_DEFAULTS[0x1AD] >> 56) & 0xFF  # the simulated P table's all-core bucket (55)
        self._fused = SimConfig.measured(topology=self.topology).fused_point

    def unit_of(self, cpu: int) -> str:
        return self.topology.unit_of_cpu(cpu)

    def max_khz(self, cpu: int) -> int:
        core = self.topology.by_cpu(cpu)
        if core.max_ratio:
            return core.max_ratio * 100_000
        return (self.E_CPUINFO if core.kind == "E" else 55) * 100_000

    def reach(self, unit: str) -> int:
        if unit.startswith("E"):
            return self.E_REACH
        return self.max_khz(self.topology.unit_cpus(unit)[0]) // 100_000

    def points(self, unit: str) -> tuple[int, ...]:
        return _REFERENCE_P_POINTS if unit.startswith("P") else _REFERENCE_E_POINTS

    def _stock(self, unit: str) -> dict[int, float | None]:
        if unit in STOCK_VID:
            return STOCK_VID[unit]
        prefix = "P" if unit.startswith("P") else "EC"
        count = 8 if prefix == "P" else 4
        return STOCK_VID[f"{prefix}{int(unit[len(prefix):]) % count}"]

    def _baseline(self, unit: str) -> dict[int, float | None]:
        if unit not in self._base:
            fused_top = None
            if unit.startswith("P"):  # the unmeasured top point of a core that cannot reach it: its fused 0x09 voltage (F7)
                word = self._fused[self.topology.unit_cpus(unit)[0]]
                fused_top = ((word >> 8) & 0xFFF) / 1024
            self._base[unit] = complete_baseline(self.points(unit), self._stock(unit), fused_top)
        return self._base[unit]

    def vid(self, unit: str, ratio: int) -> float:
        curve = effective_curve(self.points(unit), self._baseline(unit), self.offsets_mv.get(unit, {}))
        v = voltage_at(curve, ratio)
        assert v is not None
        return v

    def temp_c(self, unit: str, ratio: int) -> float:
        return round(34 + 30 * min(1.0, (ratio - 8) / 49) + self.spread[unit] * 60, 1)

    @classmethod
    def e_scale(cls) -> float:
        """kHz per HWP perf unit of an E-core."""
        return cls.E_CPUINFO * 100_000 / cls.E_HIGHEST_PERF

    @classmethod
    def e_cap_quantized(cls, cap_khz: int) -> int:
        """What an E-core's ``scaling_max_freq`` reads back after ``cap_khz`` was written: the perf step's frequency (idempotent)."""
        scale = cls.e_scale()
        return round(round(cap_khz / scale) * scale)

    @classmethod
    def _e_landing(cls, cap_khz: int) -> int:
        """The ratio an E cap lands on before the reach limit: the table-driven rule for the perf steps the 100 MHz multiples select, truncation for the others."""
        scale = cls.e_scale()
        perf = round(cap_khz / scale)
        for k in range(8, 50):
            if round(k * 100_000 / scale) == perf:
                return k - 1 if k in cls.E_QUIRK and k != 24 else k  # 24 is not low: with 23 it makes 23x unpinnable by the 100 MHz caps
        return int(perf * scale // 100_000)

    def settled(self, unit: str, cap_khz: int) -> int:
        """The ratio the unit settles at under load with ``scaling_max_freq = cap_khz`` (quiet system)."""
        cap_ratio = cap_khz // 100_000
        if unit.startswith("E"):
            cap_ratio = self._e_landing(cap_khz)
        if unit.startswith("P") and cap_ratio == 52:  # P 52x is not pinnable either: the cap lands on 51 (cap 53 gives 53)
            cap_ratio = 51
        return max(8, min(cap_ratio, self.reach(unit)))

    def word(self, cpu: int, cap_khz: int, loaded: bool, n: int, busy_others: int = 0) -> int:
        """The ``0x198`` word of sample ``n``: running ratio ``[15:8]`` (one bin low now and then, as the real ratio wavers; a favored core above the all-core ratio
        falls back to it/51x for most samples when other cores are busy), VID ``[47:32]`` in 1/8192 V."""
        unit = self.unit_of(cpu)
        ratio = self.settled(unit, cap_khz) if loaded else 8
        rng = random.Random(f"{self.seed}/{unit}/{ratio}/{n}")
        if loaded and unit in self.favored_units and ratio > self.all_core_ratio:
            share = 0.0 if busy_others + self.background_busy >= 2 else self.QUIET_SHARE
            if rng.random() >= share:
                ratio = self.all_core_ratio if rng.random() < 0.8 else 51
        elif rng.random() < 0.08 and ratio > 8:
            ratio -= 1
        vid = self.vid(unit, ratio) + rng.uniform(-0.0015, 0.0015)
        return (round(vid * 8192) << 32) | (ratio << 8)


class _SimLoad:
    def __init__(self, io: SimMeasureIO, cpus: Sequence[int]) -> None:
        self._io, self._cpus = io, tuple(cpus)
        with io.lock:
            io.loaded.update(self._cpus)

    def stop(self) -> None:
        with self._io.lock:
            self._io.loaded.difference_update(self._cpus)


class SimMeasureIO:
    """:class:`~arltuner.vfdump.MeasureIO` over a cpufreq store (:class:`SimCpufreq` or a fake-sysfs ``CpufreqFiles``) and the :class:`SimModel` instead of
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
        if self.model.unit_of(cpu).startswith("E"):
            khz = self.model.e_cap_quantized(int(khz))  # the kernel's perf rounding: the read-back is the step's frequency
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
        """Plain ``rdmsr`` of the turbo ratio tables (``0x1AD`` P, ``0x650`` E): the simulated reference CPU's stock values."""
        if msr not in (0x1AD, 0x650):
            raise OSError(5, f"MSR {msr:#x} is not simulated")
        return PLAIN_MSR_DEFAULTS[msr]

    def read_perf(self, cpu: int) -> int:
        unit_cpus_ = set(self.model.topology.unit_cpus(self.model.unit_of(cpu)))
        with self.lock:
            n = self._n[cpu] = self._n.get(cpu, 0) + 1
            loaded = cpu in self.loaded
            busy_others = len(self.loaded - unit_cpus_)
        return self.model.word(cpu, self.read_cap(cpu), loaded, n, busy_others)


def sim_memory() -> dict[str, Any]:
    """The ``--sim`` ``mem.read``: made-up DDR5-7200 timings with two obviously simulated DIMMs."""
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
