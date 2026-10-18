"""G-216: per-CPU maximum-ratio caps (``cpufreq/scaling_max_freq``), plus the pieces the runtime controls (caps, uncore) share -- the sysfs file access, the step, the
refusal. Pure apart from the files it is given. (Core parking is held out of G-216: a parked CPU renumbers the live topology, see ``ops_runtime.refuse_if_cpu_offline``.)

Facts (``[MEASURED lnx 2026-10-18]``): intel_pstate is active (HWP); ``cpu<N>/cpufreq/cpuinfo_max_freq`` of cpu0 is 5700000 kHz; ``scaling_max_freq`` is the user's cap. The XTU
analogue is a per-core max-ratio: effective ratio = min(turbo table, cap). A P-core cap is ratio x 100000 kHz. An E-core ratio goes through the caller's ``khz_of`` (the kernel's own
perf map, ``ops_stress._cap_khz`` fed with the unit's fused top ratio, G-152: E 46x needs 4.7 GHz, not 4.6 ``[MEASURED lnx 2026-10-14]``); the kernel rounds an E cap to a perf step, so an E
read-back is judged within ``E_TOLERANCE_KHZ`` of the value written.

The four E-cores of a cluster run one ratio, so a cap on one of them does not hold: a cap on an E CPU is written to all CPUs of its cluster (topology from the live read,
never hard-coded; the G-152 rule). Nothing here has a default floor: ``policy.runtime.cap_ratio_min`` says how low a cap may go and an absent key refuses naming itself; the
ceiling is always the CPU's own ``cpuinfo_max_freq``.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arltuner.guard.policy import Policy
from arltuner.topology import Topology, UnknownCore

__all__ = [
    "CAP_ATTR",
    "CPU_ROOT",
    "E_TOLERANCE_KHZ",
    "JOURNAL_KHZ_MAX",
    "KHZ_PER_RATIO",
    "MAX_ATTR",
    "MIN_ATTR",
    "CpuFreq",
    "Refused",
    "RuntimeFiles",
    "RuntimeStep",
    "SysfsError",
    "plan",
    "plan_revert",
    "read_state",
    "readback_problem",
    "require_runtime_key",
]

CPU_ROOT = Path("/sys/devices/system/cpu")
CAP_ATTR = "scaling_max_freq"
MIN_ATTR = "scaling_min_freq"
MAX_ATTR = "cpuinfo_max_freq"
KHZ_PER_RATIO = 100_000
#: the largest frequency a journal may hold (kHz): 25.5 GHz = ratio 255, the byte the policy's ratio fields are bounded by
JOURNAL_KHZ_MAX = 255 * KHZ_PER_RATIO
#: an E-core's ``scaling_max_freq`` reads back the perf step the kernel rounded to (at most half a step ~ 36 MHz off): within this of the value written is the same cap
E_TOLERANCE_KHZ = 50_000


class SysfsError(Exception):
    """A runtime sysfs file cannot be read / written."""


class Refused(SysfsError):
    """The plan or the write is refused. ``check`` names the reason (``RUNTIME_POLICY_MISSING`` ...), ``key`` the ``[runtime]`` key / intent item it is about."""

    def __init__(self, check: str, text: str, key: str | None = None) -> None:
        super().__init__(text)
        self.check = check
        self.key = key


class RuntimeFiles:
    """The sysfs files of the runtime controls under ``root`` (``/sys/devices/system/cpu``); names are relative (``cpu8/cpufreq/scaling_max_freq``, ``cpu8/online``,
    ``intel_uncore_frequency/package_00_die_00/min_freq_khz``). ``read_text`` of an absent file gives ``None``."""

    def __init__(self, root: Path | str = CPU_ROOT) -> None:
        self.root = Path(root)

    def path(self, name: str) -> Path:
        return self.root / name

    def read_text(self, name: str) -> str | None:
        try:
            return self.path(name).read_text(encoding="ascii").strip()
        except (FileNotFoundError, NotADirectoryError):
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise SysfsError(f"cannot read {self.path(name)}: {getattr(exc, 'strerror', None) or exc}") from None

    def read_int(self, name: str) -> int | None:
        text = self.read_text(name)
        if text is None:
            return None
        try:
            return int(text, 10)
        except ValueError:
            raise SysfsError(f"{self.path(name)} holds {text[:40]!r}, not a number") from None

    def listdir(self, name: str) -> list[str]:
        try:
            return sorted(os.listdir(self.path(name)))
        except (FileNotFoundError, NotADirectoryError):
            return []
        except OSError as exc:
            raise SysfsError(f"cannot list {self.path(name)}: {exc.strerror or exc}") from None

    def write_int(self, name: str, value: int) -> None:
        """One write of one file (the caller audits before and reads back after)."""
        data = str(int(value)).encode("ascii")
        try:
            fd = os.open(self.path(name), os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))  # the leaf is a kernel attribute file, never a link
            try:
                if os.write(fd, data) != len(data):  # a sysfs attribute takes its whole value in one write()
                    raise OSError("short write")
            finally:
                os.close(fd)
        except OSError as exc:
            raise SysfsError(f"cannot write {self.path(name)}: {exc.strerror or exc}") from None


@dataclass(frozen=True)
class RuntimeStep:
    """One sysfs file write. ``domain`` is ``caps`` / ``uncore``; ``key`` the intent item (``cpu8``, ``min_ratio``); ``file`` the name relative to the root; ``before`` /
    ``value`` in the file's unit (kHz); ``expect`` the ``(low, high)`` a read-back may show -- both ends equal ``value``: exact."""

    number: int
    domain: str
    key: str
    file: str
    before: int
    value: int
    expect: tuple[int, int]

    def as_json(self) -> dict[str, Any]:
        return {"n": self.number, "domain": self.domain, "key": self.key, "file": self.file, "before": self.before, "value": self.value, "expect": list(self.expect)}


def require_runtime_key(policy: Policy, key: str) -> Any:
    """The value of ``[runtime] key``, or :class:`Refused` naming the key (the operator's next step is the policy file)."""
    where = policy.where
    if policy.runtime is None:
        raise Refused("RUNTIME_POLICY_MISSING", f"the policy has no [runtime] table ({where}): this control may not be used. Add [runtime] {key} to the policy to allow it", key)
    value = getattr(policy.runtime, key)
    if value is None:
        raise Refused("RUNTIME_POLICY_MISSING", f"the policy has no [runtime] {key} ({where}): this control may not be used. Add [runtime] {key} to the policy to allow it", key)
    return value


def readback_problem(step: RuntimeStep, file_value: int | None) -> str | None:
    """``None`` when the write took (the file reads back what ``step.expect`` allows -- exactly ``step.value``), else the reason (a mismatch trips the apply)."""
    if file_value is None:
        return f"{step.file} cannot be read back"
    lo, hi = step.expect
    if not lo <= file_value <= hi:
        return f"{step.file} reads {file_value}, expected {lo if lo == hi else f'{lo} to {hi}'}"
    return None


# --------------------------------------------------------------------------------------------------------------
# the cpufreq files of a CPU
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CpuFreq:
    """What the CPU's cpufreq directory shows (kHz): its own maximum, the user cap, the user floor (``None`` = file absent)."""

    max_khz: int
    cap_khz: int
    min_khz: int | None = None

    def as_json(self) -> dict[str, Any]:
        return {"cpuinfo_max_khz": self.max_khz, "cap_khz": self.cap_khz, "min_khz": self.min_khz, "cap_ratio": self.cap_khz / KHZ_PER_RATIO}


def cap_file(cpu: int) -> str:
    return f"cpu{cpu}/cpufreq/{CAP_ATTR}"


def read_state(files: RuntimeFiles, cpus: Sequence[int]) -> dict[int, CpuFreq | None]:
    """``{cpu: CpuFreq}`` of every CPU in ``cpus`` that has the cpufreq files; ``None`` where there are none (an offline CPU). ``SysfsError`` for an unreadable file."""
    out: dict[int, CpuFreq | None] = {}
    for cpu in cpus:
        top, cap = files.read_int(f"cpu{cpu}/cpufreq/{MAX_ATTR}"), files.read_int(cap_file(cpu))
        out[cpu] = None if top is None or cap is None else CpuFreq(top, cap, files.read_int(f"cpu{cpu}/cpufreq/{MIN_ATTR}"))
    return out


# --------------------------------------------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------------------------------------------
def _ratio(key: object, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise Refused("CAPS_VALUE", f"caps.{key} must be a whole ratio (an integer >= 1; 1 ratio = 100 MHz)", f"cpu{key}")
    return value


def expand(intent: Mapping[int, int], topology: Topology) -> dict[int, int]:
    """``intent`` with every E CPU's cluster added (G-152): ``{cpu: ratio}`` of all CPUs to be written. Two CPUs of one cluster asking for different ratios is
    :class:`Refused` (``CAPS_CLUSTER_CONFLICT``); an unknown CPU is ``CAPS_UNKNOWN_CPU``."""
    known = {c.cpu for c in topology.cores}
    out: dict[int, int] = {}
    asked_by: dict[int, int] = {}
    for cpu in sorted(intent):
        if cpu not in known:
            raise Refused("CAPS_UNKNOWN_CPU", f"caps names cpu {cpu}, which this system does not have (CPUs: {min(known)}..{max(known)})", f"cpu{cpu}")
        ratio = _ratio(cpu, intent[cpu])
        core = topology.by_cpu(cpu)
        members = topology.cluster_cpus(core.cluster) if core.cluster is not None else (cpu,)
        for member in members:
            if member in out and out[member] != ratio:
                raise Refused(
                    "CAPS_CLUSTER_CONFLICT",
                    f"caps asks cpu {asked_by[member]} for ratio {out[member]} and cpu {cpu} for ratio {ratio}, but they are in one E cluster "
                    f"({', '.join(map(str, members))}) which runs one ratio: ask one ratio per cluster",
                    f"cpu{cpu}",
                )
            out[member] = ratio
            asked_by.setdefault(member, cpu)
    return out


def _tolerance(topology: Topology, cpu: int) -> int:
    try:
        return E_TOLERANCE_KHZ if topology.by_cpu(cpu).kind == "E" else 0
    except UnknownCore:
        return 0


def _same(a: int, b: int, tolerance: int) -> bool:
    return a == b if tolerance == 0 else abs(a - b) < tolerance


def _step(number: int, cpu: int, before: int, value: int, tolerance: int) -> RuntimeStep:
    return RuntimeStep(number, "caps", f"cpu{cpu}", cap_file(cpu), before, value, (value - tolerance, value + tolerance))


def plan(
    intent: Mapping[int, int], policy: Policy, state: Mapping[int, CpuFreq | None], topology: Topology, *, held: str | None = None,
    unit_top: Mapping[str, int] | None = None, khz_of: Callable[[int, int | None, int | None], int] | None = None,
) -> list[RuntimeStep]:
    """The write steps for ``intent`` (``{cpu: ratio}``) over the caps in force, or :class:`Refused` -- nothing is written by planning.

    Checks, in order (each refusal names its check): the policy key ``cap_ratio_min`` (``RUNTIME_POLICY_MISSING``), a measure / stress run or journal that owns the caps (``CAPS_CONFLICT``:
    ``held`` is its name), unknown CPUs and the E-cluster expansion (``CAPS_UNKNOWN_CPU`` / ``CAPS_CLUSTER_CONFLICT``), then per CPU (the expansion's too): not in the policy's ``cpus`` (``CAPS_CPU_NOT_ALLOWED``), no cpufreq
    directory (``CAPS_NO_FILE``: an offline CPU), the floor ``cap_ratio_min`` and the ceiling (``CAPS_RANGE``), the CPU's own ``scaling_min_freq`` (``CAPS_BELOW_MIN``).
    The ceiling is the CPU's own ``cpuinfo_max_freq`` and, for an E CPU whose unit's fused top ratio is in ``unit_top``, that ratio. ``khz_of(ratio, top, cpuinfo_khz)`` is the E-core kHz
    (``ops_stress._cap_khz``); without it, or a P CPU, a ratio is ratio x 100 MHz. Steps that change nothing are dropped.
    """
    if not intent:
        raise Refused("NOTHING_TO_DO", "the caps intent changes nothing (set caps {cpu: ratio})")
    floor = require_runtime_key(policy, "cap_ratio_min")
    if held is not None:
        raise Refused("CAPS_CONFLICT", f"{held} holds the CPU frequency caps: caps may not be changed until it is finished or restored")
    targets = expand(intent, topology)
    steps: list[RuntimeStep] = []
    for cpu in sorted(targets):
        ratio = targets[cpu]
        key = f"cpu{cpu}"
        if not policy.cpu_allowed(cpu):  # the policy's CPU allowlist also bounds the CPUs an E-cluster expansion adds
            raise Refused("CAPS_CPU_NOT_ALLOWED", f"cpu {cpu} is not in the policy's cpus ({policy.where}): its frequency cap may not be changed", key)
        freq = state.get(cpu)
        if freq is None:
            raise Refused("CAPS_NO_FILE", f"cpu {cpu} has no cpufreq directory (is it offline?)", key)
        tol = _tolerance(topology, cpu)
        top = unit_top.get(topology.unit_of_cpu(cpu)) if tol and unit_top else None
        khz = khz_of(ratio, top, freq.max_khz) if tol and khz_of is not None else ratio * KHZ_PER_RATIO
        if ratio < floor:
            raise Refused("CAPS_RANGE", f"cpu {cpu}: ratio {ratio} is below [runtime] cap_ratio_min = {floor} ({policy.where})", key)
        if top is not None and ratio > top:
            raise Refused("CAPS_RANGE", f"cpu {cpu}: ratio {ratio} is above the maximum ratio of its unit ({top})", key)
        if khz > freq.max_khz:
            raise Refused("CAPS_RANGE", f"cpu {cpu}: ratio {ratio} is above the CPU's own maximum (cpuinfo_max_freq {freq.max_khz} kHz = ratio {freq.max_khz / KHZ_PER_RATIO:g})", key)
        if freq.min_khz is not None and khz < freq.min_khz:
            raise Refused("CAPS_BELOW_MIN", f"cpu {cpu}: ratio {ratio} is below the CPU's scaling_min_freq ({freq.min_khz} kHz): the kernel would refuse it", key)
        if not _same(khz, freq.cap_khz, tol):
            steps.append(_step(len(steps) + 1, cpu, freq.cap_khz, khz, tol))
    return steps


def plan_revert(values: Mapping[int, int], state: Mapping[int, CpuFreq | None], topology: Topology) -> tuple[list[RuntimeStep], list[str]]:
    """The writes that put the journaled caps back (``values`` = ``{cpu: kHz}``): ``(steps, notes)``. The policy does not apply (this restores a recorded state); a CPU without a
    cpufreq directory (offline now) is skipped with a note -- a note means something was *not* restored, and the caller keeps the journal's in-flight mark; a value above the CPU's
    maximum is :class:`Refused`."""
    steps: list[RuntimeStep] = []
    notes: list[str] = []
    for cpu in sorted(values):
        freq = state.get(cpu)
        if freq is None:
            notes.append(f"cpu {cpu} has no cpufreq directory (offline): its cap was not restored")
            continue
        if values[cpu] > freq.max_khz:
            raise Refused("CAPS_RANGE", f"the journaled cap of cpu {cpu} ({values[cpu]} kHz) is above the CPU's own maximum ({freq.max_khz} kHz)", f"cpu{cpu}")
        tol = _tolerance(topology, cpu)
        if not _same(values[cpu], freq.cap_khz, tol):
            steps.append(_step(len(steps) + 1, cpu, freq.cap_khz, values[cpu], tol))
    return steps, notes
