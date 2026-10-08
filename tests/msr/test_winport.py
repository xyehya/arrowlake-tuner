"""Tests for arltuner.msr.winport. Only a fake ``Win32`` is used: no ctypes.windll, no driver, no hardware."""
from __future__ import annotations

import errno
import struct
import unittest

from arltuner.msr import MsrError, MsrPort
from arltuner.msr import winport
from arltuner.msr.measured_baseline import E_CPUS, LP_OF_CPU, P_CPUS
from arltuner.msr.winport import Affinity, CpuSet, L2Cache, WinThrottleStopMsrPort, check_lp_map

P_CLASS, E_CLASS = 1, 0


def good_cpu_sets() -> list[CpuSet]:
    lps = {lp: (P_CLASS if cpu in P_CPUS else E_CLASS) for cpu, lp in LP_OF_CPU.items()}
    return [CpuSet(0, lp, lps[lp]) for lp in range(24)]


def good_l2() -> list[L2Cache]:
    caches = [L2Cache(0, 1 << LP_OF_CPU[c]) for c in P_CPUS]
    for lo in (8, 12, 16, 20):
        caches.append(L2Cache(0, sum(1 << LP_OF_CPU[c] for c in range(lo, lo + 4))))
    return caches


class FakeWin32:
    """Pins are honoured unless ``stuck_on`` (the thread stays on that LP) or ``migrate_after`` is set."""

    def __init__(self) -> None:
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
        if self.migrate_after_ioctl is not None:
            self.running_lp = self.migrate_after_ioctl
        if code == winport.IOCTL_RDMSR:
            (msr,) = struct.unpack("<I", data)
            return self.msrs.get((self.running_lp, msr), 0).to_bytes(8, "little")
        msr, lo, hi = struct.unpack("<III", data)
        self.msrs[(self.running_lp, msr)] = lo | hi << 32
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


class Basics(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeWin32()

    def port(self, writable: bool = True) -> WinThrottleStopMsrPort:
        return WinThrottleStopMsrPort(writable, api=self.api)

    def test_is_an_msr_port_and_imports_without_windows(self) -> None:
        self.assertIsInstance(self.port(), MsrPort)

    def test_real_api_refuses_off_windows(self) -> None:
        import sys

        if sys.platform == "win32":
            self.skipTest("Windows")
        with self.assertRaises(OSError):
            WinThrottleStopMsrPort(True)

    def test_rdmsr_pins_to_the_mapped_lp_and_restores(self) -> None:
        self.api.msrs[(LP_OF_CPU[12], 0x150)] = 0x1122334455667788
        self.assertEqual(self.port().rdmsr(12, 0x150), 0x1122334455667788)
        handle, code, data, out_size, lp = self.api.ioctls[0]
        self.assertEqual((handle, code, data, out_size, lp), (77, 0x80006448, struct.pack("<I", 0x150), 8, LP_OF_CPU[12]))
        self.assertEqual(self.api.opened, ["\\\\.\\ThrottleStop"])
        self.assertEqual(self.api.affinity_calls, [Affinity(0, 1 << LP_OF_CPU[12]), Affinity(0, (1 << 24) - 1)])
        self.assertEqual(self.api.affinity, Affinity(0, (1 << 24) - 1))

    def test_linux_cpu_numbers_map_to_distinct_lps(self) -> None:
        p = self.port()
        for cpu in (0, 2, 7, 8, 23):
            p.wrmsr(cpu, 0x150, cpu + 1)
        self.assertEqual([lp for *_rest, lp in self.api.ioctls], [LP_OF_CPU[c] for c in (0, 2, 7, 8, 23)])

    def test_wrmsr_encoding(self) -> None:
        self.port().wrmsr(3, 0x150, 0x8000001100000042)
        _, code, data, out_size, lp = self.api.ioctls[0]
        self.assertEqual((code, out_size, lp), (0x8000644C, 0, LP_OF_CPU[3]))
        self.assertEqual(struct.unpack("<III", data), (0x150, 0x00000042 | 0x0, 0x80000011))

    def test_read_only_port_refuses_wrmsr_before_opening_anything(self) -> None:
        p = self.port(writable=False)
        with self.assertRaises(MsrError) as cm:
            p.wrmsr(0, 0x150, 1)
        self.assertEqual(cm.exception.errno, errno.EPERM)
        self.assertEqual((self.api.opened, self.api.ioctls, self.api.affinity_calls), ([], [], []))
        self.assertEqual(p.rdmsr(0, 0x150), 0)  # reads still work

    def test_argument_validation_happens_before_the_device(self) -> None:
        p = self.port()
        for call in (lambda: p.rdmsr(-1, 0x150), lambda: p.rdmsr(0, 1 << 32), lambda: p.wrmsr(0, 0x150, 1 << 64), lambda: p.rdmsr(24, 0x150), lambda: p.rdmsr(True, 1)):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual((self.api.opened, self.api.affinity_calls), ([], []))

    def test_pin_that_does_not_take_effect_raises_before_the_ioctl(self) -> None:
        self.api.stuck_on = 5
        with self.assertRaises(MsrError) as cm:
            self.port().rdmsr(0, 0x150)
        self.assertIn("not LP", str(cm.exception))
        self.assertEqual(self.api.ioctls, [])
        self.assertEqual(self.api.affinity, Affinity(0, (1 << 24) - 1))  # restored anyway

    def test_migration_during_the_access_is_reported(self) -> None:
        self.api.migrate_after_ioctl = 9
        with self.assertRaises(MsrError) as cm:
            self.port().wrmsr(0, 0x150, 1)
        self.assertIn("after the access", str(cm.exception))
        self.assertEqual(self.api.affinity, Affinity(0, (1 << 24) - 1))

    def test_ioctl_failure_is_an_msr_error_and_affinity_is_restored(self) -> None:
        self.api.fail_ioctl = OSError(errno.EACCES, "denied")
        with self.assertRaises(MsrError) as cm:
            self.port().rdmsr(1, 0x150)
        self.assertEqual(cm.exception.errno, errno.EACCES)
        self.assertEqual(self.api.affinity, Affinity(0, (1 << 24) - 1))

    def test_open_failure_names_the_driver(self) -> None:
        self.api.fail_open = OSError(errno.ENOENT, "nope")
        with self.assertRaises(MsrError) as cm:
            self.port().rdmsr(0, 0x150)
        self.assertIn("ThrottleStop", str(cm.exception))
        self.assertEqual(self.api.affinity_calls, [])

    def test_failed_restore_after_a_good_access_is_an_error(self) -> None:
        self.api.fail_restore = True
        with self.assertRaises(MsrError) as cm:
            self.port().rdmsr(0, 0x150)
        self.assertIn("restore", str(cm.exception))

    def test_failed_restore_does_not_mask_the_access_error(self) -> None:
        self.api.fail_restore = True
        self.api.fail_ioctl = OSError(errno.EIO, "boom")
        with self.assertRaises(MsrError) as cm:
            self.port().rdmsr(0, 0x150)
        self.assertIn("boom", str(cm.exception))

    def test_close_is_idempotent_and_reopens(self) -> None:
        p = self.port()
        p.rdmsr(0, 0x150)
        p.close()
        p.close()
        self.assertEqual(self.api.closed, [77])
        p.rdmsr(0, 0x150)
        self.assertEqual(len(self.api.opened), 2)


class CrossCheck(unittest.TestCase):
    def test_measured_map_passes_on_matching_data(self) -> None:
        self.assertEqual(check_lp_map(good_cpu_sets(), good_l2()), [])
        WinThrottleStopMsrPort(False, api=FakeWin32())

    def refused(self, api: FakeWin32, *needles: str) -> None:
        with self.assertRaises(MsrError) as cm:
            WinThrottleStopMsrPort(True, api=api)
        self.assertEqual(cm.exception.errno, errno.ENODEV)
        for needle in needles:
            self.assertIn(needle, str(cm.exception))
        self.assertEqual((api.opened, api.ioctls, api.affinity_calls), ([], [], []))  # fail closed, nothing touched

    def test_swapped_p_and_e_efficiency_class_fails(self) -> None:
        api = FakeWin32()
        api.sets = [CpuSet(0, cs.lp, 1 - cs.efficiency_class) for cs in api.sets]
        self.refused(api, "P-CPU 0", "E-CPU 8")

    def test_one_p_lp_in_the_wrong_class_fails(self) -> None:
        api = FakeWin32()
        lp = LP_OF_CPU[3]
        api.sets = [CpuSet(0, cs.lp, E_CLASS if cs.lp == lp else cs.efficiency_class) for cs in api.sets]
        self.refused(api, "P-CPU 3")

    def test_e_cluster_split_across_l2_caches_fails(self) -> None:
        api = FakeWin32()
        lp_a, lp_b = LP_OF_CPU[8], LP_OF_CPU[12]  # exchange one LP between two clusters
        swap = lambda m: (m & ~(1 << lp_a) | 1 << lp_b) if m >> lp_a & 1 and not m >> lp_b & 1 else m
        api.caches = [L2Cache(0, swap(c.mask)) for c in api.caches]
        self.refused(api, "E cluster")

    def test_shared_p_l2_fails(self) -> None:
        api = FakeWin32()
        api.caches = [c for c in api.caches if c.mask != 1 << LP_OF_CPU[0]] + [L2Cache(0, (1 << LP_OF_CPU[0]) | (1 << LP_OF_CPU[1]))]
        self.refused(api, "P-CPU 0")

    def test_wrong_lp_count_or_group_fails(self) -> None:
        api = FakeWin32()
        api.sets = api.sets[:-1]
        self.refused(api, "23 logical processors")
        api = FakeWin32()
        api.sets[3] = CpuSet(1, 3, 0)
        self.refused(api, "group 1")

    def test_no_l2_info_fails(self) -> None:
        api = FakeWin32()
        api.caches = []
        self.refused(api, "L2")

    def test_windows_query_failure_fails_closed(self) -> None:
        api = FakeWin32()
        api.cpu_sets = lambda: (_ for _ in ()).throw(OSError(errno.EIO, "no"))  # type: ignore[method-assign]
        self.refused(api, "cannot verify")

    def test_a_broken_lp_map_is_rejected(self) -> None:
        bad = dict(LP_OF_CPU)
        bad[1] = bad[0]
        self.assertTrue(check_lp_map(good_cpu_sets(), good_l2(), bad))
        self.assertTrue(check_lp_map(good_cpu_sets(), good_l2(), {c: c for c in range(23)}))


class Parsing(unittest.TestCase):
    def test_cpu_set_records(self) -> None:
        def rec(group: int, lp: int, eff: int) -> bytes:
            b = bytearray(32)
            struct.pack_into("<II", b, 0, 32, 0)
            struct.pack_into("<H", b, 12, group)
            b[14], b[18] = lp, eff
            return bytes(b)

        other = struct.pack("<II", 8, 9)  # an unknown record type is skipped
        self.assertEqual(winport.parse_cpu_sets(rec(0, 3, 1) + other + rec(0, 4, 0)), [CpuSet(0, 3, 1), CpuSet(0, 4, 0)])
        with self.assertRaises(ValueError):
            winport.parse_cpu_sets(struct.pack("<II", 64, 0))

    def test_l2_cache_records(self) -> None:
        def rec(level: int, mask: int, group: int = 0) -> bytes:
            b = bytearray(56)
            struct.pack_into("<II", b, 0, 2, 56)
            b[8] = level
            struct.pack_into("<Q", b, 40, mask)
            struct.pack_into("<H", b, 48, group)
            return bytes(b)

        got = winport.parse_l2_caches(rec(1, 1) + rec(2, 0b1111) + rec(3, 0xFFFFFF) + rec(2, 0b1111))
        self.assertEqual(got, [L2Cache(0, 0b1111)])


if __name__ == "__main__":
    unittest.main()
