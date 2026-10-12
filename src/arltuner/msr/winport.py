"""The Windows MSR port: ThrottleStop's signed kernel driver (``\\\\.\\ThrottleStop``), driven through ctypes.

Interface (``docs/research/win-throttlestop-fivr.md`` section 1, ``[DECOMPILED driver]``): buffered IOCTL ``0x80006448`` RDMSR
(in: u32 msr; out: u64) and ``0x8000644C`` WRMSR (in: ``{u32 msr, u32 lo, u32 hi}``; no output). There is **no CPU argument**: the
driver executes on whatever logical processor the calling thread is on. So every access here

1. pins the calling thread to ONE logical processor (``SetThreadGroupAffinity``),
2. verifies with ``GetCurrentProcessorNumberEx`` that the thread really runs there (before the IOCTL, and again after it),
3. issues the IOCTL, and
4. restores the original affinity (always, also after a failure).

Any failure is an ``MsrError``; a read-only port refuses ``wrmsr`` before anything is opened.

CPU numbering: arltuner uses LINUX CPU numbers; Windows numbers logical processors differently and cannot be asked for the Linux numbering, so the
mapping is an input: the table ``WINDOWS_LP_OF_CPU`` below (the Windows restore's map, MEASURED once on the operator's own part) or one the caller passes.
The P CPUs and E clusters are taken from the topology the caller gives (default: the simulated reference topology that table belongs to). At construction
the map is cross-checked against what Windows reports (CPU-set EfficiencyClass per LP, L2 cache sharing per LP) and the port refuses to exist if the machine
does not match (fail closed).
**Limit of the cross-check:** it proves P vs E and the E clusters, not the order of the eight P-cores among themselves (they
look identical to Windows); that part rests on the measured table alone.

Importing this module touches no Windows API (``ctypes.windll`` / ``WinDLL`` are only looked up when ``Win32Api`` is
instantiated), so it imports on Linux, where the suite exercises everything against a fake ``Win32Api``.
"""
from __future__ import annotations

import errno
import struct
import sys
import threading
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import NamedTuple, Protocol

from arltuner.topology.cpumap import Topology
from arltuner.topology.reference import arl_s_285k

from .port import MsrError, MsrPort, check_cpu, check_msr, check_value

DEVICE_PATH = "\\\\.\\ThrottleStop"
IOCTL_RDMSR = 0x80006448
IOCTL_WRMSR = 0x8000644C

#: Linux CPU -> Windows logical processor for the Windows restore (order-preserving map, ``[MEASURED win 2026-10-01]``): P ordinal order and E ordinal order.
#: Inherently per part (Windows cannot report Linux numbering); the construction-time cross-check below is the refusal when it does not fit.
_P_LP = (0, 1, 10, 11, 12, 13, 22, 23)
_E_LP = (2, 3, 4, 5, 6, 7, 8, 9, 14, 15, 16, 17, 18, 19, 20, 21)
WINDOWS_LP_OF_CPU: Mapping[int, int] = {**{cpu: lp for cpu, lp in zip(range(8), _P_LP)}, **{cpu: lp for cpu, lp in zip(range(8, 24), _E_LP)}}
#: Fused V/F point word (cmd 0x09 P1=0x11 P2=0) of each P-core, in Linux P order [MEASURED win 2026-10-01 / lnx 2026-10-02]: distinct per physical P-core, so the
#: ``--device-win`` identity check can tell the P-cores apart (Windows' own APIs cannot). It belongs to ``WINDOWS_LP_OF_CPU`` and is the check OF that map, not a
#: statement about any other part.
WINDOWS_P_FUSED_FINGERPRINT: tuple[int, ...] = (0x5B439, 0x53839, 0x57439, 0x59439, 0x54439, 0x56C39, 0x56039, 0x58039)

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_ERROR_INSUFFICIENT_BUFFER = 122
_RELATION_CACHE = 2

_WINERROR_TO_ERRNO = {2: errno.ENOENT, 3: errno.ENOENT, 5: errno.EACCES, 32: errno.EBUSY, 87: errno.EINVAL, 1: errno.EINVAL}


class CpuSet(NamedTuple):
    """One logical processor as ``GetSystemCpuSetInformation`` reports it."""

    group: int
    lp: int  # LogicalProcessorIndex, relative to the group
    efficiency_class: int


class L2Cache(NamedTuple):
    """One L2 cache as ``GetLogicalProcessorInformationEx(RelationCache)`` reports it: the LPs of one group that share it."""

    group: int
    mask: int


class Affinity(NamedTuple):
    group: int
    mask: int


class Win32(Protocol):
    """The slice of Win32 the port uses. ``Win32Api`` is the real one; the tests inject a fake. Failures raise ``OSError``."""

    def open_device(self, path: str) -> int: ...
    def close_handle(self, handle: int) -> None: ...
    def ioctl(self, handle: int, code: int, data: bytes, out_size: int) -> bytes: ...
    def set_thread_affinity(self, affinity: Affinity) -> Affinity: ...  # returns the previous affinity
    def current_processor(self) -> tuple[int, int]: ...  # (group, number within the group)
    def cpu_sets(self) -> list[CpuSet]: ...
    def l2_caches(self) -> list[L2Cache]: ...


# ----------------------------------------------------------------------------------------------- record parsing (pure)
def parse_cpu_sets(buf: bytes) -> list[CpuSet]:
    """``SYSTEM_CPU_SET_INFORMATION`` records (``Size`` u32 @0, ``Type`` u32 @4 = 0 CpuSetInformation; then Id u32 @8, Group u16
    @12, LogicalProcessorIndex u8 @14, CoreIndex u8 @15, LastLevelCacheIndex u8 @16, NumaNodeIndex u8 @17, EfficiencyClass u8 @18)."""
    found: list[CpuSet] = []
    pos = 0
    while pos + 8 <= len(buf):
        size, kind = struct.unpack_from("<II", buf, pos)
        if size < 8 or pos + size > len(buf):
            raise ValueError(f"malformed CPU set record at offset {pos} (size {size})")
        if kind == 0:
            if size < 19:
                raise ValueError(f"CPU set record at offset {pos} is too short ({size} bytes)")
            group = struct.unpack_from("<H", buf, pos + 12)[0]
            found.append(CpuSet(group, buf[pos + 14], buf[pos + 18]))
        pos += size
    return found


def parse_l2_caches(buf: bytes) -> list[L2Cache]:
    """``SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX`` records of ``RelationCache``: Relationship u32 @0, Size u32 @4, Level u8 @8,
    ... GroupMask (KAFFINITY u64 @40, Group u16 @48). Only Level 2 is kept; duplicates collapse."""
    found: list[L2Cache] = []
    pos = 0
    while pos + 8 <= len(buf):
        relationship, size = struct.unpack_from("<II", buf, pos)
        if size < 8 or pos + size > len(buf):
            raise ValueError(f"malformed logical-processor record at offset {pos} (size {size})")
        if relationship == _RELATION_CACHE and buf[pos + 8] == 2:
            if size < 50:
                raise ValueError(f"cache record at offset {pos} is too short ({size} bytes)")
            mask = struct.unpack_from("<Q", buf, pos + 40)[0]
            group = struct.unpack_from("<H", buf, pos + 48)[0]
            if L2Cache(group, mask) not in found:
                found.append(L2Cache(group, mask))
        pos += size
    return found


# --------------------------------------------------------------------------------------------------- LP map cross-check
def _bits(mask: int) -> frozenset[int]:
    return frozenset(i for i in range(mask.bit_length()) if mask >> i & 1)


def check_lp_map(
    cpu_sets: Sequence[CpuSet], l2_caches: Sequence[L2Cache], lp_of_cpu: Mapping[int, int] = WINDOWS_LP_OF_CPU, topology: Topology | None = None
) -> list[str]:
    """Problems found when ``lp_of_cpu`` (Linux CPU -> Windows LP) is held against what Windows reports; ``[]`` means it matches.
    ``topology`` gives the P CPUs and the E clusters (default: the simulated reference topology the default map belongs to).

    * as many LPs as the topology has CPUs, one processor group (0), the map is a bijection onto them;
    * exactly two EfficiencyClasses; every P-CPU's LP has the higher one, every E-CPU's LP the lower one;
    * every P-CPU's LP has an L2 of its own (a mask of that one LP); each E cluster of the topology is exactly
      the LP set of one L2 mask, i.e. its LPs share an L2 and no other LP does.
    Not provable here: the order of the P-cores among themselves (see the module docstring)."""
    problems: list[str] = []
    topo = topology if topology is not None else arl_s_285k()
    p_cpus, e_cpus = topo.cpus_of_kind("P"), topo.cpus_of_kind("E")
    e_clusters = [topo.cluster_cpus(k) for k in sorted({c.cluster for c in topo.cores if c.cluster is not None})]
    cpus = p_cpus + e_cpus
    if sorted(lp_of_cpu) != sorted(cpus):
        return [f"the LP map covers CPUs {sorted(lp_of_cpu)}, expected {list(cpus)}"]
    lps = [lp_of_cpu[c] for c in cpus]
    if len(set(lps)) != len(lps):
        return ["the LP map is not one-to-one"]
    by_lp: dict[int, CpuSet] = {}
    for cs in cpu_sets:
        if cs.group != 0:
            problems.append(f"logical processor {cs.lp} is in processor group {cs.group} (only group 0 is supported)")
        by_lp[cs.lp] = cs
    if len(cpu_sets) != len(cpus):
        problems.append(f"Windows reports {len(cpu_sets)} logical processors, expected {len(cpus)}")
    if problems:
        return problems
    for lp in lps:
        if lp not in by_lp:
            problems.append(f"the LP map names logical processor {lp}, which Windows does not report")
    if problems:
        return problems
    classes = sorted({cs.efficiency_class for cs in cpu_sets})
    if len(classes) != 2:
        return [f"expected two EfficiencyClasses (P and E), Windows reports {classes}"]
    e_class, p_class = classes
    for cpu in p_cpus:
        if by_lp[lp_of_cpu[cpu]].efficiency_class != p_class:
            problems.append(f"P-CPU {cpu} -> LP {lp_of_cpu[cpu]} is not in the performance EfficiencyClass {p_class}")
    for cpu in e_cpus:
        if by_lp[lp_of_cpu[cpu]].efficiency_class != e_class:
            problems.append(f"E-CPU {cpu} -> LP {lp_of_cpu[cpu]} is not in the efficiency EfficiencyClass {e_class}")
    l2 = {c.mask for c in l2_caches if c.group == 0}
    if not l2:
        return problems + ["Windows reports no L2 cache information"]
    for cpu in p_cpus:
        if (1 << lp_of_cpu[cpu]) not in l2:
            problems.append(f"P-CPU {cpu} -> LP {lp_of_cpu[cpu]} does not have a private L2 cache")
    for cluster in e_clusters:
        want = 0
        for cpu in cluster:
            want |= 1 << lp_of_cpu[cpu]
        if want not in l2:
            problems.append(f"E cluster {cluster[0]}-{cluster[-1]} -> LPs {sorted(lp_of_cpu[c] for c in cluster)} is not exactly one shared L2 cache")
    return problems


# --------------------------------------------------------------------------------------------------------- the real Win32
def _oserror(winerr: int, what: str) -> OSError:
    return OSError(_WINERROR_TO_ERRNO.get(winerr, errno.EIO), f"{what} failed (Win32 error {winerr})")


class Win32Api:
    """Real ``Win32`` over ``kernel32`` via ctypes (Windows only; raises ``OSError(ENOSYS)`` elsewhere). Hardware is touched
    only through ``ioctl``."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError(errno.ENOSYS, "the ThrottleStop port needs Windows (ctypes.WinDLL)")
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        self._wt = wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]  # Windows-only attribute
        self._k = k

        class GroupAffinity(ctypes.Structure):
            _fields_ = [("Mask", ctypes.c_size_t), ("Group", wintypes.WORD), ("Reserved", wintypes.WORD * 3)]

        class ProcessorNumber(ctypes.Structure):
            _fields_ = [("Group", wintypes.WORD), ("Number", ctypes.c_ubyte), ("Reserved", ctypes.c_ubyte)]

        self._GroupAffinity, self._ProcessorNumber = GroupAffinity, ProcessorNumber
        void_p, dword, bool_ = ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL
        k.CreateFileW.restype = void_p
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, dword, dword, void_p, dword, dword, void_p]
        k.CloseHandle.restype = bool_
        k.CloseHandle.argtypes = [void_p]
        k.DeviceIoControl.restype = bool_
        k.DeviceIoControl.argtypes = [void_p, dword, void_p, dword, void_p, dword, ctypes.POINTER(dword), void_p]
        k.GetCurrentThread.restype = void_p
        k.GetCurrentThread.argtypes = []
        k.GetCurrentProcess.restype = void_p
        k.GetCurrentProcess.argtypes = []
        k.SetThreadGroupAffinity.restype = bool_
        k.SetThreadGroupAffinity.argtypes = [void_p, ctypes.POINTER(GroupAffinity), ctypes.POINTER(GroupAffinity)]
        k.GetCurrentProcessorNumberEx.restype = None
        k.GetCurrentProcessorNumberEx.argtypes = [ctypes.POINTER(ProcessorNumber)]
        k.GetSystemCpuSetInformation.restype = bool_
        k.GetSystemCpuSetInformation.argtypes = [void_p, dword, ctypes.POINTER(dword), void_p, dword]
        k.GetLogicalProcessorInformationEx.restype = bool_
        k.GetLogicalProcessorInformationEx.argtypes = [dword, void_p, ctypes.POINTER(dword)]

    def _last(self, what: str) -> OSError:
        return _oserror(self._ct.get_last_error(), what)

    def open_device(self, path: str) -> int:
        handle = self._k.CreateFileW(path, _GENERIC_READ | _GENERIC_WRITE, 0, None, _OPEN_EXISTING, 0, None)
        if handle is None or handle == self._ct.c_void_p(-1).value:
            raise self._last(f"CreateFileW({path})")
        return int(handle)

    def close_handle(self, handle: int) -> None:
        if not self._k.CloseHandle(handle):
            raise self._last("CloseHandle")

    def ioctl(self, handle: int, code: int, data: bytes, out_size: int) -> bytes:
        ct = self._ct
        inbuf = ct.create_string_buffer(data, len(data))
        outbuf = ct.create_string_buffer(max(out_size, 1))
        returned = self._wt.DWORD(0)
        ok = self._k.DeviceIoControl(handle, code, inbuf, len(data), outbuf if out_size else None, out_size, ct.byref(returned), None)
        if not ok:
            raise self._last(f"DeviceIoControl({code:#x})")
        if returned.value != out_size:
            raise OSError(errno.EIO, f"DeviceIoControl({code:#x}) returned {returned.value} bytes, expected {out_size}")
        return outbuf.raw[:out_size]

    def set_thread_affinity(self, affinity: Affinity) -> Affinity:
        new, old = self._GroupAffinity(affinity.mask, affinity.group), self._GroupAffinity()
        ct = self._ct
        if not self._k.SetThreadGroupAffinity(self._k.GetCurrentThread(), ct.byref(new), ct.byref(old)):
            raise self._last("SetThreadGroupAffinity")
        return Affinity(old.Group, old.Mask)

    def current_processor(self) -> tuple[int, int]:
        pn = self._ProcessorNumber()
        self._k.GetCurrentProcessorNumberEx(self._ct.byref(pn))
        return pn.Group, pn.Number

    def _query(self, call) -> bytes:  # type: ignore[no-untyped-def]  # call(buf_or_None, size, byref DWORD) -> BOOL
        ct = self._ct
        size = self._wt.DWORD(0)
        if call(None, 0, ct.byref(size)) or ct.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
            raise self._last("size query")
        buf = ct.create_string_buffer(size.value)
        if not call(buf, size.value, ct.byref(size)):
            raise self._last("query")
        return buf.raw[: size.value]

    def cpu_sets(self) -> list[CpuSet]:
        proc = self._k.GetCurrentProcess()
        return parse_cpu_sets(self._query(lambda buf, n, ret: self._k.GetSystemCpuSetInformation(buf, n, ret, proc, 0)))

    def l2_caches(self) -> list[L2Cache]:
        return parse_l2_caches(self._query(lambda buf, n, ret: self._k.GetLogicalProcessorInformationEx(_RELATION_CACHE, buf, ret)))


# ------------------------------------------------------------------------------------------------------------------ port
def verify_machine(api: Win32 | None = None, lp_of_cpu: Mapping[int, int] = WINDOWS_LP_OF_CPU, topology: Topology | None = None) -> None:
    """Hold ``lp_of_cpu`` against what Windows reports (read-only APIs, no device); raise ``MsrError(ENODEV)`` on any mismatch
    or if Windows cannot be asked."""
    try:
        api = api if api is not None else Win32Api()
        problems = check_lp_map(api.cpu_sets(), api.l2_caches(), lp_of_cpu, topology)
    except (OSError, ValueError) as exc:
        raise MsrError(errno.ENODEV, f"cannot verify the Linux CPU -> Windows LP map: {exc}") from exc
    if problems:
        raise MsrError(
            errno.ENODEV,
            "the Linux CPU -> Windows logical-processor map (winport.WINDOWS_LP_OF_CPU) does not match what Windows reports: " + "; ".join(problems),
        )


class WinThrottleStopMsrPort(MsrPort):
    """``MsrPort`` over the ThrottleStop driver. ``cpu`` is a LINUX CPU number; it is translated to a Windows LP and the calling
    thread is pinned there for the duration of one access. The device is opened lazily (``close()`` / ``with`` releases it)."""

    def __init__(
        self, writable: bool = False, *, api: Win32 | None = None, lp_of_cpu: Mapping[int, int] = WINDOWS_LP_OF_CPU, topology: Topology | None = None
    ) -> None:
        self.writable = bool(writable)
        self._lp_of_cpu = dict(lp_of_cpu)
        self._api: Win32 = api if api is not None else Win32Api()
        self._handle: int | None = None
        self._lock = threading.Lock()  # one pinned access at a time
        verify_machine(self._api, self._lp_of_cpu, topology)  # fail closed: no port on a machine the map does not describe

    def _device(self) -> int:
        if self._handle is None:
            try:
                self._handle = self._api.open_device(DEVICE_PATH)
            except OSError as exc:
                raise MsrError(exc.errno or errno.EIO, f"open {DEVICE_PATH}: {exc.strerror or exc} (is the ThrottleStop driver loaded, and is this an Administrator shell?)") from exc
        return self._handle

    def _on_lp(self, cpu: int, what: str, op):  # type: ignore[no-untyped-def]
        """Run ``op(handle)`` with this thread pinned to the LP of ``cpu``; verify the pin before and after; always restore."""
        lp = self._lp_of_cpu.get(cpu)
        if lp is None:
            raise ValueError(f"cpu={cpu} is not a CPU of the Linux-to-Windows logical-processor map")
        with self._lock:
            handle = self._device()
            try:
                original = self._api.set_thread_affinity(Affinity(0, 1 << lp))
            except OSError as exc:
                raise MsrError(exc.errno or errno.EIO, f"{what}: cannot pin the thread to LP {lp}: {exc.strerror or exc}") from exc
            done = False
            try:
                self._expect_on(lp, what, "before")
                result = op(handle)
                self._expect_on(lp, what, "after")
                done = True
                return result
            finally:
                try:
                    self._api.set_thread_affinity(original)
                except OSError as exc:
                    if done:  # never mask the access' own error; only a clean access reports the failed restore
                        raise MsrError(exc.errno or errno.EIO, f"{what}: cannot restore the thread affinity: {exc.strerror or exc}") from exc

    def _expect_on(self, lp: int, what: str, when: str) -> None:
        try:
            group, number = self._api.current_processor()
        except OSError as exc:
            raise MsrError(exc.errno or errno.EIO, f"{what}: cannot read the current processor: {exc.strerror or exc}") from exc
        if (group, number) != (0, lp):
            raise MsrError(errno.EIO, f"{what}: thread runs on group {group} LP {number}, not LP {lp} ({when} the access); the MSR may have hit another CPU")

    def rdmsr(self, cpu: int, msr: int) -> int:
        """Read MSR ``msr`` on Linux CPU ``cpu`` (IOCTL 0x80006448 on the pinned thread)."""
        cpu = check_cpu(cpu)
        msr = check_msr(msr)
        what = f"rdmsr cpu={cpu} msr={msr:#x}"

        def op(handle: int) -> int:
            try:
                raw = self._api.ioctl(handle, IOCTL_RDMSR, struct.pack("<I", msr), 8)
            except OSError as exc:
                raise MsrError(exc.errno or errno.EIO, f"{what}: {exc.strerror or exc}") from exc
            if len(raw) != 8:
                raise MsrError(errno.EIO, f"{what}: short read ({len(raw)} bytes)")
            return int.from_bytes(raw, "little")

        return self._on_lp(cpu, what, op)

    def wrmsr(self, cpu: int, msr: int, value: int) -> None:
        """Write MSR ``msr`` on Linux CPU ``cpu`` (IOCTL 0x8000644C). Read-only port: ``MsrError(EPERM)``, nothing is opened."""
        cpu = check_cpu(cpu)
        msr = check_msr(msr)
        value = check_value(value)
        what = f"wrmsr cpu={cpu} msr={msr:#x}"
        if not self.writable:
            raise MsrError(errno.EPERM, f"{what}: port is read-only")

        def op(handle: int) -> None:
            try:
                self._api.ioctl(handle, IOCTL_WRMSR, struct.pack("<III", msr, value & 0xFFFFFFFF, value >> 32), 0)
            except OSError as exc:
                raise MsrError(exc.errno or errno.EIO, f"{what}: {exc.strerror or exc}") from exc

        self._on_lp(cpu, what, op)

    def close(self) -> None:
        """Close the device handle (idempotent); the port reopens lazily."""
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                self._api.close_handle(handle)
            except OSError:
                pass

    def __enter__(self) -> WinThrottleStopMsrPort:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()


def windows_boot_id() -> str | None:
    """A per-boot id for the guard's per-boot budget: the Prefetch ``BootId`` counter (HKLM, bumped every boot) hashed to 32 hex
    digits; ``None`` (an unknown boot, still capped by the guard) when unreadable or not Windows. [UNVERIFIED: counter semantics]"""
    if sys.platform != "win32":
        return None
    try:
        import hashlib
        import winreg

        key = r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
            value, _kind = winreg.QueryValueEx(handle, "BootId")
        return hashlib.sha256(f"windows-bootid-{int(value)}".encode("ascii")).hexdigest()[:32]
    except (OSError, ValueError, ImportError):
        return None
