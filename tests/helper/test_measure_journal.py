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
        self.assertEqual(set(by), {(c, a) for c in cpus for a in (CAP_ATTR, EPP_ATTR)})
        self.assertEqual({by[(c, CAP_ATTR)] for c in (2,)}, {str(self.model.max_khz(2))})  # the ORIGINAL values, not the capped ones
        self.assertEqual({by[(c, EPP_ATTR)] for c in cpus}, {ORIGINAL_EPP})
        self.assertEqual(doc["context"]["2"], {MIN_ATTR: "800000", "scaling_governor": "powersave"})  # recorded, never touched
        self.assertFalse([w for w in self.cpufreq.writes if w[1] in (MIN_ATTR, "scaling_governor")])
        self.assertNotEqual(self.read(2, EPP_ATTR), ORIGINAL_EPP)  # EPP is in force during the run
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
        in_force = {(c, a): self.read(c, a) for c in [1, 16, 17, 18, 19] for a in (CAP_ATTR, EPP_ATTR)}
        gate.set()
        h.response("m", 30)
        h.stop()
        for (c, a), v in in_force.items():  # what a crash leaves behind
            self.cpufreq.write(c, a, v)
        self.journal.write_text(journal)
        os.chmod(self.journal, 0o600)
        self.assertTrue(any(v != str(self.model.max_khz(c)) for (c, a), v in in_force.items() if a == CAP_ATTR) or self.read(1, EPP_ATTR) != ORIGINAL_EPP)
        return journal

    def test_a_new_helper_restores_a_stale_journal_before_it_serves_anything(self) -> None:
        self.die_mid_run()
        self.assertNotEqual(self.read(1, EPP_ATTR), ORIGINAL_EPP)
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

    def test_a_journal_that_cannot_be_fully_restored_is_kept_and_logged(self) -> None:
        self.die_mid_run()
        self.cpufreq.fail = lambda cpu, attr, value: cpu == 1 and attr == CAP_ATTR
        self.start_measure().call("p", "preflight")
        self.assertTrue(self.journal.exists())
        self.assertTrue(any("could NOT restore scaling_max_freq of cpu 1 " in line for line in self.logs), self.logs)
        self.assertEqual(self.read(1, EPP_ATTR), ORIGINAL_EPP)  # everything else was still restored

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
