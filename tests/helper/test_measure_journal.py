"""The measurement journal (SPEC-v3 section 3): written before the first change, restored on success / cancel / error and at the next helper start."""
from __future__ import annotations

import json
import os
import stat
import threading
import unittest

from arltuner.helper.ops_measure import CAP_ATTR, EPP_ATTR, MIN_ATTR, recover_journal

from ._measure_support import ORIGINAL_EPP, MeasureCase


class JournalFileTests(MeasureCase):
    def test_the_journal_is_complete_and_private_while_the_run_is_in_progress(self) -> None:
        reached, gate = threading.Event(), threading.Event()

        def hook(_s: float) -> None:
            reached.set()
            gate.wait(10)

        self.sleep_hook = hook
        h = self.start_measure()
        h.send({"id": "m", "op": "vf.measure", "args": {"units": ["P2", "EC1"]}})
        self.assertTrue(reached.wait(10))
        st = os.stat(self.journal)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        doc = json.loads(self.journal.read_text())
        by = {(e["cpu"], e["attr"]): e["value"] for e in doc["entries"]}
        cpus = [2, 12, 13, 14, 15]
        self.assertEqual(set(by), {(c, a) for c in cpus for a in (CAP_ATTR, MIN_ATTR)})  # G-132: max and min are journaled, EPP is not touched
        self.assertEqual({by[(c, CAP_ATTR)] for c in (2,)}, {str(self.model.max_khz(2))})  # the ORIGINAL values, not the pinned ones
        self.assertEqual({by[(c, MIN_ATTR)] for c in cpus}, {"800000"})
        self.assertEqual(doc["context"]["2"], {"scaling_governor": "powersave"})  # recorded, never touched
        self.assertFalse([w for w in self.cpufreq.writes if w[1] in (EPP_ATTR, "scaling_governor")])
        self.assertEqual(self.read(2, EPP_ATTR), ORIGINAL_EPP)
        self.assertEqual(self.read(2, MIN_ATTR), self.read(2, CAP_ATTR))  # pinned: min = max while the point is measured
        gate.set()
        self.assertTrue(h.response("m", 30)["ok"])
        self.assert_cpufreq_pristine()

    def test_a_journal_that_cannot_be_written_stops_the_run_before_any_change(self) -> None:
        h = self.start_measure(self.wiring(journal_path=self.tmp / "sub" / "missing-dir" / "journal.json"))
        reply = h.call("m", "vf.measure", {"units": ["P0"]})
        self.assertEqual((reply["ok"], reply["error"]["code"]), (False, "REFUSED"))
        self.assertEqual(self.cpufreq.writes, [])
        self.assert_cpufreq_pristine()

    def test_a_symlinked_journal_path_is_not_followed(self) -> None:
        target = self.tmp / "victim"
        target.write_text("keep\n")
        self.journal.symlink_to(target)
        logs: list[str] = []
        h = self.start_measure()
        # recovery must not follow it
        self.assertTrue(recover_journal(h.helper.measure_wiring, logs.append))
        self.assertTrue(any("not trusted" in line for line in logs), logs)
        self.assertEqual(target.read_text(), "keep\n")


class RecoveryTests(MeasureCase):
    """The helper died mid-run (kill -9, power loss of the GUI, a crash): caps are lowered, the journal is on disk, the spinners are gone."""

    def die_mid_run(self) -> str:
        """Run until the load is on, copy the journal and the in-force state, then let the run end normally; afterwards put the dead state back (a crash does not restore)."""
        reached, gate = threading.Event(), threading.Event()
        calls = {"n": 0}

        def hook(_s: float) -> None:
            calls["n"] += 1
            if calls["n"] == 12:
                reached.set()
                gate.wait(10)

        self.sleep_hook = hook
        h = self.start_measure()
        h.send({"id": "m", "op": "vf.measure", "args": {"units": ["P1", "EC2"]}})
        self.assertTrue(reached.wait(10))
        journal = self.journal.read_text()
        in_force = {(c, a): self.read(c, a) for c in [1, 16, 17, 18, 19] for a in (CAP_ATTR, MIN_ATTR)}
        gate.set()
        h.response("m", 30)
        h.stop()
        for (c, a), v in in_force.items():  # what a crash leaves behind
            self.cpufreq.write(c, a, v)
        self.journal.write_text(journal)
        os.chmod(self.journal, 0o600)
        self.assertTrue(any(v != str(self.model.max_khz(c)) for (c, a), v in in_force.items() if a == CAP_ATTR))
        self.assertTrue(any(v != "800000" for (c, a), v in in_force.items() if a == MIN_ATTR))
        return journal

    def test_a_new_helper_restores_a_stale_journal_before_it_serves_anything(self) -> None:
        self.die_mid_run()
        self.assertNotEqual(self.read(1, MIN_ATTR), "800000")
        h = self.start_measure()
        h.call("p", "preflight")  # the dispatch loop is up, so recovery (before it) has finished
        self.assert_cpufreq_pristine()
        self.assertTrue(any("stale measure journal applied" in line for line in self.logs), self.logs)

    def test_recovery_leaves_values_alone_that_the_journal_does_not_own(self) -> None:
        self.die_mid_run()
        self.cpufreq.write(5, "scaling_governor", "performance")  # the operator changed something else after the crash
        self.cpufreq.write(5, MIN_ATTR, "1200000")
        self.start_measure().call("p", "preflight")
        self.assertEqual(self.read(5, "scaling_governor"), "performance")
        self.assertEqual(self.read(5, MIN_ATTR), "1200000")
        self.assertEqual(self.read(1, EPP_ATTR), ORIGINAL_EPP)

    def test_a_journal_of_the_previous_helper_version_with_epp_entries_is_still_restored(self) -> None:
        self.cpufreq.write(3, EPP_ATTR, "balance_performance")
        self.cpufreq.write(3, CAP_ATTR, "3000000")
        self.journal.write_text(json.dumps({"version": 1, "pid": 1, "boot_id": None, "created": 0, "context": {}, "entries": [
            {"cpu": 3, "attr": CAP_ATTR, "value": str(self.model.max_khz(3)), "at_max": True}, {"cpu": 3, "attr": EPP_ATTR, "value": ORIGINAL_EPP}]}))
        os.chmod(self.journal, 0o600)
        self.start_measure().call("p", "preflight")
        self.assertEqual((self.read(3, CAP_ATTR), self.read(3, EPP_ATTR)), (str(self.model.max_khz(3)), ORIGINAL_EPP))
        self.assertFalse(self.journal.exists())

    def test_a_journal_that_cannot_be_fully_restored_is_kept_and_logged(self) -> None:
        self.die_mid_run()
        self.cpufreq.fail = lambda cpu, attr, value: cpu == 1 and attr == CAP_ATTR
        self.start_measure().call("p", "preflight")
        self.assertTrue(self.journal.exists())
        self.assertTrue(any("could NOT restore scaling_max_freq of cpu 1 " in line for line in self.logs), self.logs)
        self.assertEqual(self.read(1, MIN_ATTR), "800000")  # everything else was still restored

    def test_a_measurement_restores_a_stale_journal_first_and_never_journals_a_dead_runs_caps(self) -> None:
        """G-141: the journal used to be overwritten with the CURRENT (still capped) values as the "originals", so the run ended by restoring the caps of the dead run."""
        self.die_mid_run()
        self.cpufreq.fail = lambda cpu, attr, value: cpu == 1 and attr == CAP_ATTR  # the start-up recovery cannot finish: the journal is kept
        h = self.start_measure()
        h.call("p", "preflight")
        self.assertTrue(self.journal.exists())
        self.assertNotEqual(self.read(1, CAP_ATTR), str(self.model.max_khz(1)))
        self.cpufreq.fail = None  # fixed by hand / works now
        reply = h.call("m2", "vf.measure", {"units": ["P1"]}, timeout=30)
        self.assertTrue(reply["ok"], reply)
        self.assert_cpufreq_pristine()
        self.assertFalse(self.journal.exists())

    def test_a_measurement_refuses_when_the_stale_journal_cannot_be_restored(self) -> None:
        self.die_mid_run()
        self.cpufreq.fail = lambda cpu, attr, value: cpu == 1 and attr == CAP_ATTR
        h = self.start_measure()
        h.call("p", "preflight")
        before = self.journal.read_text()
        reply = h.call("m3", "vf.measure", {"units": ["P1"]}, timeout=30)
        self.assertEqual((reply["ok"], reply["error"]["code"], reply["error"]["detail"]["check"]), (False, "REFUSED", "STALE_JOURNAL"))
        self.assertEqual(self.journal.read_text(), before)  # the dead run's journal is untouched
        self.assertEqual([w for w in self.cpufreq.writes[-1:] if w[0] == 1 and w[1] == CAP_ATTR], [])

    def test_recovery_rejects_foreign_content_in_the_journal(self) -> None:
        bad = {"version": 1, "entries": [{"cpu": 0, "attr": "scaling_governor", "value": "performance"}]}
        self.journal.write_text(json.dumps(bad))
        os.chmod(self.journal, 0o600)
        logs: list[str] = []
        self.assertTrue(recover_journal(self.wiring(), logs.append))
        self.assertTrue(self.journal.exists())
        self.assertTrue(any("unusable" in line for line in logs), logs)
        self.assertEqual(self.read(0, "scaling_governor"), "powersave")
        self.journal.write_text("not json")
        self.assertTrue(recover_journal(self.wiring(), logs.append))
        self.assertTrue(self.journal.exists())

    def test_a_group_writable_journal_is_not_trusted(self) -> None:
        self.die_mid_run()
        os.chmod(self.journal, 0o664)
        logs: list[str] = []
        self.assertTrue(recover_journal(self.wiring(), logs.append))
        self.assertTrue(any("not trusted" in line for line in logs), logs)
        self.assertTrue(self.journal.exists())

    def test_no_journal_is_a_normal_start(self) -> None:
        self.assertFalse(recover_journal(self.wiring(), self.logs.append))
        self.start_measure().call("p", "preflight")
        self.assertEqual(self.logs, [])


if __name__ == "__main__":
    unittest.main()
