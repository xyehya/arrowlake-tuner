"""The simulated reference CPU as data (no behaviour, no I/O): what the mailbox answered on one 285K-like part on Windows through XTU's
level-4 log. ``SimConfig.measured()`` (``sim.py``) turns these tables into a simulated machine. **Test/simulator fixture only (D-036):
imported by ``msr/sim.py`` and the tests, never by a runtime path** (a test pins this).

Every table here is ``[MEASURED win 2026-10-01]``: ``docs/research/win-xtu-live-trace.md`` section "S-03d" (baseline words, BIOS 3.40,
OC lock off, UVP off, no XTU tuning applied) and ``docs/research/registers.md`` "Measured via XTU's level-4 log on Windows".
Nothing else belongs in this module: a value that was not measured must not be added here (``[UNVERIFIED]`` / ``[COMMUNITY]`` facts
are leads, not constants).

**Numbering.** XTU's log (and so the measurement) uses the Windows logical processor (LP). These tables are in **Linux CPU numbering**
through the order-preserving map of ``win-xtu-live-trace.md`` "Core-numbering finding": P ordinal n <-> LP ``(0, 1, 10, 11, 12, 13,
22, 23)[n]`` <-> Linux CPU n; E cpus 8-11 <-> LP 2-5, 12-15 <-> 6-9, 16-19 <-> 14-17, 20-23 <-> 18-21. The map is consistent with the
Linux measurement (favored P = CPU 0 and 3 = LP 0 and 11; the 1.156 V cluster = CPU 8-11) but has **not been measured on Linux**.

What is *not* here, on purpose: the pcode's rule behaviour (which rc for which violation), the ratio caps of ``0x11`` and the
``0x28`` answers of non-core domains -- those stay SIM-ONLY defaults of the simulator.
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from arltuner.topology.cpumap import Topology
from arltuner.topology.reference import arl_s_285k

__all__ = [
    "BASELINE_CPU_COUNT",
    "CPU_COUNT",
    "DOMAIN_VF",
    "E_CPUS",
    "FAVORED_WORD",
    "FIXED_READS",
    "FUSED_POINT",
    "GRANULAR_WORD",
    "LP_OF_CPU",
    "MAX_VOLTAGE_RAW",
    "MISC_GLOBAL_CONFIG",
    "P_CPUS",
    "TAG",
    "TURBO_DEFAULTS",
    "VF_DOMAIN0",
    "VF_POINT_RATIOS",
    "Tables",
    "tables_for",
]

TAG = "[MEASURED win 2026-10-01]"

# ---- the reference part: 8 P (CPU 0-7) + 16 E (CPU 8-23 in clusters of four), as measured --------------------------------------------------
BASELINE_CPU_COUNT = 24
P_CPUS: tuple[int, ...] = tuple(range(8))
E_CPUS: tuple[int, ...] = tuple(range(8, 24))
CPU_COUNT = BASELINE_CPU_COUNT

#: Windows logical processor of each Linux CPU, P ordinal order and E ordinal order (order-preserving map, see the module docstring).
_P_LP = (0, 1, 10, 11, 12, 13, 22, 23)
_E_LP = (2, 3, 4, 5, 6, 7, 8, 9, 14, 15, 16, 17, 18, 19, 20, 21)

#: Fused V/F point word (cmd 0x09 P1=0x11 P2=0) of each P-core and of the E clusters [MEASURED win 2026-10-01 / lnx 2026-10-02]: distinct per P-core.
_P_FUSED = (0x5B439, 0x53839, 0x57439, 0x59439, 0x54439, 0x56C39, 0x56039, 0x58039)
_E_FUSED_FIRST_CLUSTER = 0x4A02E  # CPU 8-11
_E_FUSED_OTHER_CLUSTERS = 0x48C2E  # CPU 12-23

#: Fused V/F point ratios, P2 = 1..n of cmd 0x10 [MEASURED lnx 2026-10-02]; the ring has a 9th point (40x) only while the ring OC ratio is 40x.
VF_POINT_RATIOS: Mapping[str, tuple[int, ...]] = MappingProxyType(
    {"P": (8, 20, 30, 48, 53, 55, 56, 57), "E": (8, 15, 23, 30, 40, 46), "ring": (8, 14, 20, 28, 34, 37, 38, 39)}
)

#: Plain turbo tables, CPU 0 [MEASURED lnx 2026-09-29]: 0x1AD P ratios 57,57,55 x6; 0x1AE counts 1..8; 0x650 E ratios 48 x8; 0x651 its counts.
TURBO_DEFAULTS: Mapping[int, int] = MappingProxyType(
    {0x1AD: 0x3737373737373939, 0x1AE: 0x0807060504030201, 0x650: 0x3030303030303030, 0x651: 0x100C080604030201}
)

#: ``0x28 P1=0 P2=0``: P ``0x681``, E ``0x56D`` (S2.10; XTU subtracts 1). ``0x1C`` favored ratio / fused ratio words.
_MAX_V_P, _MAX_V_E = 0x681, 0x56D
_FAV_FAVORED_P, _FAV_OTHER_P, _FAV_E = 0x3939, 0x3737, 0x2E30


def _freeze(table: Mapping[int, int]) -> Mapping[int, int]:
    return MappingProxyType(dict(table))


class Tables:
    """The per-CPU tables of a simulated part: ``VF_DOMAIN0``, ``FUSED_POINT``, ``FAVORED_WORD``, ``GRANULAR_WORD``, ``MAX_VOLTAGE_RAW`` (cpu -> word)."""

    __slots__ = ("FAVORED_WORD", "FUSED_POINT", "GRANULAR_WORD", "MAX_VOLTAGE_RAW", "VF_DOMAIN0")

    def __init__(self, vf: Mapping[int, int], fused: Mapping[int, int], favored: Mapping[int, int], granular: Mapping[int, int], maxv: Mapping[int, int]) -> None:
        self.VF_DOMAIN0, self.FUSED_POINT, self.FAVORED_WORD = _freeze(vf), _freeze(fused), _freeze(favored)
        self.GRANULAR_WORD, self.MAX_VOLTAGE_RAW = _freeze(granular), _freeze(maxv)


def tables_for(topology: Topology) -> Tables:
    """The reference part's words laid over any topology (so a simulated 6P+8E works): P-core ``n`` takes the reference P word ``n`` (cycled), E cluster 0
    the first-cluster word and the others the other-cluster word; favored P-cores read ``0x3939``, the others ``0x3737``; only the first P CPU has a ``0x10``
    word (``0x39``), every other CPU ``0x0``."""
    vf: dict[int, int] = {}
    fused: dict[int, int] = {}
    favored: dict[int, int] = {}
    maxv: dict[int, int] = {}
    first_p = min((c.cpu for c in topology.cores if c.kind == "P"), default=None)
    for c in topology.cores:
        vf[c.cpu] = 0x39 if c.cpu == first_p else 0x0
        maxv[c.cpu] = _MAX_V_P if c.kind == "P" else _MAX_V_E
        if c.kind == "P":
            fused[c.cpu] = _P_FUSED[c.index % len(_P_FUSED)]
            favored[c.cpu] = _FAV_FAVORED_P if c.favored else _FAV_OTHER_P
        else:
            fused[c.cpu] = _E_FUSED_FIRST_CLUSTER if c.cluster == 0 else _E_FUSED_OTHER_CLUSTERS
            favored[c.cpu] = _FAV_E
    return Tables(vf, fused, favored, {c.cpu: 0x3 for c in topology.cores}, maxv)


_REF = tables_for(arl_s_285k())

#: Linux CPU -> Windows logical processor (the numbering of XTU's log). Documentation / cross-check only; the tables below are in Linux numbering.
LP_OF_CPU: Mapping[int, int] = _freeze({**dict(zip(P_CPUS, _P_LP)), **dict(zip(E_CPUS, _E_LP))})

#: ``0x10 P1=0 P2=0`` (S-03d, read twice per LP): LP 0 = ``0x39`` (ratio override 57, no V target, no offset); **every other core ``0x0``**.
VF_DOMAIN0: Mapping[int, int] = _REF.VF_DOMAIN0

#: ``0x14 P1=0 P2=0``: PerCore Voltage Enable (bit 3) already set (S-03d).
MISC_GLOBAL_CONFIG = 0x8

#: ``0x26 P1=0 P2=0`` per core (granular index 3 on every core).
GRANULAR_WORD: Mapping[int, int] = _REF.GRANULAR_WORD

#: ``0x28 P1=0 P2=0`` per core: P ``0x681``, E ``0x56D`` (S2.10; XTU subtracts 1).
MAX_VOLTAGE_RAW: Mapping[int, int] = _REF.MAX_VOLTAGE_RAW

#: ``0x09 P1=0x11 P2=0`` per core: the fused V/F point (ratio ``[7:0]``, voltage ``[19:8]``, S2.10). These are the XTU UI
#: "Voltage Override" values (P0 1.426 V, P4 1.316 V, E 1.156 V / 1.137 V).
FUSED_POINT: Mapping[int, int] = _REF.FUSED_POINT

#: ``0x1C P1=0 P2=0`` per core (``[7:0]`` favored/current ratio, ``[15:8]`` fused): favored P = CPU 0 and 3 (57), other P 55,
#: E ``0x2E30`` (fused 46, favored ratio 48 -- note ``[7:0]`` > ``[15:8]`` on E cores: ``[15:8]`` is not a ceiling for the ratio).
FAVORED_WORD: Mapping[int, int] = _REF.FAVORED_WORD

#: ``0x10`` of the package-wide domains (P1 -> word): GT slice ``0xF8600000`` (offset field = -59.6 mV, the GT -0.060 V the operator
#: had applied), ring ``0x28`` (40x), NGU ``0x20`` (32x).
DOMAIN_VF: Mapping[int, int] = _freeze({1: 0xF8600000, 2: 0x28, 7: 0x20})

#: Fixed (package-wide) reads ``(cmd, P1, P2) -> word``. ``0x01 P1=3`` is *not* listed: it answers rc 5 (OCNotSupported) on this
#: machine, which the simulator models in its capability handler.
FIXED_READS: Mapping[tuple[int, int, int], int] = MappingProxyType(
    {
        (0x01, 0, 0): 0x778,
        (0x01, 1, 0): 0x778,
        (0x01, 2, 0): 0x755,
        (0x01, 7, 0): 0x755,
        (0x01, 4, 0): 0x600,
        (0x02, 0, 0): 0x36363939,
        (0x02, 1, 0): 0x36363636,
        (0x16, 0, 0): 0x640,  # IccMax 400 A (U9.2 / 4)
        (0x16, 1, 0): 0xC8,  # 50 A
        (0x04, 0, 0): 0x84002000,  # layout unresolved (C-1); kept as the raw word
        (0x18, 0, 0): 0x1E,
        (0x1A, 0, 0): 0x0,
    }
)
