"""G-126: a ``tune.apply`` that changes an OC ratio lifts (or lowers) ``scaling_max_freq`` with ``cpuinfo_max_freq``, except where the user set a cap or a measure/stress
journal owns the caps. Simulator + fake sysfs only: the simulated mailbox's 0x1D moves the fake ``cpuinfo_max_freq`` the way HWP highest moves the kernel's."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from arltuner.curves import CurveStore
from arltuner.helper.ops_measure import CAP_ATTR, MeasureWiring, SimModel, _restore, _write_journal, recover_journal, sim_memory

from ._measure_support import RecordingCpufreq, make_cpufreq
from .test_tune import TuneCase, row, rows

MAX_ATTR = "cpuinfo_max_freq"
E_CPUS = (12, 13, 14, 15)


class CpufreqLiftTests(TuneCase):
    def boot_with_sysfs(self) -> Any:
        h = self.boot()
        tmp = Path(tempfile.mkdtemp(dir=self.tmp))
        self.journal = tmp / "measure-journal.json"
        self.cpufreq = RecordingCpufreq(tmp / "sys", self.journal)
        make_cpufreq(tmp / "sys", SimModel())
        h.helper.measure_wiring = MeasureWiring(
            cpufreq=self.cpufreq, make_run=lambda: None, store=CurveStore(tmp / "curves"), journal_path=self.journal, mem_fn=sim_memory,  # type: ignore[arg-type]
        )

        def follow(cpu: int, khz: int) -> None:  # the kernel: cpuinfo_max_freq follows HWP highest; scaling_max_freq keeps its value
            self.cpufreq.path(cpu, MAX_ATTR).write_text(f"{khz}\n")

        self.sim.on_favored_write = follow
        self.read(h)
        return h

    def cap(self, cpu: int) -> str | None:
        return self.cpufreq.read(cpu, CAP_ATTR)

    def raise_to_49(self, h: Any) -> dict[str, Any]:
        return self.apply_ok(h, self.plan(h, rows(row("EC1", ratio=49, v=1.165))))

    def test_raise_lifts_the_cap_and_a_revert_lowers_it(self) -> None:
        h = self.boot_with_sysfs()
        self.assertEqual({self.cap(c) for c in E_CPUS}, {"4900000"})
        res = self.raise_to_49(h)
        self.assertEqual(res["cpufreq"], [{"cpu": c, "before_khz": 4_900_000, "after_khz": 5_000_000} for c in E_CPUS])
        self.assertEqual({self.cpufreq.read(c, MAX_ATTR) for c in E_CPUS}, {"5000000"})
        self.assertEqual({self.cap(c) for c in E_CPUS}, {"5000000"})
        self.assertEqual({(c, a, v) for c, a, v in self.cpufreq.writes}, {(c, CAP_ATTR, "5000000") for c in E_CPUS})  # nothing else was written, nowhere else
        self.assertEqual(self.cap(8), "4900000")  # another cluster is not touched
        self.cpufreq.writes.clear()
        res = self.apply_ok(h, self.plan(h, rows(row("EC1", ratio=48, v=1.1367))))
        self.assertEqual(res["cpufreq"], [{"cpu": c, "before_khz": 5_000_000, "after_khz": 4_900_000} for c in E_CPUS])
        self.assertEqual({self.cap(c) for c in E_CPUS}, {"4900000"})

    def test_a_user_cap_below_the_old_max_is_kept(self) -> None:
        h = self.boot_with_sysfs()
        self.cpufreq.path(13, CAP_ATTR).write_text("4000000\n")
        res = self.raise_to_49(h)
        by = {r["cpu"]: r for r in res["cpufreq"]}
        self.assertEqual(by[13], {"cpu": 13, "before_khz": 4_000_000, "after_khz": 4_000_000})
        self.assertEqual(self.cap(13), "4000000")
        self.assertNotIn(13, [c for c, _, _ in self.cpufreq.writes])
        self.assertEqual([self.cap(c) for c in (12, 14, 15)], ["5000000"] * 3)
        res = self.apply_ok(h, self.plan(h, rows(row("EC1", ratio=48, v=1.1367))))  # lowering: 4.0 GHz is already below the new max, so it stays
        self.assertEqual(self.cap(13), "4000000")
        self.assertEqual({r["cpu"]: r["after_khz"] for r in res["cpufreq"]}[13], 4_000_000)

    def test_a_cap_above_a_lowered_max_is_lowered(self) -> None:
        h = self.boot_with_sysfs()
        self.raise_to_49(h)
        self.cpufreq.path(14, CAP_ATTR).write_text("4950000\n")  # the user set 4.95 GHz: below the 5.0 max, above the coming 4.9
        res = self.apply_ok(h, self.plan(h, rows(row("EC1", ratio=48, v=1.1367))))
        self.assertEqual(self.cap(14), "4900000")
        self.assertEqual({r["cpu"]: (r["before_khz"], r["after_khz"]) for r in res["cpufreq"]}[14], (4_950_000, 4_900_000))

    def test_an_apply_without_an_oc_ratio_touches_nothing(self) -> None:
        h = self.boot_with_sysfs()
        res = self.apply_ok(h, self.plan(h, rows(row("EC1", curve=-10))))
        self.assertEqual(res["cpufreq"], [])
        self.assertEqual(self.cpufreq.writes, [])

    def test_a_journal_owns_the_caps(self) -> None:
        h = self.boot_with_sysfs()
        self.journal.write_text(json.dumps({"version": 1, "entries": []}))  # a measure/stress run (or its stale journal) holds the caps
        res = self.raise_to_49(h)
        self.assertEqual(res["cpufreq"], [{"cpu": c, "before_khz": 4_900_000, "after_khz": 4_900_000} for c in E_CPUS])
        self.assertEqual(self.cpufreq.writes, [])
        self.assertTrue(any("journal" in w for w in res["warnings"]), res["warnings"])

    def test_a_failed_cap_write_is_a_warning_not_an_error(self) -> None:
        h = self.boot_with_sysfs()
        self.cpufreq.fail = lambda cpu, attr, value: cpu == 13
        res = self.raise_to_49(h)
        by = {r["cpu"]: r for r in res["cpufreq"]}
        self.assertEqual((by[13]["before_khz"], by[13]["after_khz"]), (4_900_000, 4_900_000))
        self.assertEqual(by[12]["after_khz"], 5_000_000)
        self.assertTrue(any("cpu 13" in w for w in res["warnings"]), res["warnings"])

    def test_a_partial_apply_leaves_the_caps_alone(self) -> None:
        h = self.boot_with_sysfs()
        plan = self.plan(h, rows(row("EC1", ratio=49, v=1.165)))
        follow = self.sim.on_favored_write
        landed: list[int] = []

        def after_two(cpu: int, khz: int) -> None:  # the third 0x1D and the following ones fail
            follow(cpu, khz)  # type: ignore[misc]
            landed.append(cpu)
            if len(landed) == 2:
                self.sim.force_rc(0x1D, 3)

        self.sim.on_favored_write = after_two
        r = self.apply(h, plan)
        self.assertTrue(r["ok"], r)
        res = r["result"]
        self.assertFalse(res["complete"])
        self.assertEqual(landed, [12, 13])  # two ratios landed and their cpuinfo max moved
        self.assertEqual(self.cpufreq.read(12, MAX_ATTR), "5000000")
        self.assertEqual(res["cpufreq"], [])
        self.assertEqual(self.cpufreq.writes, [])
        self.assertEqual({self.cap(c) for c in E_CPUS}, {"4900000"})
        self.assertTrue(any("did not complete" in w for w in res["warnings"]), res["warnings"])

    def test_an_oc_word_write_that_moved_the_max_is_followed_but_an_unmoved_one_is_not_listed(self) -> None:
        """The OC word (0x11) carries the ratio too: a CPU written without a 0x1D is listed (and its cap lifted) only if its cpuinfo max moved."""
        from arltuner.helper.ops_tune import _lift_cpufreq

        h = self.boot_with_sysfs()
        before = {12: (4_900_000, 4_900_000), 13: (4_900_000, 4_900_000)}
        self.cpufreq.path(12, MAX_ATTR).write_text("5000000\n")  # cpu 12's max moved, cpu 13's did not

        class Ctx:
            import threading
            cancel = threading.Event()

        warnings: list[str] = []
        rows_ = _lift_cpufreq(h.helper, Ctx(), before, [], [12, 13], warnings)  # type: ignore[arg-type]
        self.assertEqual(rows_, [{"cpu": 12, "before_khz": 4_900_000, "after_khz": 5_000_000}])
        self.assertEqual((self.cap(12), self.cap(13)), ("5000000", "4900000"))

    def test_without_measurement_wiring_the_result_still_has_the_key(self) -> None:
        h = self.boot()
        self.read(h)
        res = self.raise_to_49(h)
        self.assertEqual(res["cpufreq"], [])


class JournalRestoreRuleTests(TuneCase):
    def test_a_cap_journaled_at_the_max_restores_to_the_current_max(self) -> None:
        tmp = Path(tempfile.mkdtemp(dir=self.tmp))
        journal = tmp / "measure-journal.json"
        cpufreq = RecordingCpufreq(tmp / "sys", journal)
        make_cpufreq(tmp / "sys", SimModel())
        w = MeasureWiring(cpufreq=cpufreq, make_run=lambda: None, store=CurveStore(tmp / "curves"), journal_path=journal, mem_fn=sim_memory)  # type: ignore[arg-type]
        cpufreq.path(13, CAP_ATTR).write_text("4000000\n")  # a user cap: journaled below the max
        entries = _write_journal(w, [12, 13], None)
        self.assertTrue(next(e for e in entries if e["cpu"] == 12 and e["attr"] == CAP_ATTR).get("at_max"))
        self.assertFalse(next(e for e in entries if e["cpu"] == 13 and e["attr"] == CAP_ATTR).get("at_max"))
        cpufreq.path(12, MAX_ATTR).write_text("5000000\n")  # an OC raise moved the max while the run held the caps
        cpufreq.path(13, MAX_ATTR).write_text("5000000\n")
        cpufreq.path(12, CAP_ATTR).write_text("3000000\n")
        cpufreq.path(13, CAP_ATTR).write_text("3000000\n")
        logs: list[str] = []
        self.assertTrue(recover_journal(w, logs.append))  # the stale-journal path: entries pass through the validator, the flag survives
        self.assertEqual(cpufreq.read(12, CAP_ATTR), "5000000")
        self.assertEqual(cpufreq.read(13, CAP_ATTR), "4000000")
        self.assertFalse(journal.exists())
        self.assertEqual(_restore(w, entries), [])
