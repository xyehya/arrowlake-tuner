"""``arltuner vf-dump`` (T-026): read the current V/F curve of every selected core and optionally measure the voltage of each point.

READ-ONLY toward the CPU's tuning state: this module never sends a write command and never ``wrmsr``s anything except what a guarded mailbox
*read* is (a WRMSR to MSR 0x150, ``docs/SAFETY.md`` "OC mailbox rule"). Every mailbox request and every plain ``rdmsr`` goes through the
``WriteGuard`` handed in by ``arltuner.cli``; this module never touches a transport or a port.

Facts (``docs/knowledge/platform-285k.md`` "Full point layout", "E cluster 1 V/F curve", "Step 2"):
  * ``0x10 P1=0 P2=n`` is the V/F word of point ``n`` (layout: ``arltuner.mailbox.VfWord``); ``P2 = 0`` is the OC word (OC ratio, adaptive V target,
    whole-curve offset). ``[MEASURED lnx 2026-10-02]``: **P-cores have 8 points** (CPU 0: 8,20,30,48,53,55,56,57x, BIOS offsets -49.8 mV at 53x and 57x,
    -14.6 mV at 55x), **E-cores 6** (8,15,23,30,40,46x); the point after the last answers rc ``0x11``.
  * ``[COMMUNITY]`` (SkatterBencher, operator 2026-10-02; leads, not constants): the tables are **per core** (57x is the boost ratio of the *favored* P-cores, CPU 0 and
    3; the others may top out lower or have other point ratios), and an OC ratio above the top fused ratio creates **one additional point**. So the walk never stops at
    the known count: it reads until the first rc ``0x11``, with a hard cap (P ``P2 <= 10``, E ``P2 <= 8``); the known counts are only used to point out extras.
  * The V target of a fused point is 0: its voltage is not in the word. It can only be measured: cap cpufreq ``scaling_max_freq`` so the core runs at
    the point's ratio, load it, read the requested VID = MSR ``0x198[47:32] / 8192`` V (running ratio ``0x198[15:8]``, must equal the target).
    ``[MEASURED lnx 2026-10-02]`` intel_pstate quirk: the cap that gives the ratio is ratio x 100 MHz or (ratio + 1) x 100 MHz, depending on core kind *and* ratio (T-027:
    E ran the target at (ratio+1) for 30/40/46x but one bin high for 8/15/23x), so :func:`measure_group` tries the first and retries the second. E-cores share one regulator per
    cluster of 4, so a cluster is loaded as a whole and reported once. Reference: ``tools/lnx/vfcurve.py``.
  * The measured VID includes the offset currently applied to that point (the pcode's request, not the fused base). TVB (Turbo Voltage Optimization) shifts the
    voltage with temperature, ~1.1 mV per degree C (SkatterBencher ``[COMMUNITY]``, operator 2026-10-02), so **every VID is recorded with the core temperature at
    sampling time**: the coretemp hwmon (sysfs, injected), because the plain MSRs 0x19C / 0x1A2 are not in the guard's ``PLAIN_MSR_READABLE``.

Lives beside ``cli`` (not in ``controls/``, whose static rules forbid ``subprocess``): the one place that spawns the load generator is :class:`SysfsMeasureIO`.
Everything with side effects on the OS (cpufreq sysfs, the load generator, sleeping) is injected (:class:`MeasureIO`, ``sleep``), so the tests never
run a process or touch a real sysfs.
"""
from __future__ import annotations

import math
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from arltuner.guard import GuardRefused, Refusal, Request
from arltuner.mailbox import Cmd, VfWord, rc_name
from arltuner.topology import Core, Topology

__all__ = [
    "MSR_PERF_STATUS",
    "WALK_P2_CAP",
    "walk_cap",
    "RC_NO_SUCH_POINT",
    "CpuCurve",
    "Flag",
    "MeasureGroup",
    "MeasureIO",
    "Pacer",
    "RatioMeasurement",
    "Stop",
    "SysfsMeasureIO",
    "Unit",
    "CAP_SEARCH_OFFSETS_KHZ",
    "caps_to_try",
    "restore_caps",
    "save_caps",
    "set_cap",
    "find_flags",
    "format_dump",
    "measure_group",
    "parse_cpu_list",
    "plan_requests",
    "search_cap",
    "select_cpus",
    "to_json",
    "units_of",
    "walk",
]

#: Hard cap of the walk: a table is never read past ``P2 = 15``, the largest value of the ``P2`` field (registers.md "Mailbox commands": P2 1..15 = V/F point). How many points a
#: core has is read live: the walk ends at the first rc ``0x11`` ("no such point", expected at the end of a table, not an error). Reaching the cap without an rc ``0x11`` is
#: reported, not an error.
WALK_P2_CAP = 15
#: ``[MEASURED lnx 2026-10-02]``: the completion code of ``0x10 P2 = n`` past the last point (E CPU: ``P2 = 7``). Expected at the end of a table, not an error.
RC_NO_SUCH_POINT = 0x11
#: IA32_PERF_STATUS (plain rdmsr, no mailbox): ``[15:8]`` running ratio, ``[47:32]`` requested VID in 1/8192 V (``[MEASURED lnx 2026-10-02]``, tools/lnx/vfcurve.py).
MSR_PERF_STATUS = 0x198
#: Measurement defaults: seconds of load before the first sample, samples per cap attempt (each is an audited guard operation), seconds between samples,
#: and how many samples must run at the target ratio for the VID range to be reported (T-027: the running ratio wavers under a single-thread load).
SETTLE_S = 2.5
SAMPLES = 30
SAMPLE_INTERVAL_S = 0.1
MIN_VALID = 5
#: Added to every dwell wait so a sleep that returns marginally early cannot make the guard refuse DWELL half-way through (as the apply executor does).
_DWELL_MARGIN_S = 0.05


def walk_cap(policy: Any) -> int:
    """Highest ``P2`` the walk reads: as far as the reviewed policy's ``0x10 P1=0`` read entries reach without a gap from ``P2 = 1`` (at most :data:`WALK_P2_CAP`). The
    point count is discovered (the walk ends at the first rc ``0x11``), never assumed; the policy only says how far a table may be read. A policy that allows no point
    read at all gives :data:`WALK_P2_CAP`, so the request for ``P2 = 1`` is refused by the allowlist (default deny) instead of being skipped silently."""
    cap = 0
    for p2 in range(1, WALK_P2_CAP + 1):
        entry = policy.match(int(Cmd.VOLTAGE_FREQ_READ), 0, p2)
        if entry is None or entry.kind != "read":
            break
        cap = p2
    return cap or WALK_P2_CAP


def caps_to_try(ratio: int, max_khz: int | None = None) -> list[int]:
    """The ``scaling_max_freq`` values (kHz) to try for ``ratio``, in order: ``ratio x 100 MHz``, then ``(ratio + 1) x 100 MHz``. A cap above ``max_khz`` (the CPUs'
    ``cpuinfo_max_freq``) is never offered."""
    caps = [ratio * 100_000, (ratio + 1) * 100_000]
    return [c for c in caps if max_khz is None or c <= max_khz]


# --------------------------------------------------------------------------------------------------------------
# CPU selection
# --------------------------------------------------------------------------------------------------------------
def parse_cpu_list(text: str) -> list[int]:
    """``"0-3,8,12"`` -> ``[0, 1, 2, 3, 8, 12]`` (ascending, unique). ``ValueError`` on anything else."""
    cpus: set[int] = set()
    for part in text.split(","):
        lo_s, dash, hi_s = part.strip().partition("-")
        try:
            lo = int(lo_s)
            hi = int(hi_s) if dash else lo
        except ValueError:
            raise ValueError(f"{text!r} is not a CPU list (e.g. 0-7,8,12)") from None
        if lo < 0 or hi < lo:
            raise ValueError(f"{part.strip()!r} is not a valid CPU range")
        cpus.update(range(lo, hi + 1))
    return sorted(cpus)


def select_cpus(topology: Topology, cpus: str | None, all_e: bool) -> list[Core]:
    """The CPUs to dump: ``cpus`` if given (``UnknownCore`` for a CPU the machine does not have); else all P-cores plus one CPU per E-cluster (the lowest),
    or every E CPU with ``all_e``."""
    if cpus is not None:
        return [topology.by_cpu(c) for c in parse_cpu_list(cpus)]
    chosen = [c for c in topology.cores if c.kind == "P"]
    e_cores = sorted((c for c in topology.cores if c.kind == "E"), key=lambda c: c.cpu)
    if all_e:
        return chosen + e_cores
    seen: set[int | None] = set()
    for core in e_cores:
        if core.cluster not in seen:
            seen.add(core.cluster)
            chosen.append(core)
    return chosen


def plan_requests(cores: Sequence[Core], topology: Topology, cap: int = WALK_P2_CAP) -> list[Request]:
    """The worst-case request list: per CPU ``0x10 P1=0 P2 = 0 .. WALK_P2_CAP`` (the rc ``0x11`` that ends a table shortens it). Nothing is sent."""
    out: list[Request] = []
    for core in cores:
        for p2 in range(cap + 1):
            out.append(
                Request(core.cpu, int(Cmd.VOLTAGE_FREQ_READ), 0, p2, 0, f"vf-dump: read V/F {'OC word' if p2 == 0 else f'point {p2}'} of {topology.xtu_name(core)}")
            )
    return out


# --------------------------------------------------------------------------------------------------------------
# The walk
# --------------------------------------------------------------------------------------------------------------
@dataclass
class CpuCurve:
    """What was read from one CPU: the OC word (``P2 = 0``) and the point words in order. ``end`` says how the table ended: ``"rc 0x11 at P2=n"``,
    ``"cap reached (P2=n), no rc 0x11 seen"`` or ``"stopped"`` (the dump was aborted before the table was complete)."""

    cpu: int
    kind: str
    cluster: int | None
    name: str
    oc: int | None = None
    points: list[int] = field(default_factory=list)  # raw words of P2 = 1, 2, ...
    end: str = "stopped"


@dataclass(frozen=True)
class Stop:
    """Why the dump was aborted: ``code`` is the CLI exit code (2 refused, 3 hardware / rc / trip / interrupt)."""

    code: int
    text: str


class Pacer:
    """Waits (via ``sleep``) until ``dwell_s`` has passed since the previous executed *mailbox* operation. The guard refuses, never sleeps."""

    def __init__(self, dwell_s: float, clock: Callable[[], float], sleep: Callable[[float], object]) -> None:
        self._dwell = dwell_s
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is None or self._dwell <= 0:
            return
        remaining = self._dwell - (self._clock() - self._last)
        if remaining > 0:
            self._sleep(remaining + _DWELL_MARGIN_S)

    def mark(self) -> None:
        self._last = self._clock()


Executor = Callable[[Any], Any]  # ``lambda req: guard.execute(req, confirmation)``


def walk(
    cores: Sequence[Core],
    topology: Topology,
    execute: Executor,
    pacer: Pacer,
    refusal_text: Callable[[GuardRefused], str],
    cap: int = WALK_P2_CAP,
) -> tuple[list[CpuCurve], Stop | None]:
    """Read every table, one guarded request at a time. Per CPU: ``P2 = 0``, then ``P2 = 1, 2, ...`` until the first rc ``0x11`` (the expected end of a table; the
    known point count is *not* a stop: an OC ratio above the top fused ratio adds a point) or the hard cap :data:`WALK_P2_CAP` (reported as ``end``, not an error).
    Any other rc (rc ``0x11`` at ``P2 = 0`` included), a guard refusal / trip, a timeout or Ctrl-C stops the **whole** dump: what was read so far is returned
    with the :class:`Stop`."""
    curves: list[CpuCurve] = []
    for core in cores:
        curve = CpuCurve(core.cpu, core.kind, core.cluster, topology.xtu_name(core))
        curves.append(curve)
        for p2 in range(cap + 1):
            req = Request(core.cpu, int(Cmd.VOLTAGE_FREQ_READ), 0, p2, 0, f"vf-dump: read V/F {'OC word' if p2 == 0 else f'point {p2}'} of {curve.name}")
            try:
                pacer.wait()
                result = execute(req)
                pacer.mark()
            except GuardRefused as exc:
                pacer.mark()
                return curves, Stop(3 if exc.reason is Refusal.TRIPPED else 2, refusal_text(exc))
            except KeyboardInterrupt:
                return curves, Stop(3, "interrupted: the dump was stopped; requests already sent are in the audit log.")
            except Exception as exc:  # MailboxTimeout / MsrError: the guard has audited it and tripped
                sent = getattr(exc, "write_sent", None)
                extra = "" if sent is None else f" (request written: {sent})"
                return curves, Stop(3, f"hardware error on cpu {core.cpu} P2={p2}: {type(exc).__name__}: {exc}{extra}")
            rc = result.response.rc
            if rc == RC_NO_SUCH_POINT and p2 >= 1:
                curve.end = f"rc 0x11 at P2={p2}"
                break
            if rc != 0:
                return curves, Stop(3, f"cpu {core.cpu} P2={p2}: mailbox completion code {rc} ({rc_name(rc)}); the dump was stopped")
            if p2 == 0:
                curve.oc = result.response.data
            else:
                curve.points.append(result.response.data)
        else:
            curve.end = f"cap reached (P2={cap}), no rc 0x11 seen"
    return curves, None


# --------------------------------------------------------------------------------------------------------------
# Units (P-core / E-cluster) and the differences between their CPUs
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Unit:
    """A P-core or an E-cluster: the thing that has one V/F curve. ``curves`` are the CPUs read of it (E: one, or up to four with ``--all-e``)."""

    name: str
    kind: str
    cluster: int | None
    cluster_cpus: tuple[int, ...]  # the CPUs that share the regulator / have to be loaded for a measurement
    curves: tuple[CpuCurve, ...]


def units_of(curves: Sequence[CpuCurve], topology: Topology) -> list[Unit]:
    """Group CPUs: each P-core is a unit, E CPUs of one cluster are one unit (in CPU order)."""
    units: list[Unit] = []
    by_cluster: dict[int, list[CpuCurve]] = {}
    for curve in curves:
        if curve.kind == "P":
            units.append(Unit(curve.name, "P", None, (curve.cpu,), (curve,)))
        else:
            assert curve.cluster is not None
            by_cluster.setdefault(curve.cluster, []).append(curve)
    for cluster in sorted(by_cluster):
        members = tuple(sorted(by_cluster[cluster], key=lambda c: c.cpu))
        cpus = topology.cluster_cpus(cluster)
        units.append(Unit(f"E-cluster {cluster} (CPUs {cpus[0]}-{cpus[-1]})", "E", cluster, cpus, members))
    return units


@dataclass(frozen=True)
class Flag:
    """A difference that should not normally exist: ``scope`` (``"P-cores"`` or ``"E-cluster n"``), ``point`` (0 = the OC word), ``field``
    (``"offset"`` or ``"ratio"``) and the value of each CPU."""

    scope: str
    point: int
    field: str
    values: Mapping[int, str]  # cpu -> text

    @property
    def text(self) -> str:
        what = "OC word" if self.point == 0 else f"point {self.point}"
        groups: dict[str, list[int]] = {}
        for cpu, val in self.values.items():
            groups.setdefault(val, []).append(cpu)
        who = "; ".join(f"{val}: cpu {', '.join(map(str, cpus))}" for val, cpus in groups.items())
        return f"{self.scope} {what}: {self.field} differs ({who})"


def _word_at(curve: CpuCurve, p2: int) -> int | None:
    if p2 == 0:
        return curve.oc
    return curve.points[p2 - 1] if p2 - 1 < len(curve.points) else None


def _flags_among(scope: str, curves: Sequence[CpuCurve]) -> list[Flag]:
    flags: list[Flag] = []
    if len(curves) < 2:
        return flags
    top = max(max(len(c.points) for c in curves), 0)
    for p2 in range(top + 1):
        words = {c.cpu: _word_at(c, p2) for c in curves}
        offsets = {cpu: None if w is None else VfWord.from_u32(w).offset_raw for cpu, w in words.items()}
        if len(set(offsets.values())) > 1:
            flags.append(Flag(scope, p2, "offset", {cpu: "absent" if o is None else _offset_text(o) for cpu, o in offsets.items()}))
        if p2 >= 1:  # the OC ratio legitimately differs between P-cores (favored cores); a point ratio does not
            ratios = {cpu: None if w is None else VfWord.from_u32(w).ratio for cpu, w in words.items()}
            if len(set(ratios.values())) > 1:
                flags.append(Flag(scope, p2, "ratio", {cpu: "absent" if r is None else f"{r}x" for cpu, r in ratios.items()}))
    return flags


def find_flags(units: Sequence[Unit]) -> list[Flag]:
    """Offsets / ratios that differ between P-cores at the same point, and between the CPUs of one E-cluster that were read."""
    flags = _flags_among("P-cores", [u.curves[0] for u in units if u.kind == "P"])
    for unit in units:
        if unit.kind == "E":
            flags += _flags_among(f"E-cluster {unit.cluster}", unit.curves)
    return flags


# --------------------------------------------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------------------------------------------
class MeasureIO(Protocol):
    """The OS side of ``--measure`` (injected; the real one is :class:`SysfsMeasureIO`). ``read_cap`` / ``write_cap`` are cpufreq ``scaling_max_freq`` in kHz."""

    def check(self) -> str | None:
        """``None`` if the load generator is usable, else why not."""
        ...

    def read_cap(self, cpu: int) -> int: ...

    def write_cap(self, cpu: int, khz: int) -> None: ...

    # Optional (G-132): ``read_min(cpu) -> int | None`` / ``write_min(cpu, khz)`` = ``scaling_min_freq``; with them every cap is pinned (min = max), see :func:`set_cap`.

    def read_max_khz(self, cpu: int) -> int:
        """``cpuinfo_max_freq`` in kHz: a cap is never set above it."""
        ...

    def start_load(self, cpus: Sequence[int], seconds: float) -> Any:
        """Start a CPU load pinned to ``cpus``; the returned handle has ``stop()``."""
        ...

    def read_temp_c(self, cpu: int) -> float | None:
        """Core temperature (degrees C) of ``cpu`` from the coretemp hwmon, or ``None`` if there is no sensor for it. Read-only sysfs."""
        ...


def _min_of(io: Any, cpu: int) -> int | None:
    """``scaling_min_freq`` of ``cpu`` when the IO can pin (G-132: ``read_min`` / ``write_min``), else ``None`` (the cap alone is written, as before)."""
    reader = getattr(io, "read_min", None)
    if reader is None:
        return None
    try:
        return reader(cpu)
    except (OSError, ValueError):
        return None


def set_cap(io: Any, cpu: int, khz: int) -> None:
    """G-132 (R-001 section 5): cap ``cpu`` at ``khz`` by pinning ``scaling_min_freq`` = ``scaling_max_freq`` = ``khz`` (profile-proof: EPP ``power`` cannot hold the core below it). The kernel
    refuses ``min > max``, so the order follows the direction: ``khz`` at or above the current minimum -> max first, then min; below it -> min first, then max. An IO without
    ``read_min`` / ``write_min`` (or a CPU without ``scaling_min_freq``) gets the max-only write of before."""
    cur_min = _min_of(io, cpu)
    if cur_min is None:
        io.write_cap(cpu, khz)
    elif khz >= cur_min:
        io.write_cap(cpu, khz)
        io.write_min(cpu, khz)
    else:
        io.write_min(cpu, khz)
        io.write_cap(cpu, khz)


def save_caps(io: Any, cpus: Sequence[int]) -> dict[int, tuple[int | None, int]]:
    """``{cpu: (scaling_min_freq | None, scaling_max_freq)}`` to hand to :func:`restore_caps`."""
    return {cpu: (_min_of(io, cpu), io.read_cap(cpu)) for cpu in cpus}


def restore_caps(io: Any, saved: Mapping[int, tuple[int | None, int]], problems: list[str]) -> None:
    """Put back what :func:`save_caps` read (both values, in the order the kernel accepts); a failure is appended to ``problems`` and never swallowed."""
    for cpu, (mn, mx) in saved.items():
        try:
            cur_min = _min_of(io, cpu)
            if mn is None or cur_min is None:
                io.write_cap(cpu, mx)
            elif mx >= cur_min:
                io.write_cap(cpu, mx)
                io.write_min(cpu, mn)
            else:
                io.write_min(cpu, mn)
                io.write_cap(cpu, mx)
        except Exception as exc:
            problems.append(f"could NOT restore scaling_max_freq of cpu {cpu} to {mx} kHz: {type(exc).__name__}: {exc} (restore it by hand)")


@dataclass(frozen=True)
class MeasureGroup:
    """One thing to measure: all ``cpus`` are capped and loaded, the VID is read on ``rep``, for each of ``ratios`` (ascending)."""

    name: str
    kind: str
    cpus: tuple[int, ...]
    rep: int
    ratios: tuple[int, ...]
    #: G-114 (F3, orchestrator measurement ``[MEASURED lnx 2026-10-11]``: a favored core pinned at 57x spent 31 of 40 samples there, the rest at 55/51/56 when a
    #: 3rd core woke): ratios that only a FEW active cores allow. They are sampled longer (``LONG_SAMPLES`` x ``LONG_INTERVAL_S``), stopping as soon as
    #: ``min_valid`` samples sit at the target; only if that is still not reached does the point count as not reachable (``active_cores``).
    intermittent: tuple[int, ...] = ()


@dataclass(frozen=True)
class RatioMeasurement:
    """``status``: ``"ok"`` (``vid_min`` / ``vid_max`` set, from the ``samples_kept`` of ``samples_total`` samples that ran at the target ratio), ``"MISMATCH"``
    (fewer than ``min_valid`` samples at the target, or it ran above: no voltage) or ``"skipped"``. ``cap_khz`` is the cap of the reported attempt."""

    ratio: int
    status: str
    cap_khz: int | None = None
    vid_min: float | None = None
    vid_max: float | None = None
    seen_ratios: tuple[int, ...] = ()
    note: str = ""
    temp_min_c: float | None = None  # core temperature over the samples (``None``: no sensor); TVB moves the voltage ~1.1 mV per degree C
    temp_max_c: float | None = None
    samples_kept: int = 0  # samples whose running ratio equalled the target (the VID range is over these only)
    samples_total: int = 0
    why: str = ""  # MISMATCH / skipped only (G-111): "below" = the ratio was never reached (cap search exhausted, or the target is above the CPUs' limits) ; "above" = it ran above the target or hovered around it ; "" = ok
    vid_mean: float | None = None  # mean VID over the kept samples (G-111, the curve record's ``vid_v``)


def measure_groups(units: Sequence[Unit]) -> list[MeasureGroup]:
    """One group per P-core / E-cluster: every point ratio (and the OC ratio if it is higher than the top point), ascending. The VID is read on the first CPU read."""
    groups: list[MeasureGroup] = []
    for unit in units:
        curve = unit.curves[0]
        ratios = {VfWord.from_u32(w).ratio for w in curve.points}
        if curve.oc is not None:
            oc_ratio = VfWord.from_u32(curve.oc).ratio
            if not ratios or oc_ratio > max(ratios):
                ratios.add(oc_ratio)
        ratios.discard(0)
        groups.append(MeasureGroup(unit.name, unit.kind, unit.cluster_cpus, curve.cpu, tuple(sorted(ratios))))
    return groups


def _temp_or_none(io: MeasureIO, cpu: int) -> float | None:
    """A temperature read must never abort a measurement: any error is "no sensor"."""
    try:
        return io.read_temp_c(cpu)
    except Exception:
        return None


#: Samples for an ``intermittent`` ratio: up to 40 readings 50 ms apart (2 s), stopping early once ``min_valid`` readings are at the target.
LONG_SAMPLES = 40
LONG_INTERVAL_S = 0.05


def _sample(
    io: MeasureIO,
    group: MeasureGroup,
    cap: int,
    read_perf: Callable[[int], int],
    sleep: Callable[[float], object],
    settle_s: float,
    samples: int,
    interval_s: float,
    early: tuple[int, int] | None = None,
) -> tuple[list[int], list[float]]:
    """Cap every CPU of the group at ``cap``, load them, take ``samples`` readings of ``0x198`` (and the temperature) on the rep CPU; the load is always stopped.
    ``early = (target ratio, n)``: stop as soon as ``n`` readings have that running ratio ``[15:8]``."""
    for cpu in group.cpus:
        set_cap(io, cpu, cap)
    load = io.start_load(group.cpus, settle_s + samples * interval_s + 2.0)
    words: list[int] = []
    temps: list[float] = []
    try:
        sleep(settle_s)
        for i in range(samples):
            words.append(read_perf(group.rep))
            temp = _temp_or_none(io, group.rep)
            if temp is not None:
                temps.append(temp)
            if early is not None and sum(((w >> 8) & 0xFF) == early[0] for w in words) >= early[1]:
                break
            if i + 1 < samples:
                sleep(interval_s)
    finally:
        load.stop()
    return words, temps


def measure_group(
    io: MeasureIO,
    group: MeasureGroup,
    read_perf: Callable[[int], int],
    sleep: Callable[[float], object],
    problems: list[str],
    *,
    settle_s: float = SETTLE_S,
    samples: int = SAMPLES,
    interval_s: float = SAMPLE_INTERVAL_S,
    min_valid: int = MIN_VALID,
    progress: Callable[[str], object] | None = None,
    on_result: Callable[[RatioMeasurement], object] | None = None,
) -> list[RatioMeasurement]:
    """For each ratio: cap every CPU of the group, load them all, sample ``read_perf(group.rep)`` (the ``0x198`` word) ``samples`` times, stop the load.

    Only samples whose running ratio ``[15:8]`` equals the target are kept (the ratio wavers under a single-thread load); the VID range is over those, and the
    result is ``ok`` if at least ``min_valid`` were kept. Cap search: ``ratio x 100 MHz`` first; if the ratio settles *below* the target, retry with
    ``(ratio + 1) x 100 MHz`` (never above the CPUs' ``cpuinfo_max_freq``); if it runs *above*, or still not enough samples match, the result is ``MISMATCH`` without
    a voltage. A ratio whose own frequency (``ratio x 100 MHz``) is above the CPUs' current ``scaling_max_freq`` is skipped with a note. **The original caps are
    always restored** (``finally``, so also on an exception and Ctrl-C); a restore that fails is appended to ``problems`` (never swallowed).
    ``on_result`` (G-111) is called with every :class:`RatioMeasurement` as it is appended (the helper's live ``vf.point`` events)."""
    originals = save_caps(io, group.cpus)
    ceiling = min(mx for _, mx in originals.values())
    max_khz = min(io.read_max_khz(cpu) for cpu in group.cpus)
    results: list[RatioMeasurement] = []

    def add(m: RatioMeasurement) -> None:
        results.append(m)
        if on_result is not None:
            on_result(m)

    try:
        for ratio in group.ratios:
            nominal = ratio * 100_000
            if nominal > max_khz:  # the core cannot run it at all (cpuinfo_max_freq)
                add(RatioMeasurement(ratio, "skipped", None, note=f"{ratio}x ({nominal // 1000} MHz) is above the current scaling_max_freq ({min(ceiling, max_khz) // 1000} MHz)", why="below"))
                continue
            if nominal > ceiling:  # G-132: the user's own scaling_max_freq is below it
                add(RatioMeasurement(ratio, "skipped", None, note=f"{ratio}x ({nominal // 1000} MHz) is above the current scaling_max_freq ({ceiling // 1000} MHz)", why="user_cap"))
                continue
            attempts = caps_to_try(ratio, max_khz)
            for n, cap in enumerate(attempts):
                if progress is not None:
                    progress(f"measuring {group.name} at {ratio}x (scaling_max_freq {cap // 1000} MHz on CPUs {','.join(map(str, group.cpus))})")
                if ratio in group.intermittent:
                    words, temps = _sample(io, group, cap, read_perf, sleep, settle_s, max(samples, LONG_SAMPLES), min(interval_s, LONG_INTERVAL_S), (ratio, min_valid))
                else:
                    words, temps = _sample(io, group, cap, read_perf, sleep, settle_s, samples, interval_s)
                running = [(w >> 8) & 0xFF for w in words]
                seen = tuple(sorted(set(running)))
                kept = [w for w, r in zip(words, running) if r == ratio]
                t_lo, t_hi = (min(temps), max(temps)) if temps else (None, None)
                if len(kept) >= min_valid:
                    vids = [((w >> 32) & 0xFFFF) / 8192 for w in kept]
                    note = f"cap {cap // 1000} MHz (ratio + 1 bin)" if cap != nominal else ""
                    add(RatioMeasurement(ratio, "ok", cap, min(vids), max(vids), seen, note, t_lo, t_hi, len(kept), len(words), "", sum(vids) / len(vids)))
                    break
                below = sum(r < ratio for r in running)
                above = sum(r > ratio for r in running)
                if below > above and n + 1 < len(attempts):
                    continue  # settled below the target: try the next cap
                why = "ran above the target" if above >= below and above else "settled below the target"
                note = f"{len(kept)} of {len(words)} samples at {ratio}x (need {min_valid}), running ratio {seen[0]}..{seen[-1]}x, {why} with cap {cap // 1000} MHz: no voltage reported"
                add(RatioMeasurement(ratio, "MISMATCH", cap, None, None, seen, note, t_lo, t_hi, len(kept), len(words), "below" if seen[-1] < ratio else "above"))
                break
    finally:
        restore_caps(io, originals, problems)
    return results


#: G-131: cap offsets (kHz) around ``ratio x 100 MHz`` that :func:`search_cap` tries, nearest first, the higher side first (the kernel converts the cap to HWP perf units with the
#: hybrid scaling factor, so a perf step that no multiple of 100 MHz selects can be the one that runs the target). The two neighbours (+-100 MHz) are the nominal attempts.
CAP_SEARCH_OFFSETS_KHZ = (25_000, -25_000, 50_000, -50_000, 75_000, -75_000, 99_000, -99_000)


def search_cap(
    io: MeasureIO,
    group: MeasureGroup,
    ratio: int,
    read_perf: Callable[[int], int],
    sleep: Callable[[float], object],
    problems: list[str],
    *,
    settle_s: float = SETTLE_S,
    samples: int = SAMPLES,
    interval_s: float = SAMPLE_INTERVAL_S,
    min_valid: int = MIN_VALID,
    offsets: Sequence[int] = CAP_SEARCH_OFFSETS_KHZ,
    seen_out: set[int] | None = None,
    budget_s: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RatioMeasurement | None:
    """G-131: one more try for a ``ratio`` that the nominal caps (``ratio`` / ``ratio + 1`` x 100 MHz) never ran: write each cap of ``offsets`` around ``ratio x 100 MHz`` to every
    CPU of the group, read ``scaling_max_freq`` back, **skip a cap whose read-back equals one already tried** (the kernel's perf rounding makes neighbouring values the same cap),
    sample, and stop at the first cap that gives ``min_valid`` samples at the target. Returns that :class:`RatioMeasurement` (``cap_khz`` = the read-back value in force) or ``None``.
    Never above the CPUs' ``cpuinfo_max_freq`` or their current ``scaling_max_freq``; a cap the kernel refuses is skipped. The original caps are always restored (``finally``).
    G-132: ``seen_out`` collects every running ratio the sampled caps gave (the "nearest ratios" of a point no cap reaches); ``budget_s`` bounds the time (by ``clock``): no new
    cap is sampled when the next attempt would end past it."""
    originals = save_caps(io, group.cpus)
    limit = min(min(mx for _, mx in originals.values()), min(io.read_max_khz(cpu) for cpu in group.cpus))
    nominal = ratio * 100_000
    started = clock()
    attempt_s = settle_s + samples * interval_s
    tried: set[int] = set()
    found: RatioMeasurement | None = None

    def put(cap: int) -> int | None:
        """Write ``cap`` to the group and return what the rep CPU reads back (``None``: refused)."""
        try:
            for cpu in group.cpus:
                set_cap(io, cpu, cap)
            return io.read_cap(group.rep)
        except (OSError, ValueError):
            return None

    try:
        for cap in caps_to_try(ratio, limit):  # the nominal attempts were made by measure_group: only learn what they read back as
            back = put(cap)
            if back is not None:
                tried.add(back)
        for off in offsets:
            cap = nominal + off
            if cap > limit or cap < 100_000:
                continue
            back = put(cap)
            if back is None or back in tried or back > limit:
                continue
            if budget_s is not None and clock() - started + attempt_s > budget_s:
                break
            tried.add(back)
            words, temps = _sample(io, group, back, read_perf, sleep, settle_s, samples, interval_s)
            running = [(w >> 8) & 0xFF for w in words]
            if seen_out is not None:
                seen_out.update(running)
            kept = [w for w, r in zip(words, running) if r == ratio]
            if len(kept) >= min_valid:
                vids = [((w >> 32) & 0xFFFF) / 8192 for w in kept]
                t_lo, t_hi = (min(temps), max(temps)) if temps else (None, None)
                found = RatioMeasurement(ratio, "ok", back, min(vids), max(vids), tuple(sorted(set(running))), f"cap {back // 1000} MHz (cap search)", t_lo, t_hi,
                                         len(kept), len(words), "", sum(vids) / len(vids))
                break
    finally:
        restore_caps(io, originals, problems)
    return found


class _Load:
    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc

    def stop(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()


class SysfsMeasureIO:
    """The real :class:`MeasureIO`: cpufreq ``scaling_max_freq`` under ``root`` and ``stress-ng`` (``--cpu n --taskset cpus``). **Only the CLI's ``--device`` path
    builds one**; tests pass a temp ``root`` and fake ``popen`` / ``which``. cpufreq limits are an ordinary OS setting, never an MSR."""

    def __init__(
        self,
        root: Path = Path("/sys/devices/system/cpu"),
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        which: Callable[[str], str | None] | None = None,
        hwmon_root: Path = Path("/sys/class/hwmon"),
    ) -> None:
        self._hwmon = hwmon_root
        self._root = root
        self._popen = popen
        self._which = which if which is not None else shutil.which

    def _path(self, cpu: int, attr: str = "scaling_max_freq") -> Path:
        return self._root / f"cpu{cpu}" / "cpufreq" / attr

    def check(self) -> str | None:
        return None if self._which("stress-ng") else "stress-ng was not found on PATH (needed to load the cores for --measure)"

    def read_cap(self, cpu: int) -> int:
        return int(self._path(cpu).read_text(encoding="ascii").strip())

    def write_cap(self, cpu: int, khz: int) -> None:
        self._path(cpu).write_text(str(int(khz)), encoding="ascii")

    def read_min(self, cpu: int) -> int | None:
        """``scaling_min_freq`` in kHz, ``None`` when the file is not there (G-132: then a cap is the max alone)."""
        try:
            return int(self._path(cpu, "scaling_min_freq").read_text(encoding="ascii").strip())
        except FileNotFoundError:
            return None

    def write_min(self, cpu: int, khz: int) -> None:
        self._path(cpu, "scaling_min_freq").write_text(str(int(khz)), encoding="ascii")

    def read_max_khz(self, cpu: int) -> int:
        return int((self._root / f"cpu{cpu}" / "cpufreq" / "cpuinfo_max_freq").read_text(encoding="ascii").strip())

    def read_temp_c(self, cpu: int) -> float | None:
        """coretemp hwmon: ``hwmon*/name == "coretemp"``, ``temp*_label == "Core <core_id>"`` (``cpuN/topology/core_id``), ``temp*_input`` in millidegrees."""
        try:
            label = f"Core {int((self._root / f'cpu{cpu}' / 'topology' / 'core_id').read_text(encoding='ascii').strip())}"
            for hwmon in sorted(self._hwmon.glob("hwmon*")):
                if (hwmon / "name").read_text(encoding="ascii").strip() != "coretemp":
                    continue
                for label_file in sorted(hwmon.glob("temp*_label")):
                    if label_file.read_text(encoding="ascii").strip() == label:
                        return int(label_file.with_name(label_file.name.replace("_label", "_input")).read_text(encoding="ascii").strip()) / 1000
        except (OSError, ValueError):
            return None
        return None

    def start_load(self, cpus: Sequence[int], seconds: float) -> _Load:
        cmd = ["stress-ng", "--cpu", str(len(cpus)), "--taskset", ",".join(map(str, cpus)), "--timeout", f"{max(1, math.ceil(seconds))}s", "--quiet"]
        return _Load(self._popen(cmd))


# --------------------------------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------------------------------
def _offset_text(raw: int) -> str:
    return f"{raw * 1000 / 1024:+.1f} mV (raw {raw})"


def _temp_text(m: RatioMeasurement) -> str:
    """`` @ 62 C`` (or `` @ 61..63 C``); `` @ n/a`` when there was no sensor reading."""
    if m.temp_min_c is None or m.temp_max_c is None:
        return " @ n/a"
    lo, hi = f"{m.temp_min_c:.0f}", f"{m.temp_max_c:.0f}"
    return f" @ {lo} \u00b0C" if lo == hi else f" @ {lo}..{hi} \u00b0C"


def _kept_text(m: RatioMeasurement) -> str:
    """`` [12/30 at target]`` when some samples were not at the target ratio (empty if all were or there were none)."""
    return f" [{m.samples_kept}/{m.samples_total} at target]" if m.samples_total and m.samples_kept != m.samples_total else ""


def _vid_text(m: RatioMeasurement | None) -> str:
    if m is None:
        return "-"
    if m.status == "skipped":
        return "skipped"
    if m.status == "MISMATCH":
        return f"MISMATCH ({m.seen_ratios[0]}..{m.seen_ratios[-1]}x)" + _temp_text(m) + _kept_text(m)
    assert m.vid_min is not None and m.vid_max is not None
    lo, hi = f"{m.vid_min:.4f}", f"{m.vid_max:.4f}"
    return (f"{lo} V" if lo == hi else f"{lo}..{hi} V") + _temp_text(m) + _kept_text(m)


def _oc_text(word: int) -> str:
    vf = VfWord.from_u32(word)
    target = "0 (tracks the top fused point)" if vf.v_target_raw == 0 else f"{vf.v_target_v:.3f} V (raw {vf.v_target_raw:#05x})"
    return f"ratio {vf.ratio}x, V target {target}, {'static (override)' if vf.static else 'adaptive'}, offset {_offset_text(vf.offset_raw)}"


def _measurements_by_ratio(measured: Mapping[str, Sequence[RatioMeasurement]], unit: Unit) -> dict[int, RatioMeasurement]:
    return {m.ratio: m for m in measured.get(unit.name, ())}


def format_dump(units: Sequence[Unit], flags: Sequence[Flag], measured: Mapping[str, Sequence[RatioMeasurement]] | None, stop: Stop | None) -> list[str]:
    """The readable table per P-core / E-cluster, the flagged differences and (if the dump was aborted) the stop reason."""
    lines: list[str] = []
    do_measure = measured is not None
    for unit in units:
        read_cpus = ", ".join(str(c.cpu) for c in unit.curves)
        lines.append(f"{unit.name}   [read CPU {read_cpus}]")
        by_ratio = _measurements_by_ratio(measured or {}, unit)
        head = unit.curves[0]
        cols = unit.curves
        offset_head = "offset (mV, raw)" if len(cols) == 1 else "  ".join(f"{'cpu ' + str(c.cpu):<19}" for c in cols)
        lines.append(f"  {'point':<6}{'ratio':>6}  {offset_head}" + (f"  {'measured VID @ core temp':<28}" if do_measure else "").rstrip())
        rows: list[tuple[str, int | None]] = [("OC", 0)] + [(str(n), n) for n in range(1, max(len(c.points) for c in cols) + 1)]
        for label, p2 in rows:
            assert p2 is not None
            words = [_word_at(c, p2) for c in cols]
            if words[0] is None and all(w is None for w in words):
                continue
            first = VfWord.from_u32(next(w for w in words if w is not None))
            cells = ["" if w is None else _offset_text(VfWord.from_u32(w).offset_raw) for w in words]
            offset_cells = "  ".join(f"{cell:<19}" for cell in cells) if len(cols) > 1 else cells[0]
            vid = ""
            if do_measure:
                vid = "  " + _vid_text(by_ratio.get(first.ratio))
            lines.append(f"  {label:<6}{str(first.ratio) + 'x':>6}  {offset_cells}{vid}".rstrip())
        if head.oc is not None:
            lines.append(f"  OC word {head.oc:#010x}: {_oc_text(head.oc)}")
        lines.append(f"  table end: {head.end}")
        for ratio, m in sorted(by_ratio.items()):
            if m.note:
                lines.append(f"  note {ratio}x: {m.note}")
        lines.append("")
    if flags:
        lines.append("DIFFERENCES between P-cores / between the CPUs of one E-cluster (tables are per core: the favored P-cores 0 and 3 may boost higher):")
        lines += [f"  {f.text}" for f in flags]
    elif len(units) > 1:
        lines.append("No offset or ratio differences between the P-cores / the CPUs of one E-cluster that were read.")
    if stop is not None:
        lines.append(f"STOPPED: {stop.text}")
    return lines


def to_json(
    units: Sequence[Unit],
    flags: Sequence[Flag],
    measured: Mapping[str, Sequence[RatioMeasurement]] | None,
    stop: Stop | None,
    *,
    sim: bool,
    problems: Sequence[str] = (),
) -> dict[str, Any]:
    """The same data as :func:`format_dump`, machine-readable (every word kept raw)."""

    def word(w: int) -> dict[str, Any]:
        vf = VfWord.from_u32(w)
        return {
            "word": f"{w:#010x}",
            "ratio": vf.ratio,
            "v_target_raw": vf.v_target_raw,
            "v_target_v": vf.v_target_v,
            "static": vf.static,
            "offset_raw": vf.offset_raw,
            "offset_mv": round(vf.offset_mv, 3),
        }

    out_units: list[dict[str, Any]] = []
    for unit in units:
        out_units.append(
            {
                "name": unit.name,
                "kind": unit.kind,
                "cluster": unit.cluster,
                "cluster_cpus": list(unit.cluster_cpus),
                "cpus": [
                    {
                        "cpu": c.cpu,
                        "name": c.name,
                        "oc": None if c.oc is None else word(c.oc),
                        "points": [{"point": n, **word(w)} for n, w in enumerate(c.points, start=1)],
                        "end": c.end,
                    }
                    for c in unit.curves
                ],
                "measured": None
                if measured is None
                else [
                    {
                        "ratio": m.ratio,
                        "status": m.status,
                        "cap_khz": m.cap_khz,
                        "vid_min_v": m.vid_min,
                        "vid_max_v": m.vid_max,
                        "temp_min_c": m.temp_min_c,
                        "temp_max_c": m.temp_max_c,
                        "samples_kept": m.samples_kept,
                        "samples_total": m.samples_total,
                        "seen_ratios": list(m.seen_ratios),
                        "note": m.note,
                    }
                    for m in measured.get(unit.name, ())
                ],
            }
        )
    return {
        "simulator": sim,
        "complete": stop is None,
        "stopped": None if stop is None else {"code": stop.code, "text": stop.text},
        "units": out_units,
        "differences": [{"scope": f.scope, "point": f.point, "field": f.field, "values": {str(k): v for k, v in f.values.items()}, "text": f.text} for f in flags],
        "problems": list(problems),
    }
