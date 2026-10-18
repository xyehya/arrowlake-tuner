"""G-214: ``power.read`` / ``power.plan`` / ``power.apply`` through the real helper loop on the simulator. The powercap zone is the simulated kernel (``SimPowerFiles``, MSRs kept
coherent in the simulated port) or a fake sysfs tree (``make_powercap``); no hardware, no /sys, no /run."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from arltuner.controls import power as pw
from arltuner.helper import ops_power
from arltuner.helper.ops_power import PowerWiring

from ._support import HelperCase

UW = 1_000_000

MSR_READS = """
[[msr_allow]]
name = "r606"
msr = 0x606
kind = "read"

[[msr_allow]]
name = "r610"
msr = 0x610
kind = "read"

[[msr_allow]]
name = "r614"
msr = 0x614
kind = "read"
"""
POWER = "\n[power]\npl1_w = [100, 253]\npl2_w = [100, 295]\ntau_s = [1, 200]\nmax_step_w = 100\n"
BASE = 'cpus = ["0-23"]\nmax_operations_per_session = 300\nmax_operations_per_boot = 400\nmin_dwell_s = 0.0\n'


class PowerCase(HelperCase):
    n = 0

    def boot(self, policy: str = BASE + MSR_READS + POWER, sim: pw.SimPowerFiles | None = None, **cfg: Any) -> Any:
        self.policy.write_text(policy, encoding="utf-8", newline="\n")
        self.zone = sim or pw.SimPowerFiles()
        self.journal = self.tmp / "power-journal.json"
        h = self.start(self.config(**cfg) if cfg else None)
        h.helper.power_wiring = PowerWiring(self.zone, self.journal)
        return h

    def call(self, h: Any, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        self.n += 1
        return h.call(f"p{self.n}", op, args)

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

    def plan(self, h: Any, power: dict[str, Any]) -> dict[str, Any]:
        return self.ok(h, "power.plan", {"power": power})

    def apply(self, h: Any, plan: dict[str, Any]) -> dict[str, Any]:
        return self.ok(h, "power.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM " + plan["nonce"]})

    def audit_records(self) -> list[dict[str, Any]]:
        if not self.audit.exists():
            return []
        return [json.loads(line) for line in self.audit.read_text().splitlines() if line.strip()]

    def power_records(self) -> list[dict[str, Any]]:
        return [r for r in self.audit_records() if r.get("kind") == "powercap"]


class Read(PowerCase):
    def test_reads_the_zone_and_the_registers(self) -> None:
        h = self.boot()
        r = self.ok(h, "power.read")
        self.assertTrue(r["available"])
        self.assertEqual((r["powercap"]["pl1_w"], r["powercap"]["pl2_w"], r["powercap"]["tau_us"]), (250.0, 295.0, 127926272))
        lim = r["msr"]["limit"]
        self.assertEqual((lim["pl1_w"], lim["pl2_w"], lim["locked"]), (250.0, 295.0, False))
        self.assertEqual(r["msr"]["info"]["tdp_w"], 125.0)
        self.assertTrue(r["writable"])
        self.assertIsNone(r["read_only_reason"])
        self.assertEqual(r["policy"]["pl1_w"], [100, 253])
        self.assertIsNone(r["boot"])  # nothing written this boot
        self.assertEqual(self.power_records(), [])  # a read audits no powercap action

    def test_the_lock_bit_is_read_only_with_the_reason(self) -> None:
        h = self.boot(sim=pw.SimPowerFiles(locked=True))
        r = self.ok(h, "power.read")
        self.assertTrue(r["locked"])
        self.assertFalse(r["writable"])
        self.assertIn("bit 63", r["read_only_reason"])
        self.assertEqual(r["powercap"]["pl1_w"], 250.0)  # the values are still shown

    def test_a_policy_without_the_msr_entries_still_reads_the_files(self) -> None:
        h = self.boot(BASE + POWER)
        r = self.ok(h, "power.read")
        self.assertEqual(r["powercap"]["pl1_w"], 250.0)
        self.assertIsNone(r["msr"])
        self.assertIn("0x610", r["msr_note"])
        self.assertFalse(r["writable"])
        self.assertIn("lock bit is unknown", r["read_only_reason"])

    def test_an_unreadable_register_is_a_note(self) -> None:
        h = self.boot(sim=pw.SimPowerFiles(unreadable_msr=True))
        r = self.ok(h, "power.read")
        self.assertIsNone(r["msr"]["limit"])
        self.assertIn("0x610", r["msr_note"])
        self.assertFalse(r["writable"])

    def test_file_and_register_disagreeing_is_reported(self) -> None:
        h = self.boot(sim=pw.SimPowerFiles(msr_skew_w=7))
        r = self.ok(h, "power.read")
        self.assertIn("disagree", r["msr_note"])

    def test_no_zone(self) -> None:
        h = self.boot()
        h.helper.power_wiring = PowerWiring(pw.PowerFiles(self.tmp / "nothing"), self.journal)
        r = self.ok(h, "power.read")
        self.assertFalse(r["available"])
        self.assertIn("no powercap zone", r["read_only_reason"])

    def test_fake_sysfs_tree(self) -> None:
        from tests.fakesys import make_powercap

        h = self.boot()
        make_powercap(self.tmp / "pc")
        h.helper.power_wiring = PowerWiring(pw.PowerFiles(self.tmp / "pc"), self.journal)
        r = self.ok(h, "power.read")
        self.assertEqual(r["powercap"]["pl2_uw"], 295 * UW)
        self.assertIsNone(r["msr"])  # the simulated port was never synced: 0x610 is not configured there -> a note, not an error
        self.assertTrue(r["available"])


class Apply(PowerCase):
    def test_a_pl2_change_is_planned_written_read_back_audited_and_journaled(self) -> None:
        h = self.boot()
        plan = self.plan(h, {"pl2_w": 275})
        self.assertEqual([(s["key"], s["file"], s["before"], s["value"]) for s in plan["steps"]], [("pl2_w", "constraint_1_power_limit_uw", 295 * UW, 275 * UW)])
        self.assertEqual(plan["ops_needed"], 0)
        self.assertTrue(plan["journal_new"])
        self.assertEqual(self.zone.writes, [])  # planning wrote nothing
        res = self.apply(h, plan)
        self.assertTrue(res["complete"])
        self.assertEqual(res["writes_sent"], 1)
        self.assertEqual(self.zone.writes, [("constraint_1_power_limit_uw", 275 * UW)])  # one file, once
        self.assertEqual(res["readback"], [{"key": "pl2_w", "expected": [275 * UW, 275 * UW], "file": 275 * UW, "msr": 275 * UW, "match": True}])
        self.assertEqual(res["after"]["pl2_w"], 275.0)
        # the journal holds the values before the first write, bound to the boot
        doc = json.loads(self.journal.read_text())
        self.assertEqual((doc["boot_id"], doc["values"]), ("11111111-2222-3333-4444-555555555555", {"pl1_uw": 250 * UW, "pl2_uw": 295 * UW, "tau_us": 127926272}))
        self.assertEqual(oct(self.journal.stat().st_mode & 0o777), "0o600")
        # the audit: a record before the write, one after the read-back, in that order, none of them an "attempt"
        recs = self.power_records()
        self.assertEqual([r["event"] for r in recs], ["powercap_attempt", "readback"])
        self.assertEqual((recs[0]["written"], recs[0]["before"], recs[0]["key"], recs[0]["plan_id"]), (275 * UW, 295 * UW, "pl2_w", plan["plan_id"]))
        self.assertTrue(recs[1]["matches"])
        self.assertFalse([r for r in self.audit_records() if r["event"] == "attempt"])
        again = self.ok(h, "power.read")
        self.assertEqual(again["boot"]["pl2_w"], 295.0)

    def test_several_files_are_written_one_per_step_in_a_safe_order(self) -> None:
        h = self.boot()
        plan = self.plan(h, {"pl1_w": 200, "pl2_w": 250, "tau_s": 100})
        self.assertEqual([s["key"] for s in plan["steps"]], ["pl1_w", "pl2_w", "tau_s"])  # new PL1 200 <= old PL2 295
        res = self.apply(h, plan)
        self.assertTrue(res["complete"])
        self.assertEqual([w[0] for w in self.zone.writes], ["constraint_0_power_limit_uw", "constraint_1_power_limit_uw", "constraint_0_time_window_us"])
        tau = res["readback"][2]
        snapped = pw.tau_snap(100 * UW, 976)
        self.assertEqual(plan["steps"][2]["value"], snapped)  # the plan shows the snapped window
        self.assertLessEqual(snapped, 100 * UW)
        self.assertEqual(tau["expected"], [snapped, snapped])
        self.assertEqual(tau["file"], snapped)  # the kernel model rounds down, and the read-back is exact
        self.assertTrue(all(r["match"] for r in res["readback"]))
        self.assertEqual([r["event"] for r in self.power_records()], ["powercap_attempt", "readback"] * 3)

    def test_no_policy_keys_no_write_and_the_refusal_names_the_key(self) -> None:
        h = self.boot(BASE + MSR_READS)  # no [power] table
        for key, value in (("pl1_w", 200), ("pl2_w", 280), ("tau_s", 50)):
            err = self.refused(h, "power.plan", {"power": {key: value}}, "POWER_POLICY_MISSING")
            self.assertEqual(err["detail"]["key"], key)
            self.assertIn(f"[power] {key}", err["message"])
        self.assertEqual(self.zone.writes, [])
        self.assertFalse(self.journal.exists())

    def test_bounds_and_steps_are_refused_with_nothing_written(self) -> None:
        h = self.boot()
        self.refused(h, "power.plan", {"power": {"pl1_w": 254}}, "POWER_RANGE")
        self.refused(h, "power.plan", {"power": {"pl2_w": 90}}, "POWER_RANGE")
        self.refused(h, "power.plan", {"power": {"pl1_w": 100, "pl2_w": 295}}, "POWER_STEP")  # 150 W down, max_step_w = 100
        self.refused(h, "power.plan", {"power": {"pl2_w": 200}}, "POWER_ORDER")  # below the PL1 in force
        self.assertEqual(self.zone.writes, [])

    def test_the_lock_bit_makes_the_plan_refuse(self) -> None:
        h = self.boot(sim=pw.SimPowerFiles(locked=True))
        err = self.refused(h, "power.plan", {"power": {"pl1_w": 200}}, "POWER_LOCKED")
        self.assertIn("bit 63", err["message"])
        self.assertEqual(self.zone.writes, [])

    def test_a_lock_set_after_the_plan_refuses_the_apply(self) -> None:
        h = self.boot()
        plan = self.plan(h, {"pl1_w": 200})
        self.zone.locked = True
        self.refused(h, "power.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM " + plan["nonce"]}, "POWER_LOCKED")
        self.assertEqual(self.zone.writes, [])
        self.assertFalse(self.journal.exists())

    def test_a_silently_ignored_write_trips_on_the_read_back(self) -> None:
        h = self.boot(sim=pw.SimPowerFiles(drop_writes=True))
        plan = self.plan(h, {"pl1_w": 200, "pl2_w": 250})
        res = self.apply(h, plan)
        self.assertFalse(res["complete"])
        self.assertEqual(res["stop"]["kind"], "unverified")
        self.assertTrue(res["stop"]["tripped"])
        self.assertEqual(len(self.zone.writes), 1)  # stopped at the first: PL2 was never written
        self.assertEqual([s["status"] for s in res["steps"]], ["unverified", "not_sent"])
        self.assertFalse(res["readback"][0]["match"])
        self.assertIn("reads 250000000, expected 200000000", res["readback"][0]["problem"])
        self.assertEqual([r["event"] for r in self.power_records()], ["powercap_attempt", "readback", "trip"])
        self.assertEqual(self.power_records()[-1]["reason"], "TRIPPED")

    def test_the_register_disagreeing_with_the_file_trips(self) -> None:
        h = self.boot(sim=pw.SimPowerFiles(msr_skew_w=5))
        res = self.apply(h, self.plan(h, {"pl2_w": 280}))
        self.assertFalse(res["complete"])
        self.assertTrue(res["stop"]["tripped"])
        self.assertIn("MSR 0x610", res["readback"][0]["problem"])

    def test_the_nonce_gates_the_apply_and_a_plan_is_single_use(self) -> None:
        h = self.boot()
        plan = self.plan(h, {"pl2_w": 280})
        msg = self.call(h, "power.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM AAAAAA"})
        self.assertEqual(msg["error"]["code"], "CONFIRM_MISMATCH")
        self.assertEqual(self.zone.writes, [])
        self.apply(h, plan)
        msg = self.call(h, "power.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM " + plan["nonce"]})
        self.assertEqual(msg["error"]["code"], "CONFIRM_MISMATCH")  # a replay
        self.assertEqual(len(self.zone.writes), 1)

    def test_values_that_moved_since_the_plan_make_it_stale(self) -> None:
        h = self.boot()
        plan = self.plan(h, {"pl2_w": 280})
        self.zone.pl2_uw = 290 * UW  # someone else changed PL2
        msg = self.call(h, "power.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM " + plan["nonce"]})
        self.assertEqual(msg["error"]["code"], "PLAN_STALE")
        self.assertEqual(self.zone.writes, [])

    def test_nothing_to_change_is_a_refusal(self) -> None:
        h = self.boot()
        self.refused(h, "power.plan", {"power": {"pl1_w": 250}}, "NOTHING_TO_DO")

    def test_a_bad_shape_is_a_bad_request(self) -> None:
        h = self.boot()
        for args in ({}, {"power": {}}, {"power": {"pl3_w": 1}}, {"power": {"pl1_w": "9"}}, {"power": {"pl1_w": True}}, {"power": {"pl1_w": 200}, "revert": True}, {"revert": False}, {"power": 5}):
            msg = self.call(h, "power.plan", args)
            self.assertEqual(msg["error"]["code"], "BAD_REQUEST", (args, msg))

    def test_the_audit_log_that_cannot_be_written_stops_before_the_first_write(self) -> None:
        h = self.boot()
        plan = self.plan(h, {"pl2_w": 280})
        self.audit.unlink()
        self.audit.mkdir()  # not a file any more: AuditLog cannot open it
        msg = self.call(h, "power.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM " + plan["nonce"]})
        self.assertFalse(msg["ok"], msg)
        self.assertEqual(self.zone.writes, [])


class Witnessed(pw.SimPowerFiles):
    """Records what the audit log and the lock file look like at the moment each file write happens."""

    def __init__(self, case: PowerCase, **kw: Any) -> None:
        super().__init__(**kw)
        self.case = case
        self.seen: list[tuple[str, int, bool]] = []

    def write_int(self, name: str, value: int) -> None:
        last = self.case.power_records()[-1]
        self.seen.append((last["event"], last["written"], os.path.exists(self.case.lock)))
        super().write_int(name, value)


class Safeguards(PowerCase):
    def test_the_audit_pre_record_and_the_lock_exist_when_the_write_happens(self) -> None:
        sim = Witnessed(self)
        h = self.boot(sim=sim)
        res = self.apply(h, self.plan(h, {"pl1_w": 200, "pl2_w": 250}))
        self.assertTrue(res["complete"], res)
        self.assertEqual(sim.seen, [("powercap_attempt", 200 * UW, True), ("powercap_attempt", 250 * UW, True)])
        self.assertFalse(self.lock.exists())  # released after each transaction
        recs = self.power_records()
        self.assertEqual([r["event"] for r in recs], ["powercap_attempt", "readback"] * 2)
        self.assertFalse([r for r in self.audit_records() if r["event"] == "attempt"])  # the guard's budget and dwell are untouched

    def test_a_held_lock_refuses_before_anything_is_sent(self) -> None:
        h = self.boot()
        plan = self.plan(h, {"pl2_w": 280})

        def hold(*args: Any, **kw: Any) -> None:  # another process takes the lock after the apply re-planned (the guard's reads need it free, so it appears late)
            self.lock.write_text("4242\n")

        with mock.patch.object(ops_power, "_write_journal", side_effect=hold):
            msg = self.call(h, "power.apply", {"plan_id": plan["plan_id"], "typed": "CONFIRM " + plan["nonce"]})
        self.assertEqual((msg["error"]["code"], msg["error"]["detail"]["check"]), ("REFUSED", "LOCKED"), msg)
        self.assertEqual(self.zone.writes, [])
        self.assertEqual(self.power_records(), [])  # no pre-record: nothing was going to be sent
        self.assertEqual(self.lock.read_text(), "4242\n")  # someone else's lock is never removed

    def test_a_health_gate_that_fails_during_the_read_back_stops_the_apply(self) -> None:
        sim = pw.SimPowerFiles()
        h = self.boot(sim=sim, health_gate_factory=lambda: (lambda phase: (not sim.writes, "the machine is unwell")))
        plan = self.plan(h, {"pl1_w": 200, "pl2_w": 250})
        res = self.apply(h, plan)
        self.assertFalse(res["complete"])
        self.assertEqual(res["stop"]["kind"], "unverified")
        self.assertTrue(res["stop"]["tripped"])
        self.assertEqual(len(sim.writes), 1)  # the second step was never sent
        self.assertEqual([s["status"] for s in res["steps"]], ["unverified", "not_sent"])
        self.assertIn("could not be read back", res["readback"][0]["problem"])
        self.assertEqual([r["event"] for r in self.power_records()], ["powercap_attempt", "readback", "trip"])

    def test_the_journal_path_is_trust_checked_on_read_and_write(self) -> None:
        h = self.boot()
        with mock.patch.object(ops_power, "_trust", wraps=ops_power._trust) as trust:
            self.apply(h, self.plan(h, {"pl2_w": 260}))
            self.ok(h, "power.read")
        journal_checks = [c for c in trust.call_args_list if c.args[1] == self.journal]
        self.assertGreaterEqual(len(journal_checks), 3)  # plan (read), apply (read + write), read
        self.assertTrue(all(c.kwargs == {"want": "file", "must_exist": False} for c in journal_checks))

    def test_tau_is_snapped_down_in_the_plan_and_read_back_exactly(self) -> None:
        h = self.boot(BASE + MSR_READS + "\n[power]\npl1_w = [100, 253]\npl2_w = [100, 295]\ntau_s = [1, 128]\nmax_step_w = 100\n")
        self.apply(h, self.plan(h, {"tau_s": 60}))
        plan = self.plan(h, {"tau_s": 128})
        self.assertEqual(plan["steps"][0]["value"], 127926272)  # 127.9 s: never the next window up (159.9 s)
        self.assertEqual(plan["after"]["tau_s"], 127.926272)
        res = self.apply(h, plan)
        self.assertTrue(res["complete"], res)
        self.assertEqual((res["readback"][0]["file"], res["readback"][0]["msr"]), (127926272, 127926272))
        self.assertEqual(self.zone.read_int(pw.FILES["tau_s"]), 127926272)

    def test_a_tau_that_snaps_below_the_minimum_is_refused(self) -> None:
        h = self.boot(BASE + MSR_READS + "\n[power]\ntau_s = [1, 200]\n")
        err = self.refused(h, "power.plan", {"power": {"tau_s": 1}}, "POWER_RANGE")
        self.assertIn("snapped down", err["message"])
        self.assertEqual(self.zone.writes, [])


class Revert(PowerCase):
    def test_a_journal_the_registers_cannot_hold_is_refused(self) -> None:
        h = self.boot()
        for values in ({"pl1_uw": 4096 * UW, "pl2_uw": 295 * UW, "tau_us": 127926272}, {"pl1_uw": 250 * UW, "pl2_uw": 2**63, "tau_us": 127926272}, {"pl1_uw": 250 * UW, "pl2_uw": 295 * UW, "tau_us": 2**70}):
            with self.subTest(values=values):
                doc = {"version": 1, "boot_id": "11111111-2222-3333-4444-555555555555", "created": 0, "pid": 1, "values": values, "note": ""}
                self.journal.write_text(json.dumps(doc))
                self.journal.chmod(0o600)
                self.refused(h, "power.plan", {"revert": True}, "JOURNAL_UNUSABLE")
        self.assertEqual(self.zone.writes, [])

    def test_a_revert_warns_when_a_boot_value_is_outside_the_policy(self) -> None:
        h = self.boot()
        self.apply(h, self.plan(h, {"pl1_w": 200}))
        self.policy.write_text(BASE + MSR_READS + "\n[power]\npl1_w = [100, 200]\ntau_s = [1, 200]\nmax_step_w = 100\n", encoding="utf-8", newline="\n")
        plan = self.ok(h, "power.plan", {"revert": True})  # the boot PL1 is 250 W, the policy now says <= 200
        self.assertTrue(any("boot value of pl1_w (250)" in w for w in plan["warnings"]), plan["warnings"])
        self.assertFalse(any("pl2_w" in w for w in plan["warnings"]))  # no [power] pl2_w: nothing to compare with
        self.assertTrue(self.apply(h, plan)["complete"])  # and it still restores
        self.assertEqual(self.zone.pl1_uw, 250 * UW)

    def test_revert_puts_the_boot_values_back(self) -> None:
        h = self.boot()
        self.apply(h, self.plan(h, {"pl1_w": 200, "pl2_w": 250, "tau_s": 60}))
        self.apply(h, self.plan(h, {"pl1_w": 150}))  # a second apply must not overwrite the boot values
        self.assertEqual(json.loads(self.journal.read_text())["values"]["pl1_uw"], 250 * UW)
        plan = self.ok(h, "power.plan", {"revert": True})
        self.assertTrue(plan["revert"])
        self.assertEqual([s["key"] for s in plan["steps"]], ["pl1_w", "pl2_w", "tau_s"])  # from (150, 250): the new PL1 250 does not exceed the PL2 in force, so PL1 first
        self.assertFalse(plan["journal_new"])
        res = self.apply(h, plan)
        self.assertTrue(res["complete"], res)
        self.assertEqual(self.zone.pl1_uw, 250 * UW)
        self.assertEqual(self.zone.pl2_uw, 295 * UW)
        self.assertEqual(self.zone.read_int(pw.FILES["tau_s"]), 127926272)
        self.assertTrue(self.journal.exists())  # never removed
        self.assertTrue(all(r["match"] for r in res["readback"]))

    def test_revert_without_a_journal_is_refused(self) -> None:
        h = self.boot()
        self.refused(h, "power.plan", {"revert": True}, "NO_JOURNAL")

    def test_revert_needs_no_policy_keys(self) -> None:
        h = self.boot()
        self.apply(h, self.plan(h, {"pl2_w": 260}))
        self.policy.write_text(BASE + MSR_READS, encoding="utf-8", newline="\n")  # the operator removed [power]
        plan = self.ok(h, "power.plan", {"revert": True})
        self.assertTrue(self.apply(h, plan)["complete"])
        self.assertEqual(self.zone.pl2_uw, 295 * UW)

    def test_the_lock_stops_a_revert_too(self) -> None:
        h = self.boot()
        self.apply(h, self.plan(h, {"pl2_w": 260}))
        self.zone.locked = True
        self.refused(h, "power.plan", {"revert": True}, "POWER_LOCKED")

    def test_a_journal_of_another_boot_is_not_applied(self) -> None:
        h = self.boot()
        self.apply(h, self.plan(h, {"pl2_w": 260}))
        doc = json.loads(self.journal.read_text())
        doc["boot_id"] = "another-boot"
        self.journal.write_text(json.dumps(doc))
        self.journal.chmod(0o600)
        self.refused(h, "power.plan", {"revert": True}, "NO_JOURNAL")
        # the next write of this boot replaces it with this boot's values
        self.apply(h, self.plan(h, {"pl2_w": 270}))
        self.assertEqual(json.loads(self.journal.read_text())["boot_id"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(json.loads(self.journal.read_text())["values"]["pl2_uw"], 260 * UW)

    def test_a_corrupt_journal_is_refused_not_guessed(self) -> None:
        h = self.boot()
        self.journal.write_text("not json")
        self.journal.chmod(0o600)
        self.refused(h, "power.plan", {"revert": True}, "JOURNAL_UNUSABLE")
        self.refused(h, "power.plan", {"power": {"pl2_w": 260}}, "JOURNAL_UNUSABLE")
        self.assertEqual(self.zone.writes, [])

    def test_a_group_writable_journal_is_not_trusted(self) -> None:
        h = self.boot()
        self.apply(h, self.plan(h, {"pl2_w": 260}))
        self.journal.chmod(0o666)
        self.refused(h, "power.plan", {"revert": True}, "JOURNAL_UNTRUSTED")


class SimDefaults(PowerCase):
    def test_a_simulated_helper_without_wiring_has_its_own_zone(self) -> None:
        self.policy.write_text(BASE + MSR_READS + POWER, encoding="utf-8", newline="\n")
        h = self.start()  # no power_wiring: --sim gets SimPowerFiles and the journal next to the audit log
        r = self.ok(h, "power.read")
        self.assertEqual(r["powercap"]["pl1_w"], 250.0)
        self.assertTrue(r["writable"])
        res = self.apply(h, self.plan(h, {"pl2_w": 280}))
        self.assertTrue(res["complete"])
        self.assertEqual(res["after"]["pl2_w"], 280.0)
        self.assertEqual(Path(r["journal"]).parent, self.audit.parent)

    def test_a_new_simulated_port_per_session_still_shows_the_registers(self) -> None:
        """``--sim`` (helper/main.py) builds a fresh ``SimMsrPort`` for every session: the zone's registers must reach each of them."""
        from arltuner.msr import SimConfig, SimMsrPort

        self.policy.write_text(BASE + MSR_READS + POWER, encoding="utf-8", newline="\n")
        h = self.start(self.config(port_factory=lambda: SimMsrPort(SimConfig.measured(per_point=True, reject_zero_fields=True))))
        self.assertEqual(self.ok(h, "power.read")["msr"]["limit"]["pl2_w"], 295.0)
        res = self.apply(h, self.plan(h, {"pl2_w": 270}))
        self.assertTrue(res["complete"], res)
        self.assertEqual(self.ok(h, "power.read")["msr"]["limit"]["pl2_w"], 270.0)


if __name__ == "__main__":
    import unittest

    unittest.main()
