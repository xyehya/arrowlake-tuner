"""G-138 fixtures: the simulated reference part as a Windows machine and as an accepted-state fingerprint. Test data only (D-036): ``src/`` holds no map and no
fingerprint of any chip; the tests derive what the Windows restore derives at run time from these.

``LP_OF_CPU`` / ``FUSED_POINT`` (``msr/measured_baseline``, ``[MEASURED win 2026-10-01 / lnx 2026-10-02]``) say which Windows logical processor each Linux CPU of
the reference part is and what its fused 0x09 word reads; ``FakeWin32`` serves the CPU-set / L2 data Windows would report for it, optionally over a simulated
machine (``sim`` + ``cpu_of_lp``, the physical truth: which Linux CPU's silicon each LP is) so the real ``WinThrottleStopMsrPort`` can be driven end to end.
"""
from __future__ import annotations

import errno
import struct

from arltuner.msr import winport
from arltuner.msr.measured_baseline import E_CPUS, FUSED_POINT, LP_OF_CPU, P_CPUS
from arltuner.msr.winport import Affinity, CpuSet, L2Cache
from arltuner.profiles.fingerprint import CpuId, Fingerprint, build_fingerprint
from arltuner.topology import Topology, arl_s_285k

__all__ = ["E_CLASS", "E_CPUS", "FUSED_POINT", "FakeWin32", "LP_OF_CPU", "P_CLASS", "P_CPUS", "REFERENCE_CPU_ID", "good_cpu_sets", "good_l2", "reference_fingerprint"]

P_CLASS, E_CLASS = 1, 0
REFERENCE_CPU_ID = CpuId("GenuineIntel", 6, 198, "Intel(R) Core(TM) Ultra 9 285K")  # what the reference part's /proc/cpuinfo says (a fixture)


def reference_fingerprint(words: dict[int, int] | None = None, cpu_id: CpuId | None = REFERENCE_CPU_ID, topology: Topology | None = None) -> Fingerprint:
    """What ``profiles.accept`` would record on the reference part (fused words per CPU, as ``helper.fused_words``)."""
    return build_fingerprint(topology or arl_s_285k(), dict(FUSED_POINT) if words is None else words, cpu_id)


def good_cpu_sets(lp_of_cpu=LP_OF_CPU) -> list[CpuSet]:
    kinds = {lp: (P_CLASS if cpu in P_CPUS else E_CLASS) for cpu, lp in lp_of_cpu.items()}
    return [CpuSet(0, lp, kinds[lp]) for lp in range(len(kinds))]


def good_l2(lp_of_cpu=LP_OF_CPU) -> list[L2Cache]:
    caches = [L2Cache(0, 1 << lp_of_cpu[c]) for c in P_CPUS]
    for lo in (8, 12, 16, 20):
        caches.append(L2Cache(0, sum(1 << lp_of_cpu[c] for c in range(lo, lo + 4))))
    return caches


class FakeWin32:
    """Pins are honoured unless ``stuck_on`` (the thread stays on that LP) or ``migrate_after_ioctl`` is set. With ``sim`` and ``cpu_of_lp`` set, RDMSR / WRMSR
    go to ``sim`` on the Linux CPU whose silicon the running LP is; otherwise they use a plain ``msrs`` dict keyed by (LP, MSR)."""

    def __init__(self, sim=None, cpu_of_lp=None) -> None:  # type: ignore[no-untyped-def]
        self.affinity = Affinity(0, (1 << 24) - 1)
        self.running_lp = 5
        self.stuck_on: int | None = None
        self.migrate_after_ioctl: int | None = None
        self.fail_ioctl: OSError | None = None
        self.fail_open: OSError | None = None
        self.fail_restore = False
        self.opened: list[str] = []
        self.closed: list[int] = []
        self.ioctls: list[tuple[int, int, bytes, int, int]] = []  # handle, code, data, out_size, lp it ran on
        self.affinity_calls: list[Affinity] = []
        self.msrs: dict[tuple[int, int], int] = {}
        self.sets, self.caches = good_cpu_sets(), good_l2()
        self.identity: CpuId | None = REFERENCE_CPU_ID
        self.sim, self.cpu_of_lp = sim, cpu_of_lp

    def open_device(self, path: str) -> int:
        if self.fail_open:
            raise self.fail_open
        self.opened.append(path)
        return 77

    def close_handle(self, handle: int) -> None:
        self.closed.append(handle)

    def ioctl(self, handle: int, code: int, data: bytes, out_size: int) -> bytes:
        if self.fail_ioctl:
            raise self.fail_ioctl
        self.ioctls.append((handle, code, data, out_size, self.running_lp))
        lp = self.running_lp
        if self.migrate_after_ioctl is not None:
            self.running_lp = self.migrate_after_ioctl
        if code == winport.IOCTL_RDMSR:
            (msr,) = struct.unpack("<I", data)
            if self.sim is not None:
                return self.sim.rdmsr(self.cpu_of_lp[lp], msr).to_bytes(8, "little")
            return self.msrs.get((lp, msr), 0).to_bytes(8, "little")
        msr, lo, hi = struct.unpack("<III", data)
        if self.sim is not None:
            self.sim.wrmsr(self.cpu_of_lp[lp], msr, lo | hi << 32)
        else:
            self.msrs[(lp, msr)] = lo | hi << 32
        return b""

    def set_thread_affinity(self, affinity: Affinity) -> Affinity:
        self.affinity_calls.append(affinity)
        if self.fail_restore and len(self.affinity_calls) > 1:
            raise OSError(errno.EIO, "restore refused")
        old, self.affinity = self.affinity, affinity
        if self.stuck_on is None:
            self.running_lp = affinity.mask.bit_length() - 1 if bin(affinity.mask).count("1") == 1 else self.running_lp
        else:
            self.running_lp = self.stuck_on
        return old

    def current_processor(self) -> tuple[int, int]:
        return 0, self.running_lp

    def cpu_sets(self) -> list[CpuSet]:
        return self.sets

    def l2_caches(self) -> list[L2Cache]:
        return self.caches

    def cpu_identity(self) -> CpuId | None:
        return self.identity
