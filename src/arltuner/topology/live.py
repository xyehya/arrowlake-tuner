"""Live topology (D-036): every CPU-specific fact is read from sysfs, nothing about a particular SKU is stored.

Pure functions over an injectable sysfs root (tests pass a fake directory; the real ``/sys`` is the default). No hardware access beyond reading the
small sysfs files below, no other ``arltuner`` imports than ``topology.cpumap``.

* P CPUs  = ``devices/cpu_core/cpus``, E CPUs = ``devices/cpu_atom/cpus`` (kernel cpulist syntax),
* E clusters = the distinct ``devices/system/cpu/cpu<N>/topology/cluster_cpus_list`` of the E CPUs, numbered in order of their first CPU,
* ``cpufreq/cpuinfo_max_freq`` (kHz) / 100000 = ``max_ratio`` of a CPU (a unit takes the maximum over its CPUs),
* ``acpi_cppc/highest_perf`` = ``perf`` (unit: maximum over its CPUs),
* **favored** = the P units with the highest ``perf`` (fallback: ``max_ratio``); none when all P units are equal. ``favored_source`` names the
  value that decided it. ``0x1C[7:0]`` of a ``tune.read`` is a cross-check done by the caller, never an input here.

Unit ids: ``P<n>`` (n = ordinal among the P CPUs) and ``EC<n>`` (n = ordinal among the E clusters). Arrow Lake-S has no SMT: one CPU per core.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cpumap import KIND_E, KIND_P, Core, Topology, TopologyMismatch, _read_cpulist

__all__ = ["LiveTopology", "LiveUnit", "contract_dict", "read_live", "topology_problems", "unit_table"]

_CPU_DIR = Path("devices") / "system" / "cpu"
KHZ_PER_RATIO = 100_000  # a bus-clock ratio is 100 MHz (architecture fact valid for every Arrow Lake-S part)


@dataclass(frozen=True)
class LiveUnit:
    id: str
    kind: str  # "P" | "E"
    cpus: tuple[int, ...]
    favored: bool
    max_ratio: int | None  # G-233: the ratio the unit's cores really run at the top (an E-core's kernel kHz is one bin high); ``max_khz`` is the kernel's own cpuinfo_max_freq
    perf: int | None
    max_khz: int | None = None
    ratio_model: str | None = None  # "measured" | "unmeasured" (``max_ratio`` is then ``max_khz`` / 100000); None = no maximum read


@dataclass(frozen=True)
class LiveTopology:
    topology: Topology
    units: tuple[LiveUnit, ...]
    favored_source: str | None  # "acpi_cppc" | "cpuinfo_max_freq" | None

    def contract(self) -> dict[str, Any]:
        """The ``preflight`` result's ``topology`` object (G-127 consumes it): ``{units: [{id, kind, cpus, favored, max_ratio, perf, max_khz, ratio_model}], favored_source}``."""
        return {
            "units": [
                {"id": u.id, "kind": u.kind, "cpus": list(u.cpus), "favored": u.favored, "max_ratio": u.max_ratio, "perf": u.perf,
                 "max_khz": u.max_khz, "ratio_model": u.ratio_model}
                for u in self.units
            ],
            "favored_source": self.favored_source,
        }

    def max_ratio_of_kind(self, kind: str) -> int | None:
        """The highest ``max_ratio`` of the units of a kind (the live hardware ratio limit of that kind), or None when no unit has one."""
        values = [u.max_ratio for u in self.units if u.kind == kind and u.max_ratio is not None]
        return max(values) if values else None


def _read_int(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="ascii").strip()
        value = int(text, 10)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return value if value >= 0 else None


def _cpu_dir(root: Path, cpu: int) -> Path:
    return root / _CPU_DIR / f"cpu{cpu}"


def _max_ratio(root: Path, cpu: int) -> int | None:
    khz = _read_int(_cpu_dir(root, cpu) / "cpufreq" / "cpuinfo_max_freq")
    return None if not khz else khz // KHZ_PER_RATIO


def _perf(root: Path, cpu: int) -> int | None:
    perf = _read_int(_cpu_dir(root, cpu) / "acpi_cppc" / "highest_perf")
    return perf or None


def _clusters(root: Path, e_cpus: frozenset[int]) -> list[tuple[int, ...]]:
    """The E clusters, ordered by their first CPU. Every E CPU must report the same non-empty cluster list as the CPUs it names."""
    by_cpu: dict[int, frozenset[int]] = {}
    for cpu in sorted(e_cpus):
        by_cpu[cpu] = _read_cpulist(_cpu_dir(root, cpu) / "topology" / "cluster_cpus_list", f"cpu{cpu} cluster_cpus_list")
    clusters: dict[frozenset[int], None] = {}
    for cpu, members in by_cpu.items():
        if cpu not in members or not members <= e_cpus:
            raise TopologyMismatch(f"cpu{cpu} cluster_cpus_list {sorted(members)} is not a set of E CPUs containing cpu{cpu}")
        if any(by_cpu[m] != members for m in members):
            raise TopologyMismatch(f"cpu{cpu} cluster_cpus_list {sorted(members)} disagrees with the lists of its members")
        clusters.setdefault(members, None)
    return sorted((tuple(sorted(c)) for c in clusters), key=lambda c: c[0])


def read_live(root: Path = Path("/sys")) -> LiveTopology:
    """Derive the topology from sysfs under ``root``. ``TopologyMismatch`` for a missing/malformed file, an empty or overlapping P/E set, or a cluster list
    that does not partition the E CPUs. Optional facts (``cpuinfo_max_freq``, ``highest_perf``) that cannot be read are ``None``, never guessed."""
    root = Path(root)
    p_set = _read_cpulist(root / "devices" / "cpu_core" / "cpus", "cpu_core")
    e_set = _read_cpulist(root / "devices" / "cpu_atom" / "cpus", "cpu_atom")
    if p_set & e_set:
        raise TopologyMismatch(f"cpu_core/cpus and cpu_atom/cpus overlap: {sorted(p_set & e_set)}")
    clusters = _clusters(root, e_set)

    p_cpus = sorted(p_set)
    max_ratio = {cpu: _max_ratio(root, cpu) for cpu in sorted(p_set | e_set)}
    perf = {cpu: _perf(root, cpu) for cpu in sorted(p_set | e_set)}

    # ---- favored: the P units with the highest perf (all P CPUs must have one), else the highest max_ratio; none when all are equal ----
    favored_cpus: set[int] = set()
    source: str | None = None
    for key, name in ((perf, "acpi_cppc"), (max_ratio, "cpuinfo_max_freq")):
        values = {cpu: key[cpu] for cpu in p_cpus}
        if any(v is None for v in values.values()):
            continue
        top = max(values.values())  # type: ignore[type-var]
        if len(set(values.values())) > 1:
            favored_cpus = {cpu for cpu, v in values.items() if v == top}
            source = name
        break  # the first source that exists decides, even when it finds all P-cores equal (then there is no favored core)

    cores: list[Core] = [
        Core(KIND_P, n, cpu, None, cpu in favored_cpus, max_ratio[cpu], perf[cpu]) for n, cpu in enumerate(p_cpus)
    ]
    cluster_of = {cpu: ordinal for ordinal, members in enumerate(clusters) for cpu in members}
    for k, cpu in enumerate(sorted(e_set)):  # E index = ordinal among the E CPUs, ascending
        cores.append(Core(KIND_E, k, cpu, cluster_of[cpu], False, max_ratio[cpu], perf[cpu]))
    topology = Topology(tuple(cores), favored_source=source)
    return LiveTopology(topology, unit_table(topology), source)


def unit_table(topology: Topology) -> tuple[LiveUnit, ...]:
    """The units of a topology (P by index, then E clusters) with their facts; works for live and simulated topologies alike."""
    from arltuner.controls.caps import effective_ratio  # lazy: controls.caps imports this package

    units: list[LiveUnit] = []
    for uid in topology.unit_ids():
        cores = [topology.by_cpu(cpu) for cpu in topology.unit_cpus(uid)]
        ratios = [c.max_ratio for c in cores if c.max_ratio is not None]
        perfs = [c.perf for c in cores if c.perf is not None]
        raw = max(ratios) if ratios else None
        max_khz = None if not raw else raw * KHZ_PER_RATIO
        effective, model = (None, None) if max_khz is None else effective_ratio(cores[0].kind, max_khz, max_khz)
        units.append(LiveUnit(uid, cores[0].kind, tuple(c.cpu for c in cores), any(c.favored for c in cores), raw if effective is None else effective, max(perfs) if perfs else None, max_khz, model))
    return tuple(units)


def contract_dict(topology: Topology) -> dict[str, Any]:
    """The ``preflight`` ``topology`` object for any ``Topology`` (live or simulated)."""
    return LiveTopology(topology, unit_table(topology), topology.favored_source).contract()


def topology_problems(topology: Topology) -> list[str]:
    """Why a topology is not a usable Arrow Lake-S part (empty P or E set, E clusters of unequal size); empty list = consistent."""
    problems: list[str] = []
    if not topology.cpus_of_kind(KIND_P):
        problems.append("no P-core CPUs (devices/cpu_core/cpus is empty)")
    if not topology.cpus_of_kind(KIND_E):
        problems.append("no E-core CPUs (devices/cpu_atom/cpus is empty)")
    sizes: dict[int, int] = {}
    for c in topology.cores:
        if c.cluster is not None:
            sizes[c.cluster] = sizes.get(c.cluster, 0) + 1
    if len(set(sizes.values())) > 1:
        problems.append("E clusters have unequal sizes: " + ", ".join(f"EC{k}={n}" for k, n in sorted(sizes.items())))
    return problems
