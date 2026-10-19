"""G-235: the audit trail of the ops that change cpufreq or a file under the state directory: ``vf.measure`` and ``stress.start`` (ONE ``runtime_attempt`` listing every file and its
original value before the first write, ONE ``result`` at the end, an ``error`` per failed write), the recovery of a stale journal (the same pair), ``profiles.save``, ``curves.update`` / ``curves.delete`` (a ``result``). None of it is a mailbox ``attempt``. Simulator, fake sysfs, temp dirs."""
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


def events(case: Any, *names: str) -> list[dict[str, Any]]:
    return [r for r in audit_records(case) if r["event"] in names]


def originals(case: Any, cpus: Any, attrs: Any) -> dict[str, str]:
    return {f"cpu{c}/cpufreq/{a}": case.cpufreq.read(c, a) for c in cpus for a in attrs}


def count_before_first_write(case: Any) -> dict[str, int]:
    """Install a hook on the recording cpufreq: the number of audit records on disk when the first write is sent (the hook runs before the write)."""
    seen: dict[str, int] = {}
    case.cpufreq.fail = lambda cpu, attr, value: seen.setdefault("n", len(audit_records(case))) is None
    return seen


class MeasureAudit(MeasureCase):
    def test_one_start_record_lists_every_file_and_one_result_ends_the_run(self) -> None:
        before = originals(self, range(24), (CAP, MIN))
        seen = count_before_first_write(self)
        h = self.start_measure()
        reply = h.call("m", "vf.measure", {"mode": "quick", "units": ["P1", "EC0"]}, timeout=60)
        self.assertTrue(reply["ok"], reply)
        self.assertGreater(len(self.cpufreq.writes), 4)
        recs = audit_records(self)
        self.assertEqual([r["event"] for r in recs], ["runtime_attempt", "result"], "no record per write: one start, one result")
        start, end = recs
        self.assertEqual(seen["n"], 1, "the start record is on disk (fsync'd) before the first cpufreq write")
        self.assertEqual((start["op"], start["kind"], start["domain"], start["boot_id"]), ("vf.measure", "cpufreq", "cpufreq", BOOT))
        listed = {f["path"]: f["value"] for f in start["files"]}
        self.assertTrue(listed and all(before[p] == v for p, v in listed.items()), "every file listed, with its original value")
        self.assertEqual((start["units"], start["mode"], start["file_count"]), (["P1", "EC0"], "quick", len(listed)))
        touched = {f"cpu{c}/cpufreq/{a}" for c, a, _ in self.cpufreq.writes}
        self.assertTrue(touched and touched <= set(listed), "nothing was written that the start record did not list")
        self.assertEqual((end["op"], end["outcome"], end["curve_id"], end["units"], end["restored"]), ("vf.measure", "complete", reply["result"]["curve_id"], ["P1", "EC0"], True))
        self.assertEqual((end["writes"], end["failures"], end["file_count"]), (len(self.cpufreq.writes), [], len(listed)))
        self.assertEqual(end["points"], sum(len(u["points"]) for u in reply["result"]["record"]["units"].values()))
        self.assertIn("duration_s", end)
        self.assertEqual(end["detail"], f"vf.measure: {len(listed)} files, restored ok")
        self.assertEqual(attempts(self), 0, "a cpufreq run is not a mailbox attempt: the per-boot budget is untouched")
        self.assertFalse(h.helper.tuned_this_boot)

    def test_a_cancelled_run_still_ends_with_its_result(self) -> None:
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
        self.assertEqual([r["event"] for r in audit_records(self)], ["runtime_attempt", "result"])
        (end,) = results(self, "vf.measure")
        self.assertEqual((end["outcome"], end["restored"], end["failures"]), ("cancelled", True, []))

    def test_a_write_that_fails_has_its_own_record_and_the_result_says_so(self) -> None:
        first: dict[str, int] = {}  # the first cpu that gets a cap below its maximum: its cap writes fail
        self.cpufreq.fail = lambda cpu, attr, value: attr == CAP and value != str(self.model.max_khz(cpu)) and first.setdefault("cpu", cpu) == cpu
        h = self.start_measure()
        h.call("m", "vf.measure", {"mode": "quick", "units": ["EC0"]}, timeout=60)
        failed = events(self, "error")
        self.assertTrue(failed)
        path = f"cpu{first['cpu']}/cpufreq/scaling_max_freq"
        self.assertEqual({(r["path"], r["op"], r["kind"]) for r in failed}, {(path, "vf.measure", "cpufreq")})
        self.assertIn("injected write failure", failed[0]["reason"])
        self.assertTrue(failed[0]["written"].isdigit())
        (end,) = results(self, "vf.measure")
        self.assertEqual(len(end["failures"]), len(failed))
        self.assertEqual(end["failures"][0]["path"], path)
        self.assertTrue(end["restored"], "the failed writes never changed the file, so the restore found nothing to put back")
        self.assertEqual(len(events(self, "runtime_attempt")), 1)

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

    def test_curves_update_and_delete_are_refused_before_the_change_when_the_log_cannot_be_written(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores file modes")
        h = self.start_measure()
        curve = h.call("m", "vf.measure", {"mode": "quick", "units": ["P1"]}, timeout=60)["result"]["curve_id"]
        name = h.call("g", "curves.get", {"id": curve})["result"]["name"]
        for mode in (0o400, 0o666):  # not writable / not trusted (group- and world-writable)
            self.audit.chmod(mode)
            self.addCleanup(self.audit.chmod, 0o600)
            for op, args in (("curves.update", {"id": curve, "name": "renamed", "baseline": True}), ("curves.delete", {"id": curve})):
                reply = h.call("x", op, args)
                self.assertEqual((error_code(reply), reply["error"]["detail"]["check"]), ("REFUSED", "AUDIT_UNAVAILABLE"), (mode, op, reply))
                self.assertIn("nothing was changed", reply["error"]["message"])
            row = h.call("g2", "curves.get", {"id": curve})["result"]
            self.assertEqual((row["name"], row.get("baseline")), (name, False), "the curve is untouched")
        self.audit.chmod(0o600)
        self.assertTrue(h.call("u", "curves.update", {"id": curve, "name": "renamed"})["ok"], "writable again: it goes through")


CAP = "scaling_max_freq"
MIN = "scaling_min_freq"


class RecoveryAudit(MeasureCase):
    """The restore of a stale journal (a run that died): one start record with the files and their values, one result."""

    def stale(self) -> None:
        self.cpufreq.write(3, CAP, "3000000")
        self.cpufreq.write(3, MIN, "1200000")
        self.cpufreq.writes.clear()
        entries = [{"cpu": 3, "attr": CAP, "value": str(self.model.max_khz(3)), "at_max": True}, {"cpu": 3, "attr": MIN, "value": "800000"}]
        self.journal.write_text(json.dumps({"version": 1, "pid": 1, "boot_id": None, "created": 0, "context": {}, "entries": entries}))
        os.chmod(self.journal, 0o600)

    def test_helper_start_records_the_restore_and_its_result(self) -> None:
        self.stale()
        seen = count_before_first_write(self)
        self.start_measure().call("p", "preflight")
        self.assert_cpufreq_pristine()
        self.assertEqual(seen["n"], 1)
        start, end = audit_records(self)
        self.assertEqual((start["event"], start["op"], start["during"]), ("runtime_attempt", "journal.recover", None))
        self.assertEqual({f["path"]: f["value"] for f in start["files"]}, {"cpu3/cpufreq/scaling_max_freq": str(self.model.max_khz(3)), "cpu3/cpufreq/scaling_min_freq": "800000"})
        self.assertEqual((end["event"], end["op"], end["restored"], end["outcome"], end["writes"]), ("result", "journal.recover", True, "restored", 2))
        self.assertEqual(end["detail"], "journal.recover: 2 files, restored ok")

    def test_a_restore_that_cannot_finish_is_an_error_record_and_a_kept_journal(self) -> None:
        self.stale()
        self.cpufreq.fail = lambda cpu, attr, value: cpu == 3 and attr == CAP
        self.start_measure().call("p", "preflight")
        self.assertTrue(self.journal.exists())
        (err,) = events(self, "error")
        self.assertEqual((err["path"], err["op"]), ("cpu3/cpufreq/scaling_max_freq", "journal.recover"))
        (end,) = results(self, "journal.recover")
        self.assertEqual((end["restored"], end["outcome"], len(end["failures"])), (False, "kept", 1))
        self.assertIn("NOT restored", end["detail"])

    def test_a_restore_that_fails_while_reading_has_an_error_record_and_says_not_restored(self) -> None:
        self.stale()
        real = self.cpufreq.read

        def read(cpu: int, attr: str) -> str | None:
            if (cpu, attr) == (3, CAP):
                raise OSError(5, "injected read failure")
            return real(cpu, attr)

        self.cpufreq.read = read  # type: ignore[method-assign]
        self.start_measure().call("p", "preflight")
        self.assertTrue(self.journal.exists())
        (err,) = events(self, "error")
        self.assertEqual((err["path"], err["op"]), ("cpu3/cpufreq/scaling_max_freq", "journal.recover"))
        self.assertIn("injected read failure", err["reason"])
        (end,) = results(self, "journal.recover")
        self.assertEqual((end["restored"], end["outcome"], [f["path"] for f in end["failures"]]), (False, "kept", ["cpu3/cpufreq/scaling_max_freq"]))

    def test_no_journal_means_no_record(self) -> None:
        self.start_measure().call("p", "preflight")
        self.assertEqual(audit_records(self), [])

    def test_a_measure_that_finds_a_stale_journal_records_the_restore_then_its_own_run(self) -> None:
        self.stale()
        self.cpufreq.fail = lambda cpu, attr, value: cpu == 3 and attr == CAP
        h = self.start_measure()
        h.call("p", "preflight")
        self.cpufreq.fail = None
        self.assertTrue(h.call("m", "vf.measure", {"mode": "quick", "units": ["P1"]}, timeout=60)["ok"])
        got = [(r["event"], r["op"]) for r in audit_records(self)]
        self.assertEqual(got, [("runtime_attempt", "journal.recover"), ("error", "journal.recover"), ("result", "journal.recover"),
                               ("runtime_attempt", "journal.recover"), ("result", "journal.recover"), ("runtime_attempt", "vf.measure"), ("result", "vf.measure")])
        during = [r["during"] for r in audit_records(self) if r.get("op") == "journal.recover" and r["event"] == "result"]
        self.assertEqual(during, [None, "vf.measure"])


class StressAudit(StressCase):
    def test_a_capped_run_has_one_start_record_with_the_files_and_the_caps_and_one_result(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-ok")
        cpus = [12, 13, 14, 15]
        before = originals(self, cpus, (CAP, "energy_performance_preference"))
        seen = count_before_first_write(self)
        h = self.start_stress(self.swiring(stress_ng=tool))
        reply = self.run_stress(h, {"units": ["EC1"], "ratio_cap": 30})
        self.assertTrue(reply["ok"], reply)
        self.assertGreater(len(self.cpufreq.writes), 4)
        start, end = audit_records(self)
        self.assertEqual(seen["n"], 1, "the start record is on disk before the first cap write")
        self.assertEqual((start["event"], start["op"], start["kind"], start["ratio_cap"], start["units"]), ("runtime_attempt", "stress.start", "cpufreq", 30, ["EC1"]))
        listed = {f["path"]: f["value"] for f in start["files"]}
        self.assertEqual(set(listed) - set(before), set(), "only files of the whole E-cluster")
        self.assertTrue({f"cpu{c}/cpufreq/{CAP}" for c in cpus} <= set(listed))
        self.assertTrue(all(listed[p] == v for p, v in before.items() if p in listed), "the original values")
        self.assertEqual(set(start["cap_khz"]), {str(c) for c in cpus})
        self.assertEqual(start["epp"], "performance")
        self.assertEqual((end["event"], end["op"], end["outcome"], end["result_id"], end["screen"], end["restored"], end["failures"]),
                         ("result", "stress.start", "time", reply["result"]["id"], "cpu", True, []))
        self.assertEqual((end["writes"], end["file_count"]), (len(self.cpufreq.writes), len(listed)))
        self.assertEqual(end["detail"], f"stress.start: {len(listed)} files, restored ok")
        self.assertEqual(attempts(self), 0)
        self.assert_cpufreq_pristine()

    def test_an_uncapped_run_writes_no_cpufreq_start_record_but_still_a_result(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-ok")
        h = self.start_stress(self.swiring(stress_ng=tool))
        reply = self.run_stress(h)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(events(self, "runtime_attempt"), [])
        (end,) = results(self, "stress.start")
        self.assertEqual((end["writes"], end["result_id"], end["ratio_cap"]), (0, reply["result"]["id"], None))
        self.assertEqual(end["detail"], f"stress.start: no caps written; result stored as {reply['result']['id']}")

    def test_a_cap_write_that_fails_has_its_own_record_and_the_error_result(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-ok")
        h = self.start_stress(self.swiring(stress_ng=tool))
        self.cpufreq.fail = lambda cpu, attr, value: attr == CAP and value != str(self.model.max_khz(cpu)) and cpu == 12
        reply = self.run_stress(h, {"units": ["EC1"], "ratio_cap": 30})
        self.assertEqual(error_code(reply), "REFUSED")
        self.assertEqual([r["event"] for r in audit_records(self)], ["runtime_attempt", "error", "result"])
        start, err, end = audit_records(self)
        self.assertEqual((err["path"], err["op"], err["written"]), ("cpu12/cpufreq/scaling_max_freq", "stress.start", str(start["cap_khz"]["12"])))
        self.assertIn("injected write failure", err["reason"])
        self.assertEqual((end["outcome"], end["restored"], end["failures"][0]["path"]), ("error", True, "cpu12/cpufreq/scaling_max_freq"))

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
