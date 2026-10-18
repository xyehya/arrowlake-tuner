"""G-216: the runtime controls (``arltuner.controls.caps`` / ``parking`` / ``uncore``) -- the plan rules, the read-back rule, the files, on a fake sysfs tree. No hardware, no /sys."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from arltuner.controls import caps as cp
from arltuner.controls import parking as pk
from arltuner.controls import uncore as un
from arltuner.guard import load_policy
from arltuner.topology import arl_s_285k, synthetic
from arltuner.topology.cpumap import Topology

from tests.fakesys import make_runtime_sysfs

TOPO = arl_s_285k()  # P = CPU 0-7, E = CPU 8-23 in clusters of four
FULL = "[runtime]\ncap_ratio_min = 8\nuncore_ratio = [10, 38]\nparking = true\n"
UNCORE = "intel_uncore_frequency/package_00_die_00/"


def policy(runtime: str = FULL):
    return load_policy('cpus = "auto"\n' + runtime, TOPO)


def tree(case: unittest.TestCase, **kw) -> Path:
    path = Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, path, True)
    return make_runtime_sysfs(path, **kw)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tree(self)
        self.files = cp.RuntimeFiles(self.root)
        self.cpus = list(range(24))

    def freq(self):
        return cp.read_state(self.files, self.cpus)

    def online(self):
        return pk.read_state(self.files, self.cpus)

    def refusal(self, fn, *args, **kw) -> cp.Refused:
        with self.assertRaises(cp.Refused) as cm:
            fn(*args, **kw)
        return cm.exception


class Files(Case):
    def test_the_real_layout_is_read(self) -> None:
        f = self.freq()
        self.assertEqual((f[0].max_khz, f[0].cap_khz, f[0].min_khz), (5_700_000, 5_700_000, 800_000))
        self.assertEqual(f[8].max_khz, 4_900_000)
        self.assertIsNone(self.online()[0])  # cpu0 has no online file
        self.assertTrue(self.online()[5])
        st = un.read_state(self.files)
        self.assertEqual((st.domain, st.min_khz, st.max_khz, st.initial_min_khz, st.initial_max_khz), ("package_00_die_00", 800_000, 4_000_000, 800_000, 4_000_000))

    def test_a_write_replaces_the_whole_value(self) -> None:
        self.files.write_int("cpu3/cpufreq/scaling_max_freq", 4_500_000)
        self.files.write_int("cpu3/cpufreq/scaling_max_freq", 900_000)  # shorter than the old text: no stale tail
        self.assertEqual(self.files.read_int("cpu3/cpufreq/scaling_max_freq"), 900_000)

    def test_a_missing_file_reads_none_and_a_write_never_creates_it(self) -> None:
        self.assertIsNone(self.files.read_text("cpu0/online"))
        with self.assertRaises(cp.SysfsError):
            self.files.write_int("cpu0/online", 0)
        self.assertFalse((self.root / "cpu0" / "online").exists())

    def test_a_link_is_not_followed(self) -> None:
        target = self.root / "elsewhere"
        target.write_text("1\n")
        os.symlink(target, self.root / "cpu0" / "online")
        with self.assertRaises(cp.SysfsError):
            self.files.write_int("cpu0/online", 0)
        self.assertEqual(target.read_text(), "1\n")

    def test_garbage_is_an_error(self) -> None:
        (self.root / "cpu2" / "online").write_text("maybe\n")
        with self.assertRaises(cp.SysfsError):
            self.files.read_int("cpu2/online")
        (self.root / "cpu2" / "online").write_text("7\n")
        self.assertEqual(self.refusal(pk.read_state, self.files, [2]).check, "PARK_STATE")


class Caps(Case):
    def plan(self, intent, pol=None, **kw):
        return cp.plan(intent, pol or policy(), self.freq(), TOPO, **kw)

    def test_a_p_core_cap_is_one_step_in_khz(self) -> None:
        (s,) = self.plan({3: 45})
        self.assertEqual((s.domain, s.key, s.file, s.before, s.value, s.expect), ("caps", "cpu3", "cpu3/cpufreq/scaling_max_freq", 5_700_000, 4_500_000, (4_500_000, 4_500_000)))

    def test_an_e_core_cap_is_written_to_all_four_cpus_of_its_cluster(self) -> None:
        steps = self.plan({10: 40})
        self.assertEqual([s.key for s in steps], ["cpu8", "cpu9", "cpu10", "cpu11"])
        self.assertEqual({s.value for s in steps}, {4_000_000})
        self.assertEqual([s.number for s in steps], [1, 2, 3, 4])

    def test_the_cluster_comes_from_the_topology(self) -> None:
        small = synthetic(2, 4, favored_p=(0,))  # CPU 0-1 P, 2-5 one E cluster
        self.assertEqual(small.cluster_cpus(0), (2, 3, 4, 5))
        steps = cp.plan({3: 40}, policy(), {c: cp.CpuFreq(4_900_000, 4_900_000, 800_000) for c in range(6)}, small)
        self.assertEqual([s.key for s in steps], ["cpu2", "cpu3", "cpu4", "cpu5"])

    def test_two_ratios_in_one_cluster_are_refused(self) -> None:
        self.assertEqual(self.refusal(self.plan, {8: 40, 11: 42}).check, "CAPS_CLUSTER_CONFLICT")
        self.assertEqual(len(self.plan({8: 40, 11: 40})), 4)  # the same ratio twice is fine

    def test_a_cap_above_the_cpus_own_maximum_is_refused(self) -> None:
        e = self.refusal(self.plan, {3: 58})
        self.assertEqual(e.check, "CAPS_RANGE")
        self.assertIn("cpuinfo_max_freq", str(e))
        self.assertEqual(self.refusal(self.plan, {8: 50}).check, "CAPS_RANGE")  # an E cluster here tops out at 49

    def test_a_cap_at_the_maximum_changes_nothing(self) -> None:
        self.assertEqual(self.plan({3: 57}), [])

    def test_the_floor_is_the_policy_key_and_an_absent_key_refuses_naming_it(self) -> None:
        self.assertEqual(self.refusal(self.plan, {3: 7}).check, "CAPS_RANGE")
        for pol in (policy(""), policy("[runtime]\nparking = true\n")):
            e = self.refusal(self.plan, {3: 45}, pol)
            self.assertEqual((e.check, e.key), ("RUNTIME_POLICY_MISSING", "cap_ratio_min"))
            self.assertIn("[runtime] cap_ratio_min", str(e))

    def test_a_ratio_below_the_cpus_scaling_min_is_refused(self) -> None:
        low = policy("[runtime]\ncap_ratio_min = 1\n")
        self.assertEqual(self.refusal(self.plan, {3: 5}, low).check, "CAPS_BELOW_MIN")  # 500 MHz < scaling_min_freq 800 MHz: the kernel would refuse
        self.assertEqual(self.plan({3: 8}, low)[0].value, 800_000)

    def test_a_held_cap_journal_is_a_named_conflict(self) -> None:
        e = self.refusal(self.plan, {3: 45}, held="the cpufreq journal /run/arltuner/measure-journal.json")
        self.assertEqual(e.check, "CAPS_CONFLICT")
        self.assertIn("measure-journal.json", str(e))

    def test_unknown_cpu_and_bad_values_are_refused(self) -> None:
        self.assertEqual(self.refusal(self.plan, {99: 40}).check, "CAPS_UNKNOWN_CPU")
        self.assertEqual(self.refusal(self.plan, {3: 0}).check, "CAPS_VALUE")
        self.assertEqual(self.refusal(self.plan, {3: True}).check, "CAPS_VALUE")
        self.assertEqual(self.refusal(self.plan, {}).check, "NOTHING_TO_DO")

    def test_an_offline_cpu_cannot_be_capped(self) -> None:
        state = cp.read_state(cp.RuntimeFiles(tree(self, offline=[5])), self.cpus)
        self.assertIsNone(state[5])
        self.assertEqual(self.refusal(cp.plan, {5: 40}, policy(), state, TOPO).check, "CAPS_NO_FILE")

    def test_the_policys_cpu_allowlist_bounds_caps_including_the_cluster_expansion(self) -> None:
        narrow = load_policy('cpus = ["0-8"]\n' + FULL, TOPO)
        self.assertEqual(self.plan({3: 45}, narrow)[0].key, "cpu3")
        e = self.refusal(self.plan, {8: 40}, narrow)  # cpu 8 is allowed, but its cluster (9-11) is not
        self.assertEqual((e.check, e.key), ("CAPS_CPU_NOT_ALLOWED", "cpu9"))
        self.assertEqual(self.refusal(self.plan, {12: 40}, narrow).check, "CAPS_CPU_NOT_ALLOWED")

    def test_a_cpu_parked_in_the_same_intent_cannot_be_capped(self) -> None:
        self.assertEqual(self.refusal(self.plan, {5: 40}, parked=frozenset({5})).check, "CAPS_PARKED")

    def test_revert_restores_journaled_caps_and_skips_offline_cpus(self) -> None:
        self.files.write_int("cpu3/cpufreq/scaling_max_freq", 4_500_000)
        steps, notes = cp.plan_revert({3: 5_700_000, 4: 5_700_000}, self.freq())
        self.assertEqual([(s.key, s.before, s.value) for s in steps], [("cpu3", 4_500_000, 5_700_000)])  # cpu4 is already there
        state = dict(self.freq())
        state[3] = None
        steps, notes = cp.plan_revert({3: 5_700_000}, state)
        self.assertEqual((steps, len(notes)), ([], 1))
        self.assertEqual(self.refusal(cp.plan_revert, {3: 9_000_000}, self.freq()).check, "CAPS_RANGE")

    def test_the_read_back_is_exact(self) -> None:
        (s,) = self.plan({3: 45})
        self.assertIsNone(cp.readback_problem(s, 4_500_000))
        self.assertIn("reads 4600000", cp.readback_problem(s, 4_600_000))
        self.assertIn("cannot be read back", cp.readback_problem(s, None))


class Parking(Case):
    def plan(self, intent, pol=None, online=None, **kw):
        return pk.plan(intent, pol or policy(), self.online() if online is None else online, TOPO, **kw)

    def test_park_and_unpark_steps(self) -> None:
        (s,) = self.plan({12: True})
        self.assertEqual((s.domain, s.key, s.file, s.before, s.value), ("parking", "cpu12", "cpu12/online", 1, 0))
        self.files.write_int("cpu12/online", 0)
        (s,) = self.plan({12: False})
        self.assertEqual((s.before, s.value), (0, 1))

    def test_cpu0_is_refused_by_name(self) -> None:
        e = self.refusal(self.plan, {0: True})
        self.assertEqual((e.check, e.key), ("PARK_CPU0", "cpu0"))
        self.assertEqual(self.plan({0: False}), [])  # asking the boot CPU online is a no-op

    def test_even_with_an_online_file_cpu0_is_never_parked(self) -> None:
        online = dict(self.online())
        online[0] = True
        self.assertEqual(self.refusal(self.plan, {0: True}, online=online).check, "PARK_CPU0")

    def test_the_last_online_cpu_of_a_kind_is_refused(self) -> None:
        for cpu in range(8, 23):
            self.files.write_int(f"cpu{cpu}/online", 0)
        e = self.refusal(self.plan, {23: True})
        self.assertEqual(e.check, "PARK_LAST_OF_KIND")
        self.assertIn("no E CPU online", str(e))
        self.assertEqual([s.key for s in self.plan({23: True, 22: False})], ["cpu22", "cpu23"])  # unparking one makes room

    def test_the_rule_is_cumulative_within_one_plan(self) -> None:
        self.assertEqual(self.refusal(self.plan, dict.fromkeys(range(8, 24), True)).check, "PARK_LAST_OF_KIND")
        self.assertEqual(len(self.plan(dict.fromkeys(range(8, 23), True))), 15)

    def test_the_p_kind_is_protected_in_a_topology_whose_p_cpus_can_all_go(self) -> None:
        topo = synthetic(2, 4, favored_p=(0,))  # P = CPU 0, 1
        online = {0: True, 1: True, 2: True, 3: True, 4: True, 5: True}  # (a kernel that does offer cpu0/online)
        e = self.refusal(pk.plan, {1: True, 0: True}, policy(), online, topo)
        self.assertEqual(e.check, "PARK_CPU0")  # cpu0 first, by name
        online[0] = None
        self.assertEqual(self.refusal(pk.plan, {1: True, 0: True}, policy(), online, topo).check, "PARK_CPU0")
        self.assertEqual(len(pk.plan({1: True}, policy(), online, topo)), 1)  # cpu0 stays, so a P CPU stays

    def test_parking_needs_the_policy_key_but_unparking_does_not(self) -> None:
        for pol in (policy(""), policy("[runtime]\ncap_ratio_min = 8\n")):
            e = self.refusal(self.plan, {12: True}, pol)
            self.assertEqual((e.check, e.key), ("RUNTIME_POLICY_MISSING", "parking"))
            self.assertIn("[runtime] parking", str(e))
        self.assertEqual(self.refusal(self.plan, {12: True}, policy("[runtime]\nparking = false\n")).check, "RUNTIME_POLICY_DENIED")
        self.files.write_int("cpu12/online", 0)
        (s,) = self.plan({12: False}, policy(""))
        self.assertEqual(s.value, 1)

    def test_the_policys_cpu_allowlist_bounds_parking_but_not_unparking(self) -> None:
        narrow = load_policy('cpus = ["0-8"]\n' + FULL, TOPO)
        self.assertEqual(self.plan({8: True}, narrow)[0].key, "cpu8")
        self.assertEqual(self.refusal(self.plan, {12: True}, narrow).check, "PARK_CPU_NOT_ALLOWED")
        self.files.write_int("cpu12/online", 0)
        self.assertEqual(self.plan({12: False}, narrow)[0].value, 1)

    def test_nothing_is_parked_while_something_holds_the_caps(self) -> None:
        e = self.refusal(self.plan, {12: True}, held="the running stress.start (s1)")
        self.assertEqual(e.check, "PARK_CONFLICT")
        self.assertIn("stress.start", str(e))
        self.files.write_int("cpu12/online", 0)
        self.assertEqual(self.plan({12: False}, held="the running stress.start (s1)")[0].value, 1)  # unparking is always allowed

    def test_unknown_cpu_and_bad_values(self) -> None:
        self.assertEqual(self.refusal(self.plan, {99: True}).check, "PARK_UNKNOWN_CPU")
        self.assertEqual(self.refusal(self.plan, {3: 1}).check, "PARK_VALUE")
        self.assertEqual(self.refusal(self.plan, {}).check, "NOTHING_TO_DO")

    def test_a_parked_cpu_that_left_the_topology_can_still_be_unparked(self) -> None:
        self.files.write_int("cpu12/online", 0)
        shrunk = Topology(tuple(c for c in TOPO.cores if c.cpu != 12))
        (s,) = pk.plan({12: False}, policy(""), self.online(), shrunk)
        self.assertEqual((s.key, s.value), ("cpu12", 1))
        self.assertEqual(self.refusal(pk.plan, {12: True}, policy(), self.online(), shrunk).check, "PARK_UNKNOWN_CPU")

    def test_unparks_come_before_parks(self) -> None:
        self.files.write_int("cpu12/online", 0)
        self.assertEqual([(s.key, s.value) for s in self.plan({13: True, 12: False})], [("cpu12", 1), ("cpu13", 0)])

    def test_revert_unparks_first_and_needs_no_policy(self) -> None:
        self.files.write_int("cpu12/online", 0)
        steps, notes = pk.plan_revert({12: 1, 13: 0, 0: 1}, self.online(), TOPO)
        self.assertEqual(([(s.key, s.before, s.value) for s in steps], notes), ([("cpu12", 0, 1), ("cpu13", 1, 0)], []))

    def test_revert_never_parks_the_last_online_cpu_of_a_kind(self) -> None:
        for cpu in range(9, 24):  # something else took all but cpu 8 offline since the journal was written
            self.files.write_int(f"cpu{cpu}/online", 0)
        steps, notes = pk.plan_revert({8: 0, 9: 1}, self.online(), TOPO)  # the journal: cpu8 was offline, cpu9 online
        self.assertEqual([(s.key, s.value) for s in steps], [("cpu9", 1), ("cpu8", 0)])  # the unpark makes room, so the park is fine
        steps, notes = pk.plan_revert({8: 0}, self.online(), TOPO)
        self.assertEqual(steps, [])
        self.assertIn("last online E CPU", notes[0])


class Uncore(Case):
    def plan(self, intent, pol=None):
        return un.plan(intent, pol or policy(), un.read_state(self.files))

    def test_a_max_change_is_one_step_in_khz(self) -> None:
        (s,) = self.plan({"max_ratio": 35})
        self.assertEqual((s.domain, s.key, s.file, s.before, s.value), ("uncore", "max_ratio", UNCORE + "max_freq_khz", 4_000_000, 3_500_000))

    def test_bounds_are_the_hardwares_and_the_policys(self) -> None:
        wide = policy("[runtime]\nuncore_ratio = [1, 60]\n")
        self.assertIn("hardware's own", str(self.refusal(self.plan, {"max_ratio": 41}, wide)))  # above initial_max 40
        self.assertEqual(self.refusal(self.plan, {"min_ratio": 7}, wide).check, "UNCORE_RANGE")  # below initial_min 8
        self.assertIn("[runtime] uncore_ratio = [10, 38]", str(self.refusal(self.plan, {"max_ratio": 39})))
        self.assertEqual(self.refusal(self.plan, {"min_ratio": 9}).check, "UNCORE_RANGE")

    def test_an_absent_key_refuses_naming_it(self) -> None:
        e = self.refusal(self.plan, {"max_ratio": 35}, policy(""))
        self.assertEqual((e.check, e.key), ("RUNTIME_POLICY_MISSING", "uncore_ratio"))
        self.assertIn("[runtime] uncore_ratio", str(e))

    def test_min_never_exceeds_max(self) -> None:
        self.assertEqual(self.refusal(self.plan, {"min_ratio": 30, "max_ratio": 20}).check, "UNCORE_ORDER")
        self.files.write_int(UNCORE + "max_freq_khz", 3_000_000)
        self.assertEqual(self.refusal(self.plan, {"min_ratio": 35}).check, "UNCORE_ORDER")  # alone: above the maximum in force (30)

    def test_the_write_order(self) -> None:
        self.files.write_int(UNCORE + "max_freq_khz", 3_000_000)
        self.assertEqual([s.key for s in self.plan({"min_ratio": 20, "max_ratio": 25})], ["min_ratio", "max_ratio"])  # both below the old max
        self.assertEqual([s.key for s in self.plan({"min_ratio": 36, "max_ratio": 38})], ["max_ratio", "min_ratio"])  # the new min is above the old max: max first

    def test_every_single_write_keeps_the_pair_ordered(self) -> None:
        wide = policy("[runtime]\nuncore_ratio = [8, 40]\n")
        for lo0, hi0 in ((8, 40), (20, 30), (30, 38), (8, 8), (40, 40)):
            for lo1, hi1 in ((8, 40), (10, 12), (36, 38), (20, 30), (25, 25), (8, 8), (40, 40)):
                st = un.UncoreState("package_00_die_00", lo0 * 100_000, hi0 * 100_000, 800_000, 4_000_000)
                for s in un.plan({"min_ratio": lo1, "max_ratio": hi1}, wide, st):
                    pair = [st.min_khz, st.max_khz]
                    pair[0 if s.key == "min_ratio" else 1] = s.value
                    st = un.UncoreState(st.domain, pair[0], pair[1], st.initial_min_khz, st.initial_max_khz)
                    self.assertLessEqual(st.min_khz, st.max_khz, (lo0, hi0, lo1, hi1, s.key))

    def test_no_domain_and_two_domains_are_errors(self) -> None:
        with self.assertRaises(cp.SysfsError):
            un.read_state(cp.RuntimeFiles(tree(self, uncore=None)))
        two = tree(self)
        shutil.copytree(two / "intel_uncore_frequency" / "package_00_die_00", two / "intel_uncore_frequency" / "package_01_die_00")
        with self.assertRaises(cp.SysfsError) as cm:
            un.read_state(cp.RuntimeFiles(two))
        self.assertIn("exactly one", str(cm.exception))

    def test_a_domain_without_initial_limits_is_an_error(self) -> None:
        (self.root / "intel_uncore_frequency" / "package_00_die_00" / "initial_max_freq_khz").unlink()
        with self.assertRaises(cp.SysfsError):
            un.read_state(self.files)

    def test_revert_restores_inside_the_hardware_range(self) -> None:
        self.files.write_int(UNCORE + "max_freq_khz", 3_000_000)
        st = un.read_state(self.files)
        (s,) = un.plan_revert({"min_ratio": 800_000, "max_ratio": 4_000_000}, st)
        self.assertEqual((s.key, s.before, s.value), ("max_ratio", 3_000_000, 4_000_000))
        self.assertEqual(self.refusal(un.plan_revert, {"max_ratio": 5_000_000}, st).check, "UNCORE_RANGE")


if __name__ == "__main__":
    unittest.main()
