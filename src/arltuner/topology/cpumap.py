"""CPU map of an Arrow Lake-S part: XTU names <-> Linux CPUs <-> E-clusters, derived live (D-036).

Pure Python, standard library only, no hardware access. ``detect`` builds the map from sysfs (``topology.live``) under a caller-supplied
root; nothing about a specific SKU is stored here. The simulated reference CPU used by the simulator and the tests lives in
``topology.reference``.

Naming: XTU "P-Core n" = the n-th P CPU in ascending order; "E-Core k" = the k-th E CPU in ascending order; unit ids are ``P<n>`` and
``EC<n>`` (E clusters numbered in order of their first CPU). Arrow Lake-S has no SMT: one Linux CPU per core.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "KIND_E",
    "KIND_P",
    "KINDS",
    "Core",
    "Topology",
    "TopologyError",
    "TopologyMismatch",
    "UnknownCore",
    "detect",
]

KIND_P = "P"
KIND_E = "E"
KINDS: tuple[str, str] = (KIND_P, KIND_E)

# Kernel cpulist syntax ("0-7", "8-11,12-23"); an upper sanity bound keeps a corrupt file from
# expanding into a huge set (kernel NR_CPUS never exceeds 8192).
_MAX_CPU = 8191
_CPULIST_ITEM = re.compile(r"([0-9]+)(?:-([0-9]+))?")


class TopologyError(Exception):
    """Base class for topology errors."""


class TopologyMismatch(TopologyError):
    """``detect`` could not derive a usable topology (missing, malformed or inconsistent files)."""


class UnknownCore(TopologyError, KeyError, ValueError):
    """A kind/index/cpu/cluster is not part of the topology.

    Subclasses both ``KeyError`` and ``ValueError`` so callers may catch either.
    """

    def __str__(self) -> str:  # KeyError would otherwise show repr() of the message
        return str(self.args[0]) if len(self.args) == 1 else Exception.__str__(self)


@dataclass(frozen=True)
class Core:
    """One physical core (no SMT, so one Linux CPU per core)."""

    kind: str  # "P" | "E"
    index: int  # XTU numbering: ordinal among the P (or E) CPUs, ascending
    cpu: int  # Linux CPU number
    cluster: int | None  # E: ordinal of its cluster (clusters ordered by their first CPU); P: None
    favored: bool
    max_ratio: int | None = field(default=None, compare=False)  # cpufreq/cpuinfo_max_freq / 100000; None = not read
    perf: int | None = field(default=None, compare=False)  # acpi_cppc/highest_perf; None = not read

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"Core.kind must be 'P' or 'E', got {self.kind!r}")
        if self.kind == KIND_P and self.cluster is not None:
            raise ValueError("a P-core has no E-cluster (cluster must be None)")
        if self.kind == KIND_E and self.cluster is None:
            raise ValueError("an E-core must have a cluster number")


@dataclass(frozen=True)
class Topology:
    """Immutable set of cores with lookup helpers. Equality compares ``cores`` only (kind, index, cpu, cluster, favored)."""

    cores: tuple[Core, ...]
    favored_source: str | None = field(default=None, compare=False)  # "acpi_cppc" | "cpuinfo_max_freq" | None (no favored core)
    _by_cpu: dict[int, Core] = field(init=False, repr=False, compare=False)
    _by_key: dict[tuple[str, int], Core] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        cores = tuple(self.cores)
        by_cpu: dict[int, Core] = {}
        by_key: dict[tuple[str, int], Core] = {}
        for core in cores:
            if core.cpu in by_cpu:
                raise ValueError(f"duplicate Linux CPU {core.cpu} in topology")
            key = (core.kind, core.index)
            if key in by_key:
                raise ValueError(f"duplicate core {core.kind}-Core {core.index} in topology")
            by_cpu[core.cpu] = core
            by_key[key] = core
        object.__setattr__(self, "cores", cores)
        object.__setattr__(self, "_by_cpu", by_cpu)
        object.__setattr__(self, "_by_key", by_key)

    def by_cpu(self, cpu: int) -> Core:
        """Return the core for a Linux CPU number; ``UnknownCore`` if there is none."""
        core = self._by_cpu.get(cpu) if type(cpu) is int else None
        if core is None:
            raise UnknownCore(f"unknown Linux CPU {cpu!r}")
        return core

    def resolve(self, kind: str, index: int) -> Core:
        """Return the core for an XTU name (kind "P"/"E", index); ``UnknownCore`` if there is none."""
        core = self._by_key.get((kind, index)) if isinstance(kind, str) and type(index) is int else None
        if core is None:
            raise UnknownCore(f"unknown core kind={kind!r} index={index!r}")
        return core

    def cluster_cpus(self, cluster: int) -> tuple[int, ...]:
        """Linux CPUs (ascending) of one E-cluster; ``UnknownCore`` for an unknown cluster."""
        cpus = tuple(sorted(c.cpu for c in self.cores if c.cluster is not None and c.cluster == cluster))
        if not cpus or type(cluster) is not int:
            raise UnknownCore(f"unknown E-cluster {cluster!r}")
        return cpus

    def xtu_name(self, core: Core) -> str:
        """XTU display name, e.g. ``"P-Core 4"`` / ``"E-Core 12"``; ``UnknownCore`` if not in this topology."""
        if self._by_key.get((core.kind, core.index)) != core:
            raise UnknownCore(f"core {core!r} is not part of this topology")
        return f"{core.kind}-Core {core.index}"

    # ---- units: a P-core ``P<n>`` or an E-cluster ``EC<n>`` (the things that have one V/F curve) ----
    def unit_ids(self) -> tuple[str, ...]:
        """All unit ids: ``P0..`` in index order, then ``EC0..`` in cluster order."""
        p = sorted(c.index for c in self.cores if c.kind == KIND_P)
        e = sorted({c.cluster for c in self.cores if c.cluster is not None})
        return tuple(f"P{i}" for i in p) + tuple(f"EC{k}" for k in e)

    def unit_cpus(self, unit: str) -> tuple[int, ...]:
        """Linux CPUs (ascending) of a unit; ``UnknownCore`` for an unknown id."""
        if isinstance(unit, str) and unit.startswith("EC") and unit[2:].isdigit():
            return self.cluster_cpus(int(unit[2:]))
        if isinstance(unit, str) and unit.startswith("P") and unit[1:].isdigit():
            return (self.resolve(KIND_P, int(unit[1:])).cpu,)
        raise UnknownCore(f"unknown unit {unit!r}")

    def unit_of_cpu(self, cpu: int) -> str:
        """The unit id a Linux CPU belongs to; ``UnknownCore`` if the CPU is not part of the topology."""
        core = self.by_cpu(cpu)
        return f"P{core.index}" if core.cluster is None else f"EC{core.cluster}"

    def cpus_of_kind(self, kind: str) -> tuple[int, ...]:
        """All Linux CPUs of a core kind, ascending."""
        return tuple(sorted(c.cpu for c in self.cores if c.kind == kind))


def _parse_cpulist(text: str, what: str) -> frozenset[int]:
    """Parse kernel cpulist syntax strictly; anything odd raises ``TopologyMismatch``."""
    body = text.strip()
    if not body:
        raise TopologyMismatch(f"{what}: empty cpu list")
    cpus: set[int] = set()
    for item in body.split(","):
        m = _CPULIST_ITEM.fullmatch(item)
        if m is None:
            raise TopologyMismatch(f"{what}: malformed cpu list {body!r} (bad item {item!r})")
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) is not None else lo
        if hi < lo:
            raise TopologyMismatch(f"{what}: malformed cpu list {body!r} (reversed range {item!r})")
        if hi > _MAX_CPU:
            raise TopologyMismatch(f"{what}: cpu number {hi} in {body!r} is implausibly large")
        for cpu in range(lo, hi + 1):
            if cpu in cpus:
                raise TopologyMismatch(f"{what}: malformed cpu list {body!r} (CPU {cpu} listed twice)")
            cpus.add(cpu)
    return frozenset(cpus)


def _read_cpulist(path: Path, what: str) -> frozenset[int]:
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise TopologyMismatch(f"{what}: cannot read {path}: {exc}") from exc
    return _parse_cpulist(text, f"{what} ({path})")


def detect(sysfs_root: Path = Path("/sys")) -> Topology:
    """The running machine's topology, derived from sysfs under ``sysfs_root`` (``topology.live.read_live``).

    Never guesses: a missing, malformed or inconsistent file raises ``TopologyMismatch``."""
    from .live import read_live  # lazy: live imports this module

    return read_live(Path(sysfs_root)).topology
