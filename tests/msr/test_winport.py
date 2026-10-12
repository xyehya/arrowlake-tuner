"""Tests for arltuner.msr.winport. Only a fake ``Win32`` is used: no ctypes.windll, no driver, no hardware."""
from __future__ import annotations

import errno
import struct
import unittest

from arltuner.msr import MsrError, MsrPort
from arltuner.msr import winport
from arltuner.msr.winport import Affinity, CpuSet, IdentityError, L2Cache, WinThrottleStopMsrPort, check_lp_map, identity_cpus, provisional_lp_map, resolve_identity
from arltuner.profiles.fingerprint import CpuId
from arltuner.topology import arl_s_285k, synthetic

from tests.fixtures.win_reference import (
    E_CLASS,
    FUSED_POINT,
    LP_OF_CPU,
    REFERENCE_CPU_ID,
    FakeWin32,
    good_cpu_sets,
    good_l2,
    reference_fingerprint,
)


class Basics(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeWin32()

    def port(self, writable: bool = True) -> WinThrottleStopMsrPort:
        return WinThrottleStopMsrPort(writable, api=self.api, fingerprint=reference_fingerprint())

    def test_is_an_msr_port_and_imports_without_windows(self) -> None:
        self.assertIsInstance(self.port(), MsrPort)

    def test_real_api_refuses_off_windows(self) -> None:
        import sys

        if sys.platform == "win32":
            self.skipTest("Windows")
        with self.assertRaises(OSError):
            WinThrottleStopMsrPort(True, fingerprint=reference_fingerprint())

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
        with self.assertLogs("arltuner.msr.winport", level="ERROR"), self.assertRaises(MsrError) as cm:
            self.port().rdmsr(0, 0x150)
        self.assertIn("boom", str(cm.exception))

    def test_failed_restore_after_a_failed_access_is_logged_and_attached_not_swallowed(self) -> None:
        """G-141: the restore failure used to vanish when the access had failed too; the thread could stay pinned with nobody told."""
        self.api.fail_restore = True
        self.api.fail_ioctl = OSError(errno.EIO, "boom")
        with self.assertLogs("arltuner.msr.winport", level="ERROR") as logs:
            with self.assertRaises(MsrError) as cm:
                self.port().rdmsr(0, 0x150)
        self.assertIn("boom", str(cm.exception))  # the access' own error is still the one raised
        self.assertTrue(any("cannot restore the thread affinity" in note for note in cm.exception.__notes__), cm.exception.__notes__)
        self.assertTrue(any("cannot restore the thread affinity" in line for line in logs.output), logs.output)

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
        self.assertEqual(check_lp_map(good_cpu_sets(), good_l2(), LP_OF_CPU, arl_s_285k()), [])
        WinThrottleStopMsrPort(False, api=FakeWin32(), fingerprint=reference_fingerprint())

    def refused(self, api: FakeWin32, *needles: str) -> None:
        with self.assertRaises(MsrError) as cm:
            WinThrottleStopMsrPort(True, api=api, fingerprint=reference_fingerprint())
        self.assertEqual(cm.exception.errno, errno.ENODEV)
        for needle in needles:
            self.assertIn(needle, str(cm.exception))
        self.assertEqual((api.opened, api.ioctls, api.affinity_calls), ([], [], []))  # fail closed, nothing touched

    def test_swapped_p_and_e_efficiency_class_fails(self) -> None:
        api = FakeWin32()
        api.sets = [CpuSet(0, cs.lp, 1 - cs.efficiency_class) for cs in api.sets]
        self.refused(api, "16 performance + 8 efficiency")

    def test_one_p_lp_in_the_wrong_class_fails(self) -> None:
        api = FakeWin32()
        lp = LP_OF_CPU[3]
        api.sets = [CpuSet(0, cs.lp, E_CLASS if cs.lp == lp else cs.efficiency_class) for cs in api.sets]
        self.refused(api, "7 performance + 17 efficiency")

    def test_e_cluster_split_across_l2_caches_fails(self) -> None:
        api = FakeWin32()
        lp_a, lp_b = LP_OF_CPU[8], LP_OF_CPU[12]  # exchange one LP between two clusters
        swap = lambda m: (m & ~(1 << lp_a) | 1 << lp_b) if m >> lp_a & 1 and not m >> lp_b & 1 else m
        api.caches = [L2Cache(0, swap(c.mask)) for c in api.caches]
        self.refused(api, "partitioned")

    def test_shared_p_l2_fails(self) -> None:
        api = FakeWin32()
        api.caches = [c for c in api.caches if c.mask != 1 << LP_OF_CPU[0]] + [L2Cache(0, (1 << LP_OF_CPU[0]) | (1 << LP_OF_CPU[1]))]
        self.refused(api, "P-CPU 0")

    def test_wrong_lp_count_or_group_fails(self) -> None:
        api = FakeWin32()
        api.sets = api.sets[:-1]
        self.refused(api, "7 performance + 16 efficiency")
        api = FakeWin32()
        api.sets[3] = CpuSet(1, 3, 0)
        self.refused(api, "groups [0, 1]")

    def test_no_l2_info_fails(self) -> None:
        api = FakeWin32()
        api.caches = []
        self.refused(api, "L2")

    def test_windows_query_failure_fails_closed(self) -> None:
        api = FakeWin32()
        api.cpu_sets = lambda: (_ for _ in ()).throw(OSError(errno.EIO, "no"))  # type: ignore[method-assign]
        self.refused(api, "[Errno 5] no")

    def test_a_broken_lp_map_is_rejected(self) -> None:
        bad = dict(LP_OF_CPU)
        bad[1] = bad[0]
        self.assertTrue(check_lp_map(good_cpu_sets(), good_l2(), bad, arl_s_285k()))
        self.assertTrue(check_lp_map(good_cpu_sets(), good_l2(), {c: c for c in range(23)}, arl_s_285k()))


class Derivation(unittest.TestCase):
    """The map is derived from what Windows reports and the accepted state's layout: no table of any chip."""

    def test_the_reference_numbering_is_derived_from_the_fixture_data(self) -> None:
        self.assertEqual(provisional_lp_map(good_cpu_sets(), good_l2(), arl_s_285k()), dict(LP_OF_CPU))

    def windows_of(self, topo, e_first: bool, p_class: int = 3, e_class: int = 1):  # type: ignore[no-untyped-def]
        """CPU sets and L2 caches of a part whose LPs number the E cores first (or last), with arbitrary EfficiencyClass values."""
        p, e = topo.cpus_of_kind("P"), topo.cpus_of_kind("E")
        e_lps = list(range(len(e))) if e_first else list(range(len(p), len(p) + len(e)))
        p_lps = list(range(len(e), len(e) + len(p))) if e_first else list(range(len(p)))
        sets = [CpuSet(0, lp, p_class) for lp in p_lps] + [CpuSet(0, lp, e_class) for lp in e_lps]
        caches = [L2Cache(0, 1 << lp) for lp in p_lps] + [L2Cache(0, sum(1 << lp for lp in e_lps[i : i + 4])) for i in range(0, len(e_lps), 4)]
        return sets, caches, p_lps, e_lps

    def test_another_part_with_another_numbering(self) -> None:
        topo = synthetic(6, 8)
        for e_first in (False, True):
            sets, caches, p_lps, e_lps = self.windows_of(topo, e_first)
            got = provisional_lp_map(sets, caches, topo)
            self.assertEqual([got[c] for c in topo.cpus_of_kind("P")], p_lps)
            self.assertEqual([got[c] for c in topo.cpus_of_kind("E")], e_lps)
            self.assertEqual(check_lp_map(sets, caches, got, topo), [])

    def test_e_clusters_are_taken_in_the_order_of_their_lowest_lp(self) -> None:
        topo = synthetic(2, 8)
        sets, caches, _p, _e = self.windows_of(topo, False)
        caches.reverse()  # the order Windows lists the caches in does not matter
        got = provisional_lp_map(sets, caches, topo)
        self.assertEqual([got[c] for c in topo.cluster_cpus(0)], [2, 3, 4, 5])
        self.assertEqual([got[c] for c in topo.cluster_cpus(1)], [6, 7, 8, 9])

    def test_a_cluster_size_that_differs_from_the_accepted_state_fails(self) -> None:
        topo = synthetic(2, 8)
        sets, caches, _p, e_lps = self.windows_of(topo, False)
        caches = [c for c in caches if c.mask & (1 << e_lps[0]) == 0] + [L2Cache(0, 1 << e_lps[0]), L2Cache(0, sum(1 << lp for lp in e_lps[1:4]))]
        with self.assertRaisesRegex(ValueError, "sizes"):
            provisional_lp_map(sets, caches, topo)

    def test_a_different_core_count_fails(self) -> None:
        sets, caches = good_cpu_sets(), good_l2()
        with self.assertRaisesRegex(ValueError, "performance"):
            provisional_lp_map(sets, caches, synthetic(6, 16))


class Identity(unittest.TestCase):
    """``resolve_identity``: the fused words read on the provisional P slots against the recorded ones."""

    def read_with(self, order: list[int]) -> dict[int, int]:
        """Slot ``i`` of the provisional map is the silicon of Linux P-core ``order[i]``; the E clusters read as recorded."""
        read = {i: FUSED_POINT[core] for i, core in enumerate(order)}
        read.update({c: FUSED_POINT[c] for c in (8, 12, 16, 20)})
        return read

    def test_identity_ids_are_the_p_cores_and_the_first_cpu_of_each_cluster(self) -> None:
        self.assertEqual(identity_cpus(arl_s_285k()), (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20))
        self.assertEqual(identity_cpus(synthetic(6, 8)), (0, 1, 2, 3, 4, 5, 6, 10))

    def test_the_reference_order_resolves_to_the_identity(self) -> None:
        self.assertEqual(resolve_identity(reference_fingerprint(), self.read_with(list(range(8)))), {c: c for c in range(8)})

    def test_a_shuffled_p_order_resolves_to_the_right_cores(self) -> None:
        order = [3, 0, 7, 1, 6, 2, 5, 4]
        perm = resolve_identity(reference_fingerprint(), self.read_with(order))
        self.assertEqual(perm, {core: slot for slot, core in enumerate(order)})

    def test_two_p_lps_reading_the_same_word_refuse(self) -> None:
        read = self.read_with(list(range(8)))
        read[5] = read[2]
        with self.assertRaisesRegex(IdentityError, "cannot be told apart"):
            resolve_identity(reference_fingerprint(), read)

    def test_a_word_nobody_recorded_refuses(self) -> None:
        read = self.read_with(list(range(8)))
        read[4] = 0x12345
        with self.assertRaisesRegex(IdentityError, "no P-core of the accepted state recorded"):
            resolve_identity(reference_fingerprint(), read)

    def test_a_missing_read_refuses(self) -> None:
        read = self.read_with(list(range(8)))
        del read[6]
        with self.assertRaisesRegex(IdentityError, "was not read"):
            resolve_identity(reference_fingerprint(), read)

    def test_recorded_duplicates_refuse(self) -> None:
        words = dict(FUSED_POINT)
        words[3] = words[2]
        with self.assertRaisesRegex(IdentityError, "cannot be told apart"):
            resolve_identity(reference_fingerprint(words), self.read_with(list(range(8))))

    def test_an_e_cluster_with_another_word_refuses(self) -> None:
        read = self.read_with(list(range(8)))
        read[12] = FUSED_POINT[8]  # cluster 1 reads cluster 0's word
        with self.assertRaisesRegex(IdentityError, "E cluster 1"):
            resolve_identity(reference_fingerprint(), read)


class CpuIdentity(unittest.TestCase):
    def refused(self, api: FakeWin32, fingerprint, *needles: str) -> None:  # type: ignore[no-untyped-def]
        with self.assertRaises(MsrError) as cm:
            WinThrottleStopMsrPort(True, api=api, fingerprint=fingerprint)
        self.assertEqual(cm.exception.errno, errno.ENODEV)
        for needle in needles:
            self.assertIn(needle, str(cm.exception))
        self.assertEqual((api.opened, api.ioctls, api.affinity_calls), ([], [], []))

    def test_another_brand_family_or_model_refuses(self) -> None:
        for other, needle in (
            (CpuId("GenuineIntel", 6, 198, "Intel(R) Core(TM) Ultra 7 265K"), "brand"),
            (CpuId("GenuineIntel", 6, 197, REFERENCE_CPU_ID.brand), "model"),
            (CpuId("GenuineIntel", 7, 198, REFERENCE_CPU_ID.brand), "family"),
            (CpuId("AuthenticAMD", 6, 198, REFERENCE_CPU_ID.brand), "vendor"),
        ):
            api = FakeWin32()
            api.identity = other
            self.refused(api, reference_fingerprint(), "not the CPU the state was accepted on", needle)

    def test_brand_whitespace_is_not_a_difference(self) -> None:
        api = FakeWin32()
        api.identity = CpuId("GenuineIntel", 6, 198, "  Intel(R)  Core(TM) Ultra 9 285K ")
        WinThrottleStopMsrPort(True, api=api, fingerprint=reference_fingerprint())

    def test_windows_not_saying_refuses(self) -> None:
        api = FakeWin32()
        api.identity = None
        self.refused(api, reference_fingerprint(), "does not report the CPU")

    def test_a_fingerprint_without_identity_or_words_refuses_before_windows_is_asked(self) -> None:
        for fp, needle in ((reference_fingerprint(cpu_id=None), "no CPU identity"), (reference_fingerprint({c: w for c, w in FUSED_POINT.items() if c != 3}), "P-core CPU 3")):
            api = FakeWin32()
            api.cpu_sets = lambda: self.fail("Windows was asked")  # type: ignore[method-assign]
            self.refused(api, fp, needle, "re-accept on Linux")


class ApplyIdentity(unittest.TestCase):
    def test_rebinding_moves_the_p_cores_and_keeps_the_rest(self) -> None:
        api = FakeWin32()
        port = WinThrottleStopMsrPort(True, api=api, fingerprint=reference_fingerprint())
        self.assertEqual(port.lp_of_cpu, dict(LP_OF_CPU))
        port.apply_identity({0: 1, 1: 0})  # Linux P-core 0 is the silicon of slot 1 and vice versa
        self.assertEqual((port.lp_of_cpu[0], port.lp_of_cpu[1]), (LP_OF_CPU[1], LP_OF_CPU[0]))
        self.assertEqual({c: lp for c, lp in port.lp_of_cpu.items() if c > 1}, {c: lp for c, lp in LP_OF_CPU.items() if c > 1})
        port.rdmsr(0, 0x150)
        self.assertEqual(api.ioctls[-1][-1], LP_OF_CPU[1])

    def test_not_a_permutation_is_refused_and_changes_nothing(self) -> None:
        port = WinThrottleStopMsrPort(True, api=FakeWin32(), fingerprint=reference_fingerprint())
        for bad in ({0: 1}, {0: 1, 1: 1}, {0: 99, 99: 0}):
            with self.assertRaises(MsrError):
                port.apply_identity(bad)
        self.assertEqual(port.lp_of_cpu, dict(LP_OF_CPU))

    def test_a_permutation_across_p_and_e_is_refused_by_the_windows_check(self) -> None:
        port = WinThrottleStopMsrPort(True, api=FakeWin32(), fingerprint=reference_fingerprint())
        with self.assertRaises(MsrError) as cm:
            port.apply_identity({0: 8, 8: 0})
        self.assertEqual(cm.exception.errno, errno.ENODEV)
        self.assertEqual(port.lp_of_cpu, dict(LP_OF_CPU))


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
