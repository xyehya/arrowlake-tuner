"""CPU map for the Core Ultra 9 285K (Arrow Lake-S): XTU names <-> Linux CPUs <-> E-clusters.

Pure Python, standard library only, no hardware access. ``detect`` reads two small sysfs files
(``devices/cpu_core/cpus`` and ``devices/cpu_atom/cpus``) under a caller-supplied root and only
*validates* them against the static map; it never infers a topology (Contract 3, docs/ARCHITECTURE.md).

Source of every hard-coded fact below: docs/research/lnx-turbostat-plus-facts.md, section "CPU map"
``[MEASURED lnx 2026-09-29 turbostat+ capture]``:
  * Linux CPUs 0-7 are P-cores, 8-23 are E-cores (no SMT).
  * E-clusters of four: 8-11, 12-15, 16-19, 20-23.
  * Favored P-cores (HWP highest = 92 vs 88): CPU 0 and CPU 3.
  * XTU "P-Core N" = Linux CPU N; XTU "E-Core k" = Linux CPU 8+k (inference from matching numbers;
    e.g. E-Core 12 = CPU 20, cluster 3).
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
    "arl_s_285k",
    "detect",
]

KIND_P = "P"
KIND_E = "E"
KINDS: tuple[str, str] = (KIND_P, KIND_E)

# [MEASURED lnx 2026-09-29] docs/research/lnx-turbostat-plus-facts.md "CPU map"
_P_COUNT = 8
_E_COUNT = 16
_E_CPU_BASE = 8  # E index k <-> Linux CPU 8 + k
_E_CLUSTER_SIZE = 4
_FAVORED_P_INDEXES = frozenset({0, 3})

# Kernel cpulist syntax ("0-7", "8-11,12-23"); an upper sanity bound keeps a corrupt file from
# expanding into a huge set (kernel NR_CPUS never exceeds 8192).
_MAX_CPU = 8191
_CPULIST_ITEM = re.compile(r"([0-9]+)(?:-([0-9]+))?")


class TopologyError(Exception):
    """Base class for topology errors."""


class TopologyMismatch(TopologyError):
    """``detect`` could not confirm the static 285K topology (missing, malformed or different files)."""


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
    index: int  # XTU numbering: P 0..7, E 0..15
    cpu: int  # Linux CPU number (P 0..7, E 8..23)
    cluster: int | None  # E: 0..3 (cpus 8-11, 12-15, 16-19, 20-23); P: None
    favored: bool

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"Core.kind must be 'P' or 'E', got {self.kind!r}")
        if self.kind == KIND_P and self.cluster is not None:
            raise ValueError("a P-core has no E-cluster (cluster must be None)")
        if self.kind == KIND_E and self.cluster is None:
            raise ValueError("an E-core must have a cluster number")


@dataclass(frozen=True)
class Topology:
    """Immutable set of cores with lookup helpers. Equality compares ``cores`` only."""

    cores: tuple[Core, ...]
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


def arl_s_285k() -> Topology:
    """The static 285K map: 8 P (CPU 0-7, favored P0 and P3) + 16 E (CPU 8-23, clusters of 4).

    Source: docs/research/lnx-turbostat-plus-facts.md "CPU map" [MEASURED lnx 2026-09-29].
    """
    cores = [
        Core(kind=KIND_P, index=i, cpu=i, cluster=None, favored=i in _FAVORED_P_INDEXES)
        for i in range(_P_COUNT)
    ]
    cores += [
        Core(kind=KIND_E, index=k, cpu=_E_CPU_BASE + k, cluster=k // _E_CLUSTER_SIZE, favored=False)
        for k in range(_E_COUNT)
    ]
    return Topology(cores=tuple(cores))


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
    """Validate the running machine against ``arl_s_285k()`` and return it.

    Reads ``<sysfs_root>/devices/cpu_core/cpus`` (expected ``0-7``) and
    ``<sysfs_root>/devices/cpu_atom/cpus`` (expected ``8-23``), both in kernel cpulist syntax.
    Never guesses: a missing, malformed or different file raises ``TopologyMismatch``.
    """
    root = Path(sysfs_root)
    expected = arl_s_285k()
    want_p = frozenset(c.cpu for c in expected.cores if c.kind == KIND_P)
    want_e = frozenset(c.cpu for c in expected.cores if c.kind == KIND_E)
    got_p = _read_cpulist(root / "devices" / "cpu_core" / "cpus", "cpu_core")
    got_e = _read_cpulist(root / "devices" / "cpu_atom" / "cpus", "cpu_atom")
    problems = []
    if got_p != want_p:
        problems.append(f"cpu_core/cpus is {sorted(got_p)} but the 285K map expects {sorted(want_p)}")
    if got_e != want_e:
        problems.append(f"cpu_atom/cpus is {sorted(got_e)} but the 285K map expects {sorted(want_e)}")
    if problems:
        raise TopologyMismatch("; ".join(problems))
    return expected
