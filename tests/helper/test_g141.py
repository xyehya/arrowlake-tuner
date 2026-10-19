"""G-141 (backend review fixes) through the real helper loop and the simulator: one regression test per item. No hardware, no sudo, temp directories only."""
from __future__ import annotations

import json
import os
import stat
import threading
from typing import Any

from arltuner.guard import AuditLog, Request
from arltuner.helper import ops_accept, ops_tune
from arltuner.helper.state import State
from arltuner.profiles import TurboTables, load_profile
from arltuner.profiles.fingerprint import parse_fingerprint

from ._support import error_code
from .test_accept import AcceptCase
from .test_continuity import LastReadCase
from .test_tune import ALL_P_53, TuneCase, row, rows


def set_dwell(case: TuneCase, seconds: float) -> None:
    text = case.policy.read_text()
    assert "min_dwell_s = 0.0" in text
    case.policy.write_text(text.replace("min_dwell_s = 0.0", f"min_dwell_s = {seconds}"))


class Item1PlanMatchesGuardRatio(LastReadCase):
    """The ratio ceiling of ``"auto"`` is kind-wide in the page, ``tune.plan``, the executor's plan and the guard alike."""

    def test_a_non_favored_core_may_take_the_kind_ceiling(self) -> None:
        h = self.boot()
        self.read(h)
        # P0 reads 0x3939 (fused 57), P1 0x3737 (fused 55): the kind ceiling is 57 + 3 = 60. P1's own word alone would cap it at 58.
        plan = self.plan(h, rows(row("P1", ratio=59)))
        res = self.apply_ok(h, plan)  # was: the plan inside tune.apply refused 59 on P1, after the nonce was burnt
        self.assertTrue(res["complete"])
        self.assertEqual(self.word(1).ratio, 59)

    def test_the_kind_ceiling_survives_a_helper_restart(self) -> None:
        h = self.boot()
        self.read(h)
        h2 = self.restart(h)
        plan = self.plan(h2, rows(row("P1", ratio=59)))  # item 5: the 0x1C words of the stored read are restored too
        self.assertTrue(self.apply_ok(h2, plan)["complete"])
        self.assertEqual(self.word(1).ratio, 59)

    def test_a_ratio_above_the_cpu_limit_is_still_refused_at_plan_time(self) -> None:
        h = self.boot()
        self.read(h)
        self.plan(h, rows(row("P1", ratio=61)))  # D-049: the old "auto" cap (fused 0x1C + 3 = 60) is gone; the CPU's own limit (cmd 0x01) is 120 here
        r = h.call("pp", "tune.plan", {"intent": rows(row("P1", ratio=121))})
        self.assertEqual(error_code(r), "REFUSED")
        self.assertEqual(r["error"]["detail"]["check"], "POLICY_RANGE")
        self.assertIn("CPU limit 120x (cmd 0x01", r["error"]["message"])


class Item2ExtrasArePaced(TuneCase):
    def test_a_policy_with_a_dwell_still_reads_every_extra_group(self) -> None:
        set_dwell(self, 1.0)
        h = self.boot()
        res = self.read(h)
        self.assertEqual(res["extras_notes"], {}, res["extras_notes"])
        self.assertEqual(sorted(res["hw_limits"]), ["E", "P"])
        self.assertEqual(res["cpu_limits"]["P"]["max_ratio"], 120)  # the CPU's own limit group was read despite the dwell
        self.assertIsNotNone(res["fused"])

    def test_a_refusal_other_than_the_allowlist_is_worded_by_its_reason(self) -> None:
        self.assertEqual(
            ops_tune._refused_text(ops_tune.GuardRefused(ops_tune.Refusal.DWELL, "0.0s since the previous operation")),
            "the guard refused (DWELL): 0.0s since the previous operation",
        )
        self.assertIn("no allow entry", ops_tune._refused_text(ops_tune.GuardRefused(ops_tune.Refusal.NOT_ALLOWLISTED, "x")))


class Item4AppliedTablesAreFolded(LastReadCase):
    def test_the_applied_turbo_table_is_what_the_helper_knows_afterwards(self) -> None:
        h = self.boot()
        self.read(h)
        self.apply_ok(h, self.plan(h, {**rows(row("EC0", ratio=49)), "turbo": {"E": [49] * 8}}))  # D-050: the E table is raised by naming it, no longer by the ratio
        st = h.helper._tune_state
        self.assertEqual(st.tables["e"], (49,) * 8)
        self.assertEqual(st.turbo["E"]["ratios"], [49] * 8)
        last = self.last(h)["result"]
        self.assertEqual(last["tables"]["e"], [49] * 8)
        self.assertEqual(last["turbo"]["E"]["ratios"], [49] * 8)
        h2 = self.restart(h)  # ... and the stored read carries it over a helper restart
        self.assertEqual(self.last(h2)["result"]["tables"]["e"], [49] * 8)
        plan = self.plan(h2, {**rows(row("EC0", ratio=49)), "turbo": {"E": [49] * 8}})
        self.assertFalse(any(s["kind"] == "write_msr" for s in plan["steps"]), plan["steps"])  # the table already holds 49: no second table write is planned


class Item5RestoreOrdering(LastReadCase):
    def test_a_concurrent_caller_waits_for_the_restore_and_gets_the_words(self) -> None:
        h = self.boot()
        self.read(h)
        h.stop()
        h2 = self.start(self.config())
        helper = h2.helper
        entered, release = threading.Event(), threading.Event()
        original = ops_tune._restore_last_locked

        def slow(helper_: Any) -> None:
            entered.set()
            release.wait(10)
            original(helper_)

        ops_tune._restore_last_locked = slow
        try:
            results: dict[str, Any] = {}
            a = threading.Thread(target=lambda: results.__setitem__("a", ops_tune._last_words(helper)))
            a.start()
            self.assertTrue(entered.wait(10))
            b = threading.Thread(target=lambda: results.__setitem__("b", ops_tune._last_words(helper)))
            b.start()
            b.join(0.3)
            self.assertTrue(b.is_alive(), "the second caller must wait for the restore, not see an empty state")
            release.set()
            a.join(10)
            b.join(10)
        finally:
            ops_tune._restore_last_locked = original
        self.assertIsNotNone(results["a"])
        self.assertEqual(results["b"], results["a"])

    def test_the_favored_words_come_back_after_a_restart(self) -> None:
        h = self.boot()
        self.read(h)
        before = dict(h.helper._tune_state.favored_words)
        h2 = self.restart(h)
        ops_tune._last_words(h2.helper)
        self.assertEqual(h2.helper._tune_state.favored_words, before)


class Item6NoHiddenCeilings(TuneCase):
    def test_a_ratio_and_a_v_target_above_the_old_sanity_ranges_reach_the_cpu_limit_check(self) -> None:
        h = self.boot()
        self.read(h)
        # 101x and 2.5 V were refused as BAD_REQUEST by a private 1..100 / 0.5..2.0 range; now only the CPU's limits answer (POLICY_RANGE): 101x is inside 120x
        self.plan(h, rows(row("P0", ratio=101)))
        for intent in (rows(row("P0", ratio=121)), rows(row("P0", v=2.5))):
            r = h.call(f"p{len(h.messages)}", "tune.plan", {"intent": intent})
            self.assertEqual(error_code(r), "REFUSED", r)
            self.assertEqual(r["error"]["detail"]["check"], "POLICY_RANGE", r)

    def test_the_encoding_bound_names_its_source(self) -> None:
        h = self.boot()
        r = h.call("p1", "tune.plan", {"intent": rows(row("P0", ratio=256))})
        self.assertEqual(error_code(r), "BAD_REQUEST")
        self.assertIn("1..255", r["error"]["message"])
        self.assertIn("ratio field [7:0]", r["error"]["message"])
        r = h.call("p2", "tune.plan", {"intent": rows(row("P0", v=4.0))})
        self.assertEqual(error_code(r), "BAD_REQUEST")
        self.assertIn("voltage field [19:8]", r["error"]["message"])

    def test_the_turbo_buckets_take_a_full_byte(self) -> None:
        h = self.boot()
        self.read(h)
        r = h.call("p3", "tune.plan", {"intent": {"turbo": {"P": [101, 101, 101, 101, 101, 101, 101, 101]}}})
        self.assertNotEqual(r.get("error", {}).get("code"), "BAD_REQUEST", r)  # the policy ceiling refuses it, not a hidden 100


class Item8AcceptFromAStaleRead(AcceptCase):
    def foreign_write(self) -> None:
        """What another process (the CLI) leaves in the audit log: an attempt of a 0x11 write."""
        AuditLog(self.audit).append("attempt", request=Request(3, 0x11, 0, 5, 0x1234, "another process"), boot_id="x")

    def test_a_write_after_the_read_makes_accept_ask_for_a_new_read(self) -> None:
        h = self.boot_accepting()
        self.foreign_write()
        r = self.accept(h, **{"from": "read"})
        self.assertEqual(error_code(r), "REFUSED", r)
        self.assertEqual(r["error"]["detail"]["check"], "STALE_READ")
        self.assertIn("Read again first", r["error"]["message"])
        self.assertFalse(self.acc.exists())
        self.read(h)  # a new read is current again
        self.assertTrue(self.accept(h, **{"from": "read"})["ok"])

    def test_a_complete_apply_of_this_process_keeps_the_words_current(self) -> None:
        h = self.boot_accepting()
        self.apply_ok(h, self.plan(h, rows(row("P3", points={"53": -60}))))
        r = self.accept(h, **{"from": "read"})  # no re-read: the verified read-backs are the words, and the apply's own writes are not "after" them
        self.assertTrue(r["ok"], r)

    def test_a_plain_msr_write_after_the_read_is_stale_too(self) -> None:
        h = self.boot_accepting()
        AuditLog(self.audit).append("attempt", request=ops_tune.MsrRequest(0, 0x1AD, 0x3939393939393939, "another process", "write"), boot_id="x")
        self.assertEqual(self.accept(h, **{"from": "read"})["error"]["detail"]["check"], "STALE_READ")

    def test_a_restarted_helper_keeps_the_mark_of_the_stored_read(self) -> None:
        h = self.boot_accepting()
        h.stop()
        h2 = self.start(self.config(accepted_dir=self.acc))
        self.assertTrue(self.accept(h2, **{"from": "read"})["ok"])  # nothing was written since the read
        self.foreign_write()
        self.assertEqual(self.accept(h2, **{"from": "read"})["error"]["detail"]["check"], "STALE_READ")

    def test_the_turbo_tables_of_the_read_are_in_the_accepted_profile(self) -> None:
        h = self.boot_accepting()
        self.tune(h, rows(row("P3", points={"53": -60})))
        name = self.accept(h, **{"from": "read"})["result"]["name"]
        profile = load_profile(self.accepted_files()[f"profiles/{name}.toml"])
        self.assertEqual(profile.turbo, TurboTables(e=(48,) * 8, p=(57, 57, 55, 55, 55, 55, 55, 55)))

    def test_accept_refuses_busy_while_another_op_holds_the_device(self) -> None:
        h = self.boot_accepting()
        h.helper.machine.begin("long1", "tune.apply", State.APPLYING)
        r = self.accept(h, **{"from": "read"})
        self.assertEqual(error_code(r), "BUSY", r)
        self.assertFalse(self.acc.exists())
        h.helper.machine.end("long1")
        self.assertTrue(self.accept(h, **{"from": "read"})["ok"])
        self.assertIsNone(h.helper.machine.running)  # accept let go of the device again

    def test_an_apply_cannot_start_while_accept_is_working(self) -> None:
        h = self.boot_accepting()
        plan = self.plan(h, rows(row("P3", points={"53": -60})))
        inside, release = threading.Event(), threading.Event()
        original = ops_accept._accept

        def slow(helper: Any, args: Any) -> Any:
            inside.set()
            release.wait(10)
            return original(helper, args)

        ops_accept._accept = slow
        try:
            h.send({"id": "acc1", "op": "profiles.accept", "args": {"from": "read"}})
            self.assertTrue(inside.wait(10))
            r = self.apply(h, plan)
            self.assertEqual(error_code(r), "BUSY", r)
            release.set()
            self.assertTrue(h.response("acc1")["ok"])
        finally:
            ops_accept._accept = original
        self.assertEqual(self.word(3, 5).offset_raw, 0)  # the refused apply wrote nothing


class Item9Fingerprint(AcceptCase):
    FP = "profiles.fingerprint"

    def fingerprint(self, h: Any, req_id: str = "fp") -> dict[str, Any]:
        return h.call(req_id + str(len(h.messages)), self.FP, {})

    def accepted_without_fingerprint(self, **overrides: Any) -> Any:
        cpuinfo = self.tmp / "cpuinfo"
        cpuinfo.write_text(
            "processor\t: 0\nvendor_id\t: GenuineIntel\ncpu family\t: 6\nmodel\t\t: 198\nmodel name\t: Intel(R) Core(TM) Ultra 9 285K\n\n"
        )
        h = self.boot_accepting(cpuinfo_path=cpuinfo, **overrides)
        self.tune(h, rows(row("P3", points={"53": -60})))
        self.assertTrue(self.accept(h, **{"from": "read"})["ok"])
        (self.acc / "fingerprint.json").unlink()  # an accepted state made before G-138
        return h

    def test_it_writes_the_fingerprint_alone(self) -> None:
        h = self.accepted_without_fingerprint()
        before = self.accepted_files()
        sent = len(self.sim.requests)
        r = self.fingerprint(h)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["fingerprint"], "complete")
        self.assertEqual((r["result"]["cpus"], len(r["result"]["accepted"])), (24, 1))
        after = self.accepted_files()
        self.assertEqual(sorted(after), sorted([*before, "fingerprint.json"]))
        for name, text in before.items():
            self.assertEqual(after[name], text, f"{name} must not change")
        fp = parse_fingerprint(after["fingerprint.json"])
        fp.require_restorable()
        self.assertEqual(len(self.sim.requests), sent)  # no mailbox traffic
        self.assertEqual(stat.S_IMODE(os.stat(self.acc / "fingerprint.json").st_mode), 0o644)
        self.assertEqual(sorted(p.name for p in self.acc.iterdir()), ["CURRENT", "EXPECTED-words.txt", "fingerprint.json", "profiles"])  # no temp file left
        records = [json.loads(line) for line in self.audit.read_text().splitlines() if line.strip()]
        self.assertEqual([x["op"] for x in records if x.get("op") == self.FP], [self.FP])

    def test_it_replaces_an_existing_fingerprint(self) -> None:
        h = self.accepted_without_fingerprint()
        (self.acc / "fingerprint.json").write_text("{}\n")
        self.assertTrue(self.fingerprint(h)["ok"])
        parse_fingerprint((self.acc / "fingerprint.json").read_text())

    def test_it_refuses_without_an_accepted_state_and_creates_nothing(self) -> None:
        self.acc = self.tmp / "etc" / "accepted"
        self.acc.parent.mkdir()
        self.acc.parent.chmod(0o755)
        h = self.boot(ALL_P_53, accepted_dir=self.acc)
        self.read(h)
        r = self.fingerprint(h)
        self.assertEqual(error_code(r), "REFUSED")
        self.assertEqual(r["error"]["detail"]["check"], "NO_ACCEPTED_STATE")
        self.assertFalse(self.acc.exists())

    def test_it_refuses_when_this_boots_read_lacks_the_fused_words(self) -> None:
        h = self.accepted_without_fingerprint()
        h.stop()
        h2 = self.start(self.config(accepted_dir=self.acc, boot_id_fn=lambda: "99999999-0000-0000-0000-000000000009"))  # another boot: the stored read is not used
        r = self.fingerprint(h2)
        self.assertEqual(error_code(r), "REFUSED")
        self.assertEqual(r["error"]["detail"]["check"], "NO_READ")
        self.assertFalse((self.acc / "fingerprint.json").exists())

    def test_after_a_helper_restart_the_stored_read_is_enough(self) -> None:
        h = self.accepted_without_fingerprint()
        h.stop()
        h2 = self.start(self.config(accepted_dir=self.acc))
        r = self.fingerprint(h2)
        self.assertTrue(r["ok"], r)

    def test_it_refuses_busy(self) -> None:
        h = self.accepted_without_fingerprint()
        h.helper.machine.begin("long1", "tune.apply", State.APPLYING)
        self.assertEqual(error_code(self.fingerprint(h)), "BUSY")
        h.helper.machine.end("long1")

    def test_under_the_simulator_it_says_the_identity_is_missing(self) -> None:
        h = self.boot_accepting()
        self.tune(h, rows(row("P3", points={"53": -60})))
        self.accept(h, **{"from": "read"})
        (self.acc / "fingerprint.json").unlink()
        r = self.fingerprint(h)
        self.assertTrue(r["ok"], r)
        self.assertIn("no CPU identity", r["result"]["fingerprint"])


class Item10AuditMode(TuneCase):
    def test_an_audit_log_created_by_a_helper_op_is_0600(self) -> None:
        h = self.boot()
        self.read(h)
        self.assertEqual(stat.S_IMODE(os.stat(self.audit).st_mode), 0o600)

    def test_a_log_created_group_readable_is_tightened_on_the_next_append(self) -> None:
        self.audit.write_text("")
        os.chmod(self.audit, 0o644)
        AuditLog(self.audit).append("result", op="x")
        self.assertEqual(stat.S_IMODE(os.stat(self.audit).st_mode), 0o600)


class Item12SimClockAfterRestart(LastReadCase):
    def test_apply_after_a_restart_is_not_refused_as_in_the_future(self) -> None:
        set_dwell(self, 5.0)
        h = self.boot()
        self.read(h)
        self.apply_ok(h, self.plan(h, rows(row("P3", points={"53": -60}))))
        h2 = self.restart(h)  # a new process: its virtual clock starts again, the audit log keeps the old process's timestamps
        plan = self.plan(h2, rows(row("P3", points={"53": -61})))
        res = self.apply_ok(h2, plan)  # was: REFUSED DWELL "previous mailbox operation ... is Ns in the future"
        self.assertTrue(res["complete"])
        self.assertEqual(self.word(3, 5).offset_raw, ops_tune.VfWord.mv_to_offset_raw(-61))

    def test_a_read_after_a_restart_works_too(self) -> None:
        set_dwell(self, 5.0)
        h = self.boot()
        self.read(h)
        h2 = self.restart(h)
        self.assertEqual(self.read(h2)["extras_notes"], {})


class Item13Small(LastReadCase):
    def test_a_plan_is_refused_when_the_audit_log_cannot_be_counted(self) -> None:
        h = self.boot()
        self.read(h)
        os.link(self.audit, self.tmp / "audit-alias")  # a hard-linked audit log is not trusted: the per-boot count is unknown
        r = h.call("p9", "tune.plan", {"intent": rows(row("P1", curve=-20))})
        self.assertEqual(error_code(r), "REFUSED", r)
        self.assertEqual(r["error"]["detail"]["check"], "AUDIT_UNAVAILABLE")

    def test_an_unknown_boot_id_reports_unknown_operations_not_a_count(self) -> None:
        h = self.boot(boot_id_fn=lambda: None)
        self.read(h)
        progress = [m for m in h.messages if m.get("event") == "progress"]
        self.assertTrue(progress)
        self.assertTrue(all(m["ops_used"] is None for m in progress), progress[:2])


class Item14OpsDone(TuneCase):
    def test_ops_done_is_every_mailbox_operation_of_the_read(self) -> None:
        h = self.boot()
        res = self.read(h)
        self.assertEqual(res["ops_done"], len(self.sim.requests))
        self.assertEqual(res["ops_words"] + res["ops_extras"] + res["ops_ring"], res["ops_done"])
        self.assertEqual(res["ops_ring"], 11)  # G-154: the ring walk (P1 = 2): the OC word, 9 points (the OC-created 40x slot included) and the rc 0x11 at P2 = 10
        self.assertGreater(res["ops_extras"], 0)
        self.assertEqual(res["ops_words"], sum(1 for q in self.sim.requests if q.cmd == 0x10 and q.p1 == 0))
        counted = sum(1 for line in self.audit.read_text().splitlines() if json.loads(line).get("event") == "attempt")
        self.assertEqual(res["ops_done"], counted)  # what the per-boot counter counted
