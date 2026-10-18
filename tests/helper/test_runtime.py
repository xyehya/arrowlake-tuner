"""G-216: ``runtime.read`` / ``runtime.plan`` / ``runtime.apply`` through the real helper loop on the simulator. The kernel is ``SimRuntimeFiles`` (per-CPU cpufreq over the shared simulated
cpufreq, ``online`` without cpu0, the uncore limits) or a fake sysfs tree; the journal and the audit log are in a temp directory. No hardware, no /sys, no /run."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import types
from pathlib import Path
from typing import Any
from unittest import mock

from arltuner.controls import caps as cp
from arltuner.guard import read_state
from arltuner.helper import ops_runtime
from arltuner.helper.measure_sim import SimCpufreq
from arltuner.helper.ops_runtime import RuntimeWiring, SimRuntimeFiles
from arltuner.helper.state import State

from tests.fakesys import make_runtime_sysfs

from ._support import HelperCase

BOOT = "11111111-2222-3333-4444-555555555555"
BASE = 'cpus = ["0-23"]\nmax_operations_per_session = 300\nmax_operations_per_boot = 400\nmin_dwell_s = 0.0\n'
RUNTIME = "\n[runtime]\ncap_ratio_min = 8\nuncore_ratio = [10, 38]\nparking = true\n"
P_MAX = 5_700_000  # SimModel.max_khz(cpu 0)


def e_max() -> int:
    return int(SimCpufreq().read(8, "cpuinfo_max_freq"))


class RuntimeCase(HelperCase):
    n = 0

    def boot(self, policy: str = BASE + RUNTIME, files: SimRuntimeFiles | None = None, **cfg: Any) -> Any:
        self.policy.write_text(policy, encoding="utf-8", newline="\n")
        self.cpufreq = SimCpufreq()
        self.files = files or SimRuntimeFiles(self.cpufreq, range(24))
        self.journal = self.tmp / "runtime-journal.json"
        self.measure_journal = self.tmp / "measure-journal.json"
        h = self.start(self.config(**cfg) if cfg else None)
        h.helper.runtime_wiring = RuntimeWiring(self.files, self.journal)
        h.helper.measure_wiring = types.SimpleNamespace(journal_path=self.measure_journal, cpufreq=self.cpufreq)
        return h

    def call(self, h: Any, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        self.n += 1
        return h.call(f"r{self.n}", op, args)

    def ok(self, h: Any, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        msg = self.call(h, op, args)
        self.assertTrue(msg.get("ok"), msg)
        return msg["result"]

    def refused(self, h: Any, op: str, args: dict[str, Any], check: str | None = None) -> dict[str, Any]:
        msg = self.call(h, op, args)
        self.assertFalse(msg.get("ok"), msg)
        self.assertEqual(msg["error"]["code"], "REFUSED", msg)
        if check is not None:
            self.assertEqual(msg["error"]["detail"]["check"], check, msg)
        return msg["error"]

    def plan(self, h: Any, **intent: Any) -> dict[str, Any]:
        return self.ok(h, "runtime.plan", intent)

    def apply(self, h: Any, plan: dict[str, Any]) -> dict[str, Any]:
        return self.ok(h, "runtime.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM " + plan["nonce"]})

    def go(self, h: Any, **intent: Any) -> dict[str, Any]:
        res = self.apply(h, self.plan(h, **intent))
        self.assertTrue(res["complete"], res)
        return res

    def audit_records(self) -> list[dict[str, Any]]:
        if not self.audit.exists():
            return []
        return [json.loads(line) for line in self.audit.read_text().splitlines() if line.strip()]

    def runtime_records(self) -> list[dict[str, Any]]:
        return [r for r in self.audit_records() if r.get("kind") == "runtime"]

    def cap(self, cpu: int) -> int:
        return int(self.cpufreq.read(cpu, "scaling_max_freq"))

    def journal_doc(self) -> dict[str, Any]:
        return json.loads(self.journal.read_text())


class Read(RuntimeCase):
    def test_reads_caps_parking_uncore_policy_and_the_journal(self) -> None:
        h = self.boot()
        r = self.ok(h, "runtime.read")
        self.assertEqual(len(r["caps"]), 24)
        cpu0, cpu8 = r["caps"][0], r["caps"][8]
        self.assertEqual((cpu0["kind"], cpu0["online"], cpu0["can_park"], cpu0["cpufreq"]["cap_khz"]), ("P", True, False, P_MAX))
        self.assertEqual((cpu8["kind"], cpu8["unit"], cpu8["can_park"]), ("E", "EC0", True))
        self.assertIsNone(r["parking"]["0"])
        self.assertTrue(r["parking"]["5"])
        self.assertEqual((r["uncore"]["min_khz"], r["uncore"]["max_khz"], r["uncore"]["initial_max_khz"]), (800_000, 4_000_000, 4_000_000))
        self.assertEqual(r["policy"], {"source": str(self.policy), "cap_ratio_min": 8, "uncore_ratio": [10, 38], "parking": True})
        self.assertEqual((r["held"], r["journal"]), (None, None))

    def test_an_absent_runtime_table_is_all_null(self) -> None:
        h = self.boot(BASE)
        p = self.ok(h, "runtime.read")["policy"]
        self.assertEqual((p["cap_ratio_min"], p["uncore_ratio"], p["parking"]), (None, None, None))

    def test_the_read_names_what_holds_the_caps(self) -> None:
        h = self.boot()
        self.measure_journal.write_text("{}")
        self.assertIn("measure-journal.json", self.ok(h, "runtime.read")["held"])

    def test_the_read_is_allowed_while_a_stress_run_holds_the_helper(self) -> None:
        h = self.boot()
        h.helper.machine.begin("s1", "stress.start", State.STRESSING)
        self.addCleanup(h.helper.machine.end, "s1")
        self.assertIn("stress.start", self.ok(h, "runtime.read")["held"])
        msg = self.call(h, "runtime.plan", {"caps": {"3": 45}})
        self.assertEqual(msg["error"]["code"], "BUSY", msg)  # a long op: the helper is stressing

    def test_no_uncore_driver_is_a_note_not_a_failure(self) -> None:
        h = self.boot()
        self.files.uncore.clear()
        r = self.ok(h, "runtime.read")
        self.assertIsNone(r["uncore"])
        self.assertIsNotNone(r["caps"])

    def test_a_fake_sysfs_tree_reads_like_the_machine(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        make_runtime_sysfs(root, offline=[9])
        h = self.boot(files=cp.RuntimeFiles(root))  # type: ignore[arg-type]
        r = self.ok(h, "runtime.read")
        self.assertEqual((r["caps"][0]["can_park"], r["caps"][9]["online"], r["caps"][9]["cpufreq"]), (False, False, None))
        self.assertEqual(r["uncore"]["domain"], "package_00_die_00")


class Caps(RuntimeCase):
    def test_a_cap_is_planned_written_read_back_audited_and_journaled(self) -> None:
        h = self.boot()
        plan = self.plan(h, caps={"3": 45})
        self.assertEqual([(s["domain"], s["key"], s["file"], s["before"], s["value"]) for s in plan["steps"]], [("caps", "cpu3", "cpu3/cpufreq/scaling_max_freq", P_MAX, 4_500_000)])
        self.assertEqual((plan["ops_needed"], plan["journal_new"]), (0, True))
        self.assertEqual(self.files.writes, [])  # planning wrote nothing
        res = self.apply(h, plan)
        self.assertTrue(res["complete"])
        self.assertEqual(self.files.writes, [("cpu3/cpufreq/scaling_max_freq", 4_500_000)])
        self.assertEqual(res["readback"], [{"domain": "caps", "key": "cpu3", "expected": [4_500_000, 4_500_000], "file": 4_500_000, "match": True}])
        self.assertEqual(res["after"]["caps"], {"3": 4_500_000})
        self.assertEqual(self.cap(3), 4_500_000)
        doc = self.journal_doc()
        self.assertEqual((doc["boot_id"], doc["caps"], doc["online"], doc["uncore"], doc["in_flight"]), (BOOT, {"3": P_MAX}, {}, {}, False))
        self.assertEqual(oct(self.journal.stat().st_mode & 0o777), "0o600")
        recs = self.runtime_records()
        self.assertEqual([r["event"] for r in recs], ["runtime_attempt", "readback"])
        self.assertEqual((recs[0]["written"], recs[0]["before"], recs[0]["key"], recs[0]["domain"], recs[0]["plan_id"]), (4_500_000, P_MAX, "cpu3", "caps", plan["plan_id"]))
        self.assertFalse([r for r in self.audit_records() if r["event"] == "attempt"])  # no mailbox budget used
        self.assertEqual(self.ok(h, "runtime.read")["journal"]["caps"], {"3": P_MAX})

    def test_an_e_core_cap_writes_the_four_cpus_of_its_cluster_one_file_each(self) -> None:
        h = self.boot()
        plan = self.plan(h, caps={"10": 40})
        self.assertEqual([s["key"] for s in plan["steps"]], ["cpu8", "cpu9", "cpu10", "cpu11"])
        self.assertTrue(any("also set on cpu 8, 9, 11" in w for w in plan["warnings"]), plan["warnings"])
        res = self.apply(h, plan)
        self.assertEqual(res["writes_sent"], 4)
        self.assertEqual([self.cap(c) for c in (8, 9, 10, 11, 12)], [4_000_000] * 4 + [e_max()])
        self.assertEqual(sorted(self.journal_doc()["caps"]), ["10", "11", "8", "9"])

    def test_a_later_apply_keeps_the_first_journaled_value(self) -> None:
        h = self.boot()
        cap4 = self.cap(4)
        self.go(h, caps={"3": 45})
        plan = self.plan(h, caps={"3": 40, "4": 41})
        self.assertTrue(plan["journal_new"])  # cpu4 is new
        self.apply(h, plan)
        self.assertEqual(self.journal_doc()["caps"], {"3": P_MAX, "4": cap4})

    def test_no_policy_key_no_write_and_the_refusal_names_the_key(self) -> None:
        h = self.boot(BASE)
        e = self.refused(h, "runtime.plan", {"caps": {"3": 45}}, "RUNTIME_POLICY_MISSING")
        self.assertEqual(e["detail"]["key"], "cap_ratio_min")
        self.assertIn("[runtime] cap_ratio_min", e["message"])
        self.assertEqual(self.files.writes, [])
        self.assertFalse(self.journal.exists())

    def test_range_and_cluster_rules(self) -> None:
        h = self.boot()
        self.refused(h, "runtime.plan", {"caps": {"3": 7}}, "CAPS_RANGE")
        self.refused(h, "runtime.plan", {"caps": {"3": 99}}, "CAPS_RANGE")
        self.refused(h, "runtime.plan", {"caps": {"8": 40, "9": 41}}, "CAPS_CLUSTER_CONFLICT")
        self.refused(h, "runtime.plan", {"caps": {"99": 40}}, "CAPS_UNKNOWN_CPU")

    def test_a_cap_journal_of_a_measure_or_stress_run_is_a_named_conflict(self) -> None:
        h = self.boot()
        self.measure_journal.write_text("{}")
        e = self.refused(h, "runtime.plan", {"caps": {"3": 45}}, "CAPS_CONFLICT")
        self.assertIn("measure-journal.json", e["message"])
        self.assertIn("vf.measure or stress.start", e["message"])
        self.measure_journal.unlink()
        self.plan(h, caps={"3": 45})

    def test_a_conflict_that_appears_after_the_plan_makes_the_apply_refuse(self) -> None:
        h = self.boot()
        plan = self.plan(h, caps={"3": 45})
        self.measure_journal.write_text("{}")
        self.refused(h, "runtime.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM " + plan["nonce"]}, "CAPS_CONFLICT")
        self.assertEqual(self.files.writes, [])

    def test_the_cap_that_the_kernel_ignores_trips_on_the_read_back(self) -> None:
        sim = SimRuntimeFiles(SimCpufreq(), range(24), drop_writes=True)
        h = self.boot(files=sim)
        res = self.apply(h, self.plan(h, caps={"8": 40}))
        self.assertFalse(res["complete"])
        self.assertEqual((res["stop"]["kind"], res["stop"]["tripped"], res["writes_sent"]), ("unverified", True, 1))  # the other three were never sent
        self.assertEqual([s["status"] for s in res["steps"]], ["unverified", "not_sent", "not_sent", "not_sent"])
        self.assertIn("reads", res["readback"][0]["problem"])
        self.assertEqual([r["event"] for r in self.runtime_records()], ["runtime_attempt", "readback", "trip"])
        self.assertFalse(self.journal_doc()["in_flight"])  # the apply ended


class Parking(RuntimeCase):
    def test_park_then_unpark(self) -> None:
        h = self.boot()
        plan = self.plan(h, parking={"12": True})
        self.assertEqual([(s["key"], s["file"], s["before"], s["value"]) for s in plan["steps"]], [("cpu12", "cpu12/online", 1, 0)])
        self.assertTrue(any("leaves the live CPU topology" in w for w in plan["warnings"]))
        res = self.apply(h, plan)
        self.assertEqual(res["after"]["online"], {"12": False})
        self.assertEqual(self.files.online[12], 0)
        self.assertEqual(self.journal_doc()["online"], {"12": 1})
        self.assertFalse(self.ok(h, "runtime.read")["caps"][12]["online"])
        self.assertIsNone(self.ok(h, "runtime.read")["caps"][12]["cpufreq"])  # an offline CPU has no cpufreq directory
        self.go(h, parking={"12": False})
        self.assertEqual(self.files.online[12], 1)

    def test_cpu0_and_the_last_online_cpu_of_a_kind_are_refused(self) -> None:
        h = self.boot()
        e = self.refused(h, "runtime.plan", {"parking": {"0": True}}, "PARK_CPU0")
        self.assertEqual(e["detail"]["key"], "cpu0")
        self.refused(h, "runtime.plan", {"parking": dict.fromkeys(map(str, range(8, 24)), True)}, "PARK_LAST_OF_KIND")
        self.go(h, parking=dict.fromkeys(map(str, range(8, 23)), True))
        self.refused(h, "runtime.plan", {"parking": {"23": True}}, "PARK_LAST_OF_KIND")
        self.assertEqual([self.files.online[c] for c in (8, 22, 23)], [0, 0, 1])

    def test_parking_needs_the_policy_key_unparking_does_not(self) -> None:
        h = self.boot(BASE + RUNTIME)
        self.go(h, parking={"12": True})
        self.policy.write_text(BASE, encoding="utf-8", newline="\n")  # the operator removed [runtime]
        e = self.refused(h, "runtime.plan", {"parking": {"13": True}}, "RUNTIME_POLICY_MISSING")
        self.assertEqual(e["detail"]["key"], "parking")
        self.go(h, parking={"12": False})

    def test_no_parking_while_a_cap_journal_is_held_but_unparking_is_allowed(self) -> None:
        h = self.boot()
        self.go(h, parking={"12": True})
        self.measure_journal.write_text("{}")
        self.refused(h, "runtime.plan", {"parking": {"13": True}}, "PARK_CONFLICT")
        self.go(h, parking={"12": False})

    def test_no_parking_while_stress_runs(self) -> None:
        h = self.boot()
        h.helper.machine.begin("s1", "stress.start", State.STRESSING)
        try:
            self.assertIn("stress.start", ops_runtime._held(h.helper))
        finally:
            h.helper.machine.end("s1")
        self.assertIsNone(ops_runtime._held(h.helper))

    def test_a_cap_on_a_cpu_parked_in_the_same_intent_is_refused(self) -> None:
        h = self.boot()
        self.refused(h, "runtime.plan", {"parking": {"5": True}, "caps": {"5": 40}}, "CAPS_PARKED")

    def test_a_cpu_the_kernel_will_not_offline_trips(self) -> None:
        h = self.boot(files=SimRuntimeFiles(SimCpufreq(), range(24), drop_writes=True))
        res = self.apply(h, self.plan(h, parking={"12": True, "13": True}))
        self.assertEqual((res["complete"], res["stop"]["kind"], res["writes_sent"]), (False, "unverified", 1))


class Uncore(RuntimeCase):
    def test_a_limit_is_written_and_read_back_in_khz(self) -> None:
        h = self.boot()
        res = self.go(h, uncore={"max_ratio": 35})
        self.assertEqual(self.files.uncore["max_freq_khz"], 3_500_000)
        self.assertEqual(res["after"]["uncore"]["max_ratio"], 35.0)
        self.assertEqual(self.journal_doc()["uncore"], {"max_ratio": 4_000_000})

    def test_min_and_max_are_ordered_so_the_driver_never_sees_min_above_max(self) -> None:
        h = self.boot()
        self.go(h, uncore={"min_ratio": 20, "max_ratio": 25})
        self.assertEqual([w[0].rsplit("/", 1)[1] for w in self.files.writes], ["min_freq_khz", "max_freq_khz"])
        self.files.writes.clear()
        self.go(h, uncore={"min_ratio": 36, "max_ratio": 38})  # the new min is above the max in force
        self.assertEqual([w[0].rsplit("/", 1)[1] for w in self.files.writes], ["max_freq_khz", "min_freq_khz"])
        self.assertEqual((self.files.uncore["min_freq_khz"], self.files.uncore["max_freq_khz"]), (3_600_000, 3_800_000))

    def test_bounds_policy_and_order_refusals(self) -> None:
        h = self.boot()
        self.refused(h, "runtime.plan", {"uncore": {"max_ratio": 39}}, "UNCORE_RANGE")
        self.refused(h, "runtime.plan", {"uncore": {"min_ratio": 9}}, "UNCORE_RANGE")
        self.refused(h, "runtime.plan", {"uncore": {"min_ratio": 30, "max_ratio": 20}}, "UNCORE_ORDER")
        self.policy.write_text(BASE, encoding="utf-8", newline="\n")  # the operator removed [runtime]
        e = self.refused(h, "runtime.plan", {"uncore": {"max_ratio": 35}}, "RUNTIME_POLICY_MISSING")
        self.assertEqual(e["detail"]["key"], "uncore_ratio")
        self.assertEqual(self.files.writes, [])

    def test_a_driver_that_refuses_is_a_transport_stop(self) -> None:
        h = self.boot()
        plan = self.plan(h, uncore={"max_ratio": 35})
        self.files.uncore["initial_max_freq_khz"] = 3_000_000  # the hardware range moved under the plan: the plan is stale, not written
        msg = self.call(h, "runtime.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM " + plan["nonce"]})
        self.assertFalse(msg["ok"], msg)
        self.assertEqual(self.files.writes, [])


class Safeguards(RuntimeCase):
    def test_the_audit_pre_record_the_journal_and_the_lock_exist_when_the_write_happens(self) -> None:
        case = self
        seen: list[tuple[str, int, bool, bool]] = []

        class Witnessed(SimRuntimeFiles):
            def write_int(self, name: str, value: int) -> None:
                last = case.runtime_records()[-1]
                seen.append((last["event"], last["written"], os.path.exists(case.lock), json.loads(case.journal.read_text())["in_flight"]))
                super().write_int(name, value)

        h = self.boot(files=Witnessed(SimCpufreq(), range(24)))
        self.go(h, caps={"3": 45, "4": 44})
        self.assertEqual(seen, [("runtime_attempt", 4_500_000, True, True), ("runtime_attempt", 4_400_000, True, True)])
        self.assertFalse(self.lock.exists())  # released after each transaction
        self.assertEqual([r["event"] for r in self.runtime_records()], ["runtime_attempt", "readback"] * 2)
        self.assertFalse(self.journal_doc()["in_flight"])

    def test_runtime_attempt_is_not_counted_by_the_guard(self) -> None:
        h = self.boot()
        self.go(h, caps={"3": 45})
        self.assertTrue(self.runtime_records())
        state = read_state(self.audit)
        self.assertEqual((state.last_attempt_wall, dict(state.attempts_by_boot)), (None, {}))  # no dwell, no per-boot budget used

    def test_a_held_lock_refuses_before_anything_is_sent(self) -> None:
        h = self.boot()
        plan = self.plan(h, caps={"3": 45})

        def hold(*args: Any, **kw: Any) -> None:  # another process takes the lock after the apply re-planned
            self.lock.write_text("4242\n")

        with mock.patch.object(ops_runtime, "_write_journal", side_effect=hold):
            msg = self.call(h, "runtime.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM " + plan["nonce"]})
        self.assertEqual((msg["error"]["code"], msg["error"]["detail"]["check"]), ("REFUSED", "LOCKED"), msg)
        self.assertEqual(self.files.writes, [])
        self.assertEqual(self.runtime_records(), [])
        self.assertEqual(self.lock.read_text(), "4242\n")  # someone else's lock is never removed

    def test_an_audit_log_that_cannot_be_written_stops_before_the_first_write(self) -> None:
        h = self.boot()
        plan = self.plan(h, caps={"3": 45})
        if self.audit.exists():
            self.audit.unlink()
        self.audit.mkdir()  # not a file: AuditLog cannot open it
        msg = self.call(h, "runtime.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM " + plan["nonce"]})
        self.assertFalse(msg["ok"], msg)
        self.assertEqual(self.files.writes, [])
        self.assertFalse(self.journal.exists())

    def test_the_nonce_gates_the_apply_and_a_plan_is_single_use(self) -> None:
        h = self.boot()
        plan = self.plan(h, caps={"3": 45})
        msg = self.call(h, "runtime.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM AAAAAA"})
        self.assertEqual(msg["error"]["code"], "CONFIRM_MISMATCH", msg)
        self.assertEqual(self.files.writes, [])
        self.apply(h, plan)
        msg = self.call(h, "runtime.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM " + plan["nonce"]})
        self.assertEqual(msg["error"]["code"], "CONFIRM_MISMATCH", msg)  # replay
        self.assertEqual(len(self.files.writes), 1)

    def test_a_plan_of_the_power_store_is_not_accepted_here(self) -> None:
        h = self.boot()
        msg = self.call(h, "runtime.apply", {"plan_id": "0" * 64, "typed": "CONFIRM AAAAAA"})
        self.assertEqual(msg["error"]["code"], "PLAN_STALE", msg)

    def test_values_that_moved_since_the_plan_make_it_stale(self) -> None:
        h = self.boot()
        plan = self.plan(h, caps={"3": 45})
        self.cpufreq.write(3, "scaling_max_freq", "5000000")
        msg = self.call(h, "runtime.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM " + plan["nonce"]})
        self.assertEqual(msg["error"]["code"], "PLAN_STALE", msg)
        self.assertEqual(self.files.writes, [])

    def test_nothing_to_change_is_a_refusal_and_bad_shapes_are_bad_requests(self) -> None:
        h = self.boot()
        self.refused(h, "runtime.plan", {"caps": {"3": 57}}, "NOTHING_TO_DO")  # already at the maximum
        for args in ({}, {"caps": {}}, {"caps": {"x": 40}}, {"caps": {"3": 40.5}}, {"caps": {"3": True}}, {"parking": {"3": 1}}, {"uncore": {"mid": 20}}, {"uncore": []}, {"revert": 1}, {"revert": True, "caps": {"3": 40}}, {"bogus": 1}):
            msg = self.call(h, "runtime.plan", args)
            self.assertEqual(msg["error"]["code"], "BAD_REQUEST", (args, msg))
        self.assertEqual(self.files.writes, [])

    def test_a_cancel_before_the_first_write_changes_nothing(self) -> None:
        h = self.boot()
        plan = self.plan(h, caps={"3": 45})
        real = ops_runtime._write_journal

        def cancelling(helper: Any, *a: Any, **kw: Any) -> None:
            real(helper, *a, **kw)
            h.helper._cancel.set()  # type: ignore[union-attr]

        with mock.patch.object(ops_runtime, "_write_journal", side_effect=cancelling):
            msg = self.call(h, "runtime.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM " + plan["nonce"]})
        self.assertEqual(msg["error"]["code"], "CANCELLED", msg)
        self.assertEqual(self.files.writes, [])
        self.assertFalse(self.journal_doc()["in_flight"])


class Revert(RuntimeCase):
    def test_revert_puts_everything_back_unparks_first_and_keeps_the_journal(self) -> None:
        h = self.boot()
        self.go(h, caps={"3": 45, "10": 40}, parking={"12": True}, uncore={"min_ratio": 20, "max_ratio": 30})
        self.assertEqual((self.cap(3), self.cap(10), self.files.online[12], self.files.uncore["max_freq_khz"]), (4_500_000, 4_000_000, 0, 3_000_000))
        self.files.writes.clear()
        plan = self.plan(h, revert=True)
        keys = [(s["domain"], s["key"]) for s in plan["steps"]]
        self.assertEqual(keys[0], ("parking", "cpu12"))  # the unpark comes first
        self.assertEqual({d for d, _ in keys}, {"parking", "caps", "uncore"})
        res = self.apply(h, plan)
        self.assertTrue(res["complete"], res)
        self.assertEqual((self.cap(3), self.cap(10), self.cap(8), self.files.online[12]), (P_MAX, e_max(), e_max(), 1))
        self.assertEqual((self.files.uncore["min_freq_khz"], self.files.uncore["max_freq_khz"]), (800_000, 4_000_000))
        self.assertTrue(self.journal.exists())  # kept: a second revert (nothing to do) and a later apply use the same baseline
        self.refused(h, "runtime.plan", {"revert": True}, "NOTHING_TO_DO")
        self.assertTrue(all(r["revert"] for r in self.runtime_records() if r["event"] == "runtime_attempt" and r["plan_id"] == plan["plan_id"]))

    def test_revert_without_a_journal_is_refused(self) -> None:
        h = self.boot()
        self.refused(h, "runtime.plan", {"revert": True}, "NO_JOURNAL")

    def test_revert_needs_no_policy_keys(self) -> None:
        h = self.boot()
        self.go(h, caps={"3": 45}, parking={"12": True})
        self.policy.write_text(BASE, encoding="utf-8", newline="\n")
        self.assertTrue(self.apply(h, self.plan(h, revert=True))["complete"])
        self.assertEqual((self.cap(3), self.files.online[12]), (P_MAX, 1))

    def test_a_revert_warns_when_a_journaled_value_is_outside_the_policy_now(self) -> None:
        h = self.boot()
        self.go(h, caps={"3": 45}, uncore={"max_ratio": 30})
        self.policy.write_text(BASE + "\n[runtime]\ncap_ratio_min = 58\nuncore_ratio = [10, 20]\n", encoding="utf-8", newline="\n")
        warnings = self.plan(h, revert=True)["warnings"]
        self.assertTrue(any("cap_ratio_min" in w for w in warnings), warnings)
        self.assertTrue(any("max_ratio" in w and "uncore_ratio" in w for w in warnings), warnings)

    def test_a_cpu_that_is_offline_at_revert_keeps_its_cap_and_says_so(self) -> None:
        h = self.boot()
        self.go(h, caps={"3": 45})
        self.files.online[3] = 0  # parked behind our back
        e = self.refused(h, "runtime.plan", {"revert": True}, "NOTHING_TO_DO")  # no step is left (the CPU has no cpufreq directory)
        self.assertIn("cpu 3 has no cpufreq directory (offline): its cap was not restored", e["message"])

    def test_a_revert_waits_for_a_run_that_holds_the_caps_but_unparking_does_not(self) -> None:
        h = self.boot()
        self.go(h, caps={"3": 45}, parking={"12": True})
        self.measure_journal.write_text("{}")
        e = self.refused(h, "runtime.plan", {"revert": True}, "CAPS_CONFLICT")
        self.assertIn("measure-journal.json", e["message"])
        self.assertEqual((self.cap(3), self.files.online[12]), (4_500_000, 0))
        self.measure_journal.unlink()
        self.go(h, parking={"13": True})
        self.measure_journal.write_text("{}")
        self.files.write_int("cpu3/cpufreq/scaling_max_freq", P_MAX)  # nothing left to restore for caps: only unparks remain
        self.assertTrue(self.apply(h, self.plan(h, revert=True))["complete"])
        self.assertEqual((self.files.online[12], self.files.online[13]), (1, 1))

    def test_the_lock_stops_a_revert_too(self) -> None:
        h = self.boot()
        self.go(h, caps={"3": 45})
        plan = self.plan(h, revert=True)
        self.lock.write_text("4242\n")
        self.refused(h, "runtime.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM " + plan["nonce"]}, "LOCKED")
        self.assertEqual(self.cap(3), 4_500_000)


class Journal(RuntimeCase):
    def put(self, doc: dict[str, Any]) -> None:
        base = {"version": 1, "boot_id": BOOT, "created": 1.0, "pid": 1, "in_flight": False, "caps": {}, "online": {}, "uncore": {}}
        self.journal.write_text(json.dumps({**base, **doc}))
        self.journal.chmod(0o600)

    def test_a_journal_of_another_boot_is_not_applied_and_the_next_write_replaces_it(self) -> None:
        h = self.boot()
        self.go(h, caps={"3": 45})
        doc = self.journal_doc()
        doc["boot_id"] = "another-boot"
        self.put(doc)
        self.refused(h, "runtime.plan", {"revert": True}, "NO_JOURNAL")
        self.go(h, caps={"3": 40})
        self.assertEqual(self.journal_doc()["boot_id"], BOOT)
        self.assertEqual(self.journal_doc()["caps"], {"3": 4_500_000})  # the journal restarted from what was in force then

    def test_a_corrupt_journal_is_refused_not_guessed(self) -> None:
        h = self.boot()
        self.journal.write_text("not json")
        self.journal.chmod(0o600)
        self.refused(h, "runtime.plan", {"revert": True}, "JOURNAL_UNUSABLE")
        self.refused(h, "runtime.plan", {"caps": {"3": 45}}, "JOURNAL_UNUSABLE")
        self.assertEqual(self.files.writes, [])

    def test_values_the_controls_cannot_hold_are_refused(self) -> None:
        h = self.boot()
        for doc in (
            {"caps": {"5000": 4_500_000}}, {"caps": {"x": 4_500_000}}, {"caps": {"3": 99_999_999}}, {"caps": {"3": 0}}, {"caps": {"3": -1}}, {"caps": {"3": True}},
            {"online": {"3": 2}}, {"online": {"3": -1}}, {"uncore": {"mid_ratio": 3_000_000}}, {"uncore": {"max_ratio": 99_999_999}}, {"in_flight": "yes"}, {"version": 2},
        ):
            self.put(doc)
            self.refused(h, "runtime.plan", {"revert": True}, "JOURNAL_UNUSABLE")
        self.assertEqual(self.files.writes, [])

    def test_a_group_writable_journal_is_not_trusted(self) -> None:
        h = self.boot()
        self.go(h, caps={"3": 45})
        self.journal.chmod(0o666)
        self.refused(h, "runtime.plan", {"revert": True}, "JOURNAL_UNTRUSTED")

    def test_the_journal_path_is_trust_checked_on_read_and_write(self) -> None:
        h = self.boot()
        with mock.patch.object(ops_runtime, "_trust", wraps=ops_runtime._trust) as trust:
            self.go(h, caps={"3": 45})
            self.ok(h, "runtime.read")
        checks = [c for c in trust.call_args_list if c.args[1] == self.journal]
        self.assertGreaterEqual(len(checks), 4)
        self.assertTrue(all(c.kwargs == {"want": "file", "must_exist": False} for c in checks))

    def test_the_boot_id_must_be_known(self) -> None:
        h = self.boot(boot_id_fn=lambda: "")
        plan = self.plan(h, caps={"3": 45})
        self.refused(h, "runtime.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM " + plan["nonce"]}, "BOOT_ID_UNKNOWN")
        self.assertEqual(self.files.writes, [])


class Recovery(RuntimeCase):
    """The accept: a journal restore at helper start -- only of an apply that died in flight; a settled journal is kept for revert and never applied at start."""

    def crash_midway(self, h: Any) -> None:
        """An apply that dies after its first write: the second write raises something the apply does not handle."""
        files = self.files
        real = files.write_int

        def dying(name: str, value: int) -> None:
            if files.writes:
                raise RuntimeError("the helper died")
            real(name, value)

        plan = self.plan(h, caps={"3": 45, "4": 44}, parking={"12": True})
        with mock.patch.object(files, "write_int", side_effect=dying):
            msg = self.call(h, "runtime.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM " + plan["nonce"]})
        self.assertFalse(msg["ok"], msg)

    def test_an_apply_that_died_in_flight_is_restored_at_start(self) -> None:
        h = self.boot()
        cap4 = self.cap(4)
        self.crash_midway(h)
        self.assertTrue(self.journal_doc()["in_flight"])
        self.assertEqual(len(self.files.writes), 1)  # the first step was written, the second never completed
        self.assertEqual(self.cap(3), 4_500_000)
        # the next plan refuses until the revert: nothing is built on a half-applied state
        self.refused(h, "runtime.plan", {"caps": {"5": 45}}, "JOURNAL_IN_FLIGHT")
        # a new helper starts: it restores the journal
        self.logs.clear()
        self.assertTrue(ops_runtime.recover_journal(h.helper))
        self.assertEqual((self.cap(3), self.cap(4), self.files.online[12]), (P_MAX, cap4, 1))
        self.assertFalse(self.journal_doc()["in_flight"])
        self.assertTrue(any("restored" in line and "cleared" in line for line in self.logs), self.logs)
        recs = [r for r in self.runtime_records() if r["event"] == "runtime_attempt" and r["revert"]]
        self.assertTrue(recs)  # audited like any write
        self.assertFalse(self.lock.exists())
        self.ok(h, "runtime.plan", {"caps": {"5": 45}})  # planning works again

    def test_a_settled_journal_is_not_applied_at_start(self) -> None:
        h = self.boot()
        self.go(h, caps={"3": 45}, parking={"12": True})
        self.files.writes.clear()
        self.assertFalse(ops_runtime.recover_journal(h.helper))  # the helper idles out and restarts: the operator's caps stay
        self.assertEqual((self.files.writes, self.cap(3), self.files.online[12]), ([], 4_500_000, 0))

    def test_no_journal_and_a_journal_of_another_boot_do_nothing(self) -> None:
        h = self.boot()
        self.assertFalse(ops_runtime.recover_journal(h.helper))
        self.journal.write_text(json.dumps({"version": 1, "boot_id": "another-boot", "created": 1.0, "pid": 1, "in_flight": True, "caps": {"3": P_MAX}, "online": {}, "uncore": {}}))
        self.journal.chmod(0o600)
        self.assertFalse(ops_runtime.recover_journal(h.helper))
        self.assertEqual(self.files.writes, [])

    def test_an_untrusted_or_unusable_journal_is_logged_and_not_applied(self) -> None:
        h = self.boot()
        self.journal.write_text("garbage")
        self.journal.chmod(0o600)
        self.logs.clear()
        self.assertFalse(ops_runtime.recover_journal(h.helper))
        self.assertTrue(any("not applied" in line and "unusable" in line for line in self.logs), self.logs)
        self.journal.write_text(json.dumps({"version": 1, "boot_id": BOOT, "created": 1.0, "pid": 1, "in_flight": True, "caps": {"3": P_MAX}, "online": {}, "uncore": {}}))
        self.journal.chmod(0o666)
        self.assertFalse(ops_runtime.recover_journal(h.helper))
        self.assertEqual(self.files.writes, [])

    def test_a_restore_that_cannot_complete_keeps_the_mark(self) -> None:
        h = self.boot()
        self.crash_midway(h)
        self.lock.write_text("4242\n")  # another process holds the lock: the first restore write is refused
        self.logs.clear()
        self.assertTrue(ops_runtime.recover_journal(h.helper))
        self.assertTrue(self.journal_doc()["in_flight"])
        self.assertTrue(any("NOT restored" in line for line in self.logs), self.logs)
        self.lock.unlink()
        self.assertTrue(ops_runtime.recover_journal(h.helper))  # the next start tries again
        self.assertFalse(self.journal_doc()["in_flight"])

    def test_a_revert_also_clears_an_in_flight_journal_for_the_operator(self) -> None:
        h = self.boot()
        self.crash_midway(h)
        res = self.apply(h, self.plan(h, revert=True))
        self.assertTrue(res["complete"], res)
        self.assertEqual((self.cap(3), self.files.online[12]), (P_MAX, 1))
        self.assertFalse(self.journal_doc()["in_flight"])  # the revert settled it
        self.ok(h, "runtime.plan", {"caps": {"5": 45}})  # and planning works again


class SimDefaults(RuntimeCase):
    def test_a_simulated_helper_without_wiring_uses_the_measure_wirings_cpufreq(self) -> None:
        from arltuner.helper.ops_measure import sim_wiring

        self.policy.write_text(BASE + RUNTIME, encoding="utf-8", newline="\n")
        h = self.start()  # no runtime_wiring: --sim builds SimRuntimeFiles over the measure wiring's SimCpufreq, the journal next to the audit log
        mw = sim_wiring(self.tmp / "state")
        h.helper.measure_wiring = mw
        r = self.ok(h, "runtime.read")
        self.assertEqual(Path(r["journal_path"]).parent, self.audit.parent)
        self.go(h, caps={"3": 45}, uncore={"max_ratio": 30})
        self.assertEqual(mw.cpufreq.read(3, "scaling_max_freq"), "4500000")  # the same state a stress / measure run caps
        # and the measure journal now blocks us
        mw.journal_path.parent.mkdir(parents=True, exist_ok=True)
        mw.journal_path.write_text("{}")
        self.refused(h, "runtime.plan", {"caps": {"4": 45}}, "CAPS_CONFLICT")

    def test_no_measure_support_is_an_internal_error_in_the_simulator(self) -> None:
        self.policy.write_text(BASE + RUNTIME, encoding="utf-8", newline="\n")
        h = self.start()
        msg = self.call(h, "runtime.read")
        self.assertEqual(msg["error"]["code"], "INTERNAL", msg)


if __name__ == "__main__":
    import unittest

    unittest.main()
