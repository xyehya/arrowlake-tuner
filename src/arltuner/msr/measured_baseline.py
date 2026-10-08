"""The MEASURED baseline of this machine as data (no behaviour, no I/O): what the mailbox answered on Windows through XTU's
level-4 log. ``SimConfig.measured()`` (``sim.py``) turns these tables into a simulated machine.

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

from arltuner.platform import default_platform

__all__ = [
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
    "VF_DOMAIN0",
]

TAG = "[MEASURED win 2026-10-01]"

# Topology, LP map and fused words come from ``platforms/arl-s-285k.toml`` (G-101); the values are unchanged.
_PLATFORM = default_platform()
_TOPO = _PLATFORM.topology

CPU_COUNT = _TOPO.cpu_count
P_CPUS = _TOPO.p_cpus  # Linux CPUs 0-7
E_CPUS = _TOPO.e_cpus  # Linux CPUs 8-23 (clusters 8-11, 12-15, 16-19, 20-23)

_P_LP = _TOPO.lp_p
_E_LP = _TOPO.lp_e


def _freeze(table: Mapping[int, int]) -> Mapping[int, int]:
    return MappingProxyType(dict(table))


#: Linux CPU -> Windows logical processor (the numbering of XTU's log). Documentation / cross-check only; the tables below are
#: already in Linux numbering.
LP_OF_CPU: Mapping[int, int] = _freeze({**dict(zip(P_CPUS, _P_LP)), **dict(zip(E_CPUS, _E_LP))})

#: ``0x10 P1=0 P2=0`` (S-03d, read twice per LP): LP 0 = ``0x39`` (ratio override 57, no V target, no offset); **every other core ``0x0``**.
VF_DOMAIN0: Mapping[int, int] = _freeze({cpu: (0x39 if cpu == 0 else 0x0) for cpu in range(CPU_COUNT)})

#: ``0x14 P1=0 P2=0``: PerCore Voltage Enable (bit 3) already set (S-03d).
MISC_GLOBAL_CONFIG = 0x8

#: ``0x26 P1=0 P2=0`` per core (granular index 3 on every core).
GRANULAR_WORD: Mapping[int, int] = _freeze({cpu: 0x3 for cpu in range(CPU_COUNT)})

#: ``0x28 P1=0 P2=0`` per core: P ``0x681``, E ``0x56D`` (S2.10; XTU subtracts 1).
MAX_VOLTAGE_RAW: Mapping[int, int] = _freeze({cpu: (0x681 if cpu < 8 else 0x56D) for cpu in range(CPU_COUNT)})

#: ``0x09 P1=0x11 P2=0`` per core: the fused V/F point (ratio ``[7:0]``, voltage ``[19:8]``, S2.10). These are the XTU UI
#: "Voltage Override" values (P0 1.426 V, P4 1.316 V, E 1.156 V / 1.137 V).
FUSED_POINT: Mapping[int, int] = _freeze(_PLATFORM.fused_point)

#: ``0x1C P1=0 P2=0`` per core (``[7:0]`` favored/current ratio, ``[15:8]`` fused): favored P = CPU 0 and 3 (57), other P 55,
#: E ``0x2E30`` (fused 46, favored ratio 48 -- note ``[7:0]`` > ``[15:8]`` on E cores: ``[15:8]`` is not a ceiling for the ratio).
FAVORED_WORD: Mapping[int, int] = _freeze(
    {
        **{cpu: (0x3939 if cpu in (0, 3) else 0x3737) for cpu in range(8)},
        **{cpu: 0x2E30 for cpu in range(8, 24)},
    }
)

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
