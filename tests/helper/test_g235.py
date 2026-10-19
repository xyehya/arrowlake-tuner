"""G-235: the audit trail of the ops that change cpufreq or a file under the state directory: ``vf.measure`` and ``stress.start`` (a ``runtime_attempt`` per cpufreq write, one
``result`` at the end), ``profiles.save``, ``curves.update`` / ``curves.delete`` (a ``result``). None of it is a mailbox ``attempt``. Simulator, fake sysfs, temp dirs."""
from __future__ import annotations

import json
import os
import threading
import unittest
from typing import Any

from arltuner.guard import read_state

from ._support import error_code
from .test_accept import AcceptCase
from .test_stress import START, StressCase
from .test_tune import row, rows
from ._measure_support import MeasureCase

BOOT = "11111111-2222-3333-4444-555555555555"


def audit_records(case: Any) -> list[dict[str, Any]]:
    if not case.audit.exists():
        return []
    return [json.loads(line) for line in case.audit.read_text(encoding="utf-8").splitlines() if line.strip()]


def attempts(case: Any) -> int:
    return read_state(case.audit).attempts_by_boot.get(BOOT, 0)


def results(case: Any, op: str) -> list[dict[str, Any]]:
    return [r for r in audit_records(case) if r["event"] == "result" and r.get("op") == op]


def lock_audit(case: Any) -> None:
    case.audit.touch(mode=0o600)
    case.audit.chmod(0o400)
    case.addCleanup(case.audit.chmod, 0o600)


class MeasureAudit(MeasureCase):
    def test_every_cap_write_is_audited_first_and_the_run_ends_with_one_result(self) -> None:
        h = self.start_measure()
        reply = h.call("m", "vf.measure", {"mode": "quick", "units": ["P1", "EC0"]}, timeout=60)
        self.assertTrue(reply["ok"], reply)
        writes = [r for r in audit_records(self) if r["event"] == "runtime_attempt"]
        self.assertGreater(len(self.cpufreq.writes), 0)
        self.assertEqual(len(writes), len(self.cpufreq.writes), "one record per cpufreq file write, the restore included")
        self.assertEqual({r["op"] for r in writes}, {"vf.measure"})
        self.assertEqual({r["kind"] for r in writes}, {"cpufreq"})
        self.assertEqual({r["key"] for r in writes}, {CAP, MIN})
        self.assertEqual({r["boot_id"] for r in writes}, {BOOT})
        self.assertTrue(all(r["detail"].startswith("cpufreq write cpu") and r["path"].startswith("cpu") for r in writes))
        self.assertEqual([w["path"] for w in writes], [f"cpu{c}/cpufreq/{a}" for c, a, _ in self.cpufreq.writes], "audited in the order sent")
        (end,) = results(self, "vf.measure")
        self.assertEqual((end["outcome"], end["curve_id"], end["units"], end["restored"], end["writes"]), ("complete", reply["result"]["curve_id"], ["P1", "EC0"], True, len(writes)))
        self.assertEqual(end["points"], sum(len(u["points"]) for u in reply["result"]["record"]["units"].values()))
        self.assertIn("duration_s", end)
        self.assertEqual(max(i for i, r in enumerate(audit_records(self)) if r["event"] == "runtime_attempt") < audit_records(self).index(end), True)
        self.assertEqual(attempts(self), 0, "a cpufreq write is not a mailbox attempt: the per-boot budget is untouched")
        self.assertFalse(h.helper.tuned_this_boot)

    def test_a_cancelled_run_still_records_the_restore_and_its_result(self) -> None:
        calls = {"n": 0}
        gate, reached = threading.Event(), threading.Event()

        def hook(_s: float) -> None:
            calls["n"] += 1
            if calls["n"] == 12:
                reached.set()
                gate.wait(10)

        self.sleep_hook = hook
        h = self.start_measure()
        h.send({"id": "m", "op": "vf.measure", "args": {"mode": "quick", "units": ["P1", "EC0"]}})
        self.assertTrue(reached.wait(10))
        h.call("c", "cancel", {"op_id": "m"})
        gate.set()
        reply = h.response("m", 30)
        self.assertEqual(error_code(reply), "CANCELLED")
        self.assert_cpufreq_pristine()
        (end,) = results(self, "vf.measure")
        self.assertEqual((end["outcome"], end["restored"]), ("cancelled", True))
        self.assertEqual(len([r for r in audit_records(self) if r["event"] == "runtime_attempt"]), len(self.cpufreq.writes))

    def test_an_audit_log_that_cannot_be_written_refuses_before_any_change(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores file modes")
        lock_audit(self)
        h = self.start_measure()
        reply = h.call("m", "vf.measure", {"mode": "quick", "units": ["P1"]}, timeout=60)
        self.assertEqual(error_code(reply), "REFUSED")
        self.assertEqual(reply["error"]["detail"]["check"], "AUDIT_UNAVAILABLE")
        self.assertEqual(self.cpufreq.writes, [])
        self.assertFalse(self.journal.exists())
        self.assert_cpufreq_pristine()

    def test_curves_update_and_delete_leave_a_result(self) -> None:
        h = self.start_measure()
        curve = h.call("m", "vf.measure", {"mode": "quick", "units": ["P1"]}, timeout=60)["result"]["curve_id"]
        self.assertTrue(h.call("u", "curves.update", {"id": curve, "name": "renamed", "baseline": True})["ok"])
        (upd,) = results(self, "curves.update")
        self.assertEqual((upd["curve_id"], upd["name"], upd["is_baseline"]), (curve, "renamed", True))
        self.assertTrue(h.call("d", "curves.delete", {"id": curve})["ok"])
        (dele,) = results(self, "curves.delete")
        self.assertEqual(dele["curve_id"], curve)
        self.assertEqual(attempts(self), 0)


CAP = "scaling_max_freq"
MIN = "scaling_min_freq"


class StressAudit(StressCase):
    def test_a_capped_run_audits_the_caps_epp_and_restore_and_ends_with_one_result(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-ok")
        h = self.start_stress(self.swiring(stress_ng=tool))
        reply = self.run_stress(h, {"units": ["EC1"], "ratio_cap": 30})
        self.assertTrue(reply["ok"], reply)
        writes = [r for r in audit_records(self) if r["event"] == "runtime_attempt"]
        self.assertEqual(len(writes), len(self.cpufreq.writes))
        self.assertEqual({r["op"] for r in writes}, {"stress.start"})
        self.assertIn("energy_performance_preference", {r["key"] for r in writes})
        self.assertEqual({int(r["path"].split("/")[0][3:]) for r in writes}, {12, 13, 14, 15}, "the whole E-cluster is capped, so every CPU of it is audited")
        (end,) = results(self, "stress.start")
        self.assertEqual((end["outcome"], end["result_id"], end["screen"], end["ratio_cap"], end["units"], end["restored"]), ("time", reply["result"]["id"], "cpu", 30, ["EC1"], True))
        self.assertEqual(attempts(self), 0)
        self.assert_cpufreq_pristine()

    def test_an_uncapped_run_writes_no_cpufreq_record_but_still_a_result(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-ok")
        h = self.start_stress(self.swiring(stress_ng=tool))
        reply = self.run_stress(h)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual([r for r in audit_records(self) if r["event"] == "runtime_attempt"], [])
        (end,) = results(self, "stress.start")
        self.assertEqual((end["writes"], end["result_id"], end["ratio_cap"]), (0, reply["result"]["id"], None))

    def test_a_cap_write_that_fails_still_records_the_error_result(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-ok")
        h = self.start_stress(self.swiring(stress_ng=tool))
        self.cpufreq.fail = lambda cpu, attr, value: attr == CAP and value != str(self.model.max_khz(cpu)) and cpu == 12
        reply = self.run_stress(h, {"units": ["EC1"], "ratio_cap": 30})
        self.assertEqual(error_code(reply), "REFUSED")
        (end,) = results(self, "stress.start")
        self.assertEqual((end["outcome"], end["restored"]), ("error", True))

    def test_an_audit_log_that_cannot_be_written_refuses_before_any_change(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores file modes")
        lock_audit(self)
        h = self.start_stress(self.swiring(stress_ng=self.fake("stress-ng", "stress-ng-ok")))
        reply = self.run_stress(h, {**START, "units": ["EC1"], "ratio_cap": 30})
        self.assertEqual(error_code(reply), "REFUSED")
        self.assertEqual(reply["error"]["detail"]["check"], "AUDIT_UNAVAILABLE")
        self.assertEqual(self.cpufreq.writes, [])


class ProfilesSaveAudit(AcceptCase):
    def test_both_save_shapes_leave_one_result_and_no_attempt(self) -> None:
        h = self.boot_accepting()
        before = attempts(self)
        self.assertTrue(h.call("a", "profiles.save", {"name": "now", "from": "read"})["ok"])
        self.assertTrue(h.call("b", "profiles.save", {"name": "rows", "intent": rows(row("P3", points={"53": -60}))})["ok"])
        got = {r["profile"]: r for r in results(self, "profiles.save")}
        self.assertEqual(set(got), {"now", "rows"})
        self.assertEqual((got["now"]["source"], got["rows"]["source"]), ("read", "edits"))
        self.assertTrue(got["now"]["units"] > 0 and got["now"]["points"] > 0 and got["now"]["turbo"] is True)
        self.assertEqual(got["now"]["operator"], got["rows"]["operator"])
        self.assertEqual(attempts(self), before, "saving a profile is not a mailbox attempt")

    def test_a_refused_save_leaves_no_result(self) -> None:
        h = self.boot_accepting()
        self.assertTrue(h.call("a", "profiles.save", {"name": "now", "from": "read"})["ok"])
        self.assertFalse(h.call("b", "profiles.save", {"name": "now", "from": "read"})["ok"])  # EXISTS
        self.assertEqual(len(results(self, "profiles.save")), 1)

    def test_an_audit_log_that_cannot_be_written_saves_nothing(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores file modes")
        h = self.boot_accepting()
        self.audit.chmod(0o400)
        self.addCleanup(self.audit.chmod, 0o600)
        reply = h.call("a", "profiles.save", {"name": "now", "from": "read"})
        self.assertEqual(error_code(reply), "REFUSED")
        self.assertEqual(reply["error"]["detail"]["check"], "AUDIT_UNAVAILABLE")
        self.audit.chmod(0o600)
        self.assertEqual(h.call("l", "profiles.list", {})["result"]["profiles"], [])


if __name__ == "__main__":
    unittest.main()
