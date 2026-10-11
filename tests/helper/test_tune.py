"""G-112: tune.read / tune.plan / tune.apply / profiles.* through the real helper process loop, the simulator and the REAL advanced policy file (D-032).

No hardware, no sudo: ``SimMsrPort`` behind the real ``WriteGuard``. Words are checked in the simulator after the apply, not just in the response.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from arltuner.mailbox import VfWord
from arltuner.msr import SimConfig, SimMsrPort
from arltuner.msr.sim import POINT_RATIOS_ARL_S

from ._support import HelperCase, Harness, error_code

REPO = Path(__file__).resolve().parents[2]
ADVANCED = REPO / "policies" / "advanced-2026-10-11.toml"
ALL_P_53 = SimConfig.measured(per_point=True, reject_zero_fields=True, point_ratios=dict(POINT_RATIOS_ARL_S))  # every P-core has the favored cores' table


def texts(plan: dict[str, Any]) -> list[str]:
    """The ``text`` of every ``{code, text}`` warning of a tune.plan result."""
    assert all(set(w) == {"code", "text"} for w in plan["warnings"]), plan["warnings"]
    return [w["text"] for w in plan["warnings"]]


def rows(*row: dict[str, Any], scope: str = "unit") -> dict[str, Any]:
    return {"scope": scope, "rows": list(row)}


def row(target: str, *, curve: int | None = None, points: dict[str, int] | None = None, v: float | None = None, ratio: int | None = None) -> dict[str, Any]:
    offset = None if curve is None and points is None else ({"kind": "curve", "mv": curve} if curve is not None else {"kind": "points", "mv": points})
    return {"target": target, "offset": offset, "v_target_v": v, "ratio": ratio}


class TuneCase(HelperCase):
    def setUp(self) -> None:
        super().setUp()
        shutil.copy(ADVANCED, self.policy)  # the real D-032 policy file: the limits under test are the shipped ones

    def boot(self, config: SimConfig | None = None, **overrides: Any) -> Harness:
        if config is not None:
            self.sim = SimMsrPort(config)
        return self.start(self.config(**overrides))

    # ---- helpers
    def read(self, h: Harness, **args: Any) -> dict[str, Any]:
        r = h.call(f"read{len(h.messages)}", "tune.read", args)
        self.assertTrue(r["ok"], r)
        return r["result"]

    def plan(self, h: Harness, intent: dict[str, Any]) -> dict[str, Any]:
        r = h.call(f"plan{len(h.messages)}", "tune.plan", {"intent": intent})
        self.assertTrue(r["ok"], r)
        return r["result"]

    def apply(self, h: Harness, plan: dict[str, Any], typed: str | None = None) -> dict[str, Any]:
        return h.call(f"apply{len(h.messages)}", "tune.apply", {"plan_id": plan["plan_id"], "typed": typed or "CONFIRM " + plan["nonce"]}, timeout=60)

    def apply_ok(self, h: Harness, plan: dict[str, Any]) -> dict[str, Any]:
        r = self.apply(h, plan)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["result"]["complete"], r)
        return r["result"]

    def word(self, cpu: int, p2: int = 0) -> VfWord:
        return self.sim.vf(cpu, 0, p2)


class ReadTests(TuneCase):
    def test_tune_read_decodes_every_unit_and_remembers_the_words(self) -> None:
        h = self.boot()
        res = self.read(h)
        self.assertEqual(sorted(res["tune"]), sorted(["P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "EC0", "EC1", "EC2", "EC3"]))
        p0 = res["tune"]["P0"]["cpus"][0]
        self.assertEqual(p0["cpu"], 0)
        self.assertEqual([p["point"] for p in p0["points"]], list(range(9)))  # point 0 = the OC word, 1..8 the fused points
        self.assertEqual(p0["points"][0], {"point": 0, "point_label": "OC (point 9)", "ratio": 57, "offset_mv": 0.0, "v_target_v": None, "mode": "adaptive"})  # CPU 0: 0x39
        self.assertEqual(res["tune"]["P1"]["cpus"][0]["points"][0]["ratio"], None)  # OC word 0x0: unset fields are null
        p5 = next(p for p in p0["points"] if p["point"] == 5)
        self.assertEqual((p5["ratio"], p5["offset_mv"]), (53, 0.0))  # G-117: the simulator starts at cold-boot stock, point offsets 0
        self.assertEqual(res["units"][0]["id"], "P0")
        words = h.helper.last_words
        self.assertEqual(sorted(words), [*range(8), 8, 12, 16, 20])  # 8 P-cores + the lowest CPU of each E-cluster
        self.assertEqual(words[0][0], 0x39)
        self.assertEqual(len(words[0]), 9)

    def test_vf_dump_also_remembers_the_words(self) -> None:
        h = self.boot()
        r = h.call("d1", "vf.dump", {"cpus": [0, 8]})
        self.assertTrue(r["ok"], r)
        self.assertEqual(sorted(h.helper.last_words), [0, 8])
        self.assertIsNone(r["result"].get("tune"))  # vf.dump keeps its old shape

    def test_a_refused_dump_leaves_the_words_alone(self) -> None:
        h = self.boot()
        self.read(h, cpus=[0])
        before = dict(h.helper.last_words)
        self.sim.eio(8, 0x150)
        r = h.call("d2", "vf.dump", {"cpus": [8]})
        self.assertFalse(r["ok"])
        self.assertEqual(h.helper.last_words, before)

    def test_turbo_tables_are_read_when_the_policy_allows(self) -> None:
        h = self.boot()
        res = self.read(h)
        self.assertEqual(res["tables"], {"e": [48] * 8, "p": [57, 57, 55, 55, 55, 55, 55, 55]})

    def test_a_policy_without_msr_entries_reads_no_tables_and_says_so(self) -> None:
        text = ADVANCED.read_text().split("# --- plain-MSR access")[0]  # everything before the msr_allow entries
        self.policy.write_text(text)
        h = self.boot()
        res = self.read(h)
        self.assertIsNone(res["tables"])
        self.assertIn("turbo tables not read", res["tables_note"])


class PlanTests(TuneCase):
    def test_plan_needs_no_hardware_and_sends_nothing(self) -> None:
        h = self.boot()
        self.read(h)
        sent = len(self.sim.requests)
        p = self.plan(h, rows(row("P1", curve=-20)))
        self.assertEqual(len(self.sim.requests), sent)  # tune.plan is pure
        self.assertEqual(len(p["plan_id"]), 64)
        self.assertRegex(p["nonce"], r"^[A-Z2-9]{6}$")
        self.assertEqual(p["expires_s"], 600)
        self.assertEqual(p["units"], ["P1"])
        self.assertEqual(p["after"]["P1"]["0"]["offset_mv"], round(VfWord.mv_to_offset_raw(-20) * 1000 / 1024, 3))
        self.assertEqual(p["before"]["P1"]["0"], {"ratio": None, "offset_mv": 0.0, "v_target_v": None, "effective_v": None, "clamped_by": None})
        self.assertGreater(p["ops_needed"], 0)
        self.assertIsInstance(p["ops_left"], int)
        self.assertTrue(any("effective offset" in w for w in texts(p)))

    def test_points_by_ratio_need_a_read(self) -> None:
        h = self.boot()
        r = h.call("p", "tune.plan", {"intent": rows(row("P0", points={"53": -60}))})
        self.assertEqual(error_code(r), "REFUSED")
        self.assertEqual(r["error"]["detail"]["check"], "NO_READ")

    def test_a_ratio_the_unit_does_not_have_is_an_error_per_unit_and_a_skip_in_simple_scope(self) -> None:
        h = self.boot()
        self.read(h)
        r = h.call("p1", "tune.plan", {"intent": rows(row("P1", points={"58": -10}))})  # F1: every P-core's table stops at 57
        self.assertEqual(error_code(r), "BAD_REQUEST")
        self.assertIn("no V/F point at ratio 58", r["error"]["message"])
        p = self.plan(h, rows(row("P", points={"58": -10, "53": -10}), scope="simple"))
        self.assertTrue(any("P1 has no V/F point at ratio 58" in w for w in texts(p)))

    def test_policy_range_plus_minus_200_accepted_205_refused(self) -> None:
        h = self.boot()
        self.read(h)
        for mv in (200, -200):
            self.plan(h, rows(row("P2", curve=mv)))
            self.plan(h, rows(row("EC1", curve=mv)))
            self.plan(h, rows(row("P0", points={"53": mv})))
        for mv in (205, -205, 201, -201):
            for intent in (rows(row("P2", curve=mv)), rows(row("EC1", curve=mv)), rows(row("P0", points={"53": mv}))):
                r = h.call(f"r{mv}{len(h.messages)}", "tune.plan", {"intent": intent})
                self.assertEqual(error_code(r), "REFUSED", (mv, intent))
                self.assertEqual(r["error"]["detail"]["check"], "POLICY_RANGE")
                self.assertIn("outside the policy window", r["error"]["message"])

    def test_policy_range_v_target_and_ratio_ceilings(self) -> None:
        h = self.boot()
        self.read(h)
        self.plan(h, rows(row("P4", v=1.625)))
        self.plan(h, rows(row("EC0", v=1.355)))
        cases = {
            "P V target 1.626": row("P4", v=1.626),
            "E V target 1.356": row("EC2", v=1.356),
            "P ratio 58": row("P4", ratio=58),
            "E ratio 50": row("EC0", ratio=50),
        }
        for label, bad in cases.items():
            with self.subTest(label):
                r = h.call(f"v{len(h.messages)}", "tune.plan", {"intent": rows(bad)})
                self.assertEqual(error_code(r), "REFUSED")
                self.assertEqual(r["error"]["detail"]["check"], "POLICY_RANGE")
                self.assertRegex(r["error"]["message"], r"policy (v_target_max_mv|ratio_max)")
        self.plan(h, rows(row("P0", ratio=57)))
        self.plan(h, rows(row("EC3", ratio=49)))

    def test_shape_errors_are_bad_requests(self) -> None:
        h = self.boot()
        bad = {
            "not an object": [],
            "unknown key": {"scope": "unit", "rows": [], "x": 1},
            "unknown scope": {"scope": "all", "rows": [row("P0", curve=1)]},
            "unit in simple scope": rows(row("P0", curve=1), scope="simple"),
            "group in unit scope": rows(row("P", curve=1)),
            "duplicate target": rows(row("P0", curve=1), row("P0", curve=2)),
            "fractional mv": rows({"target": "P0", "offset": {"kind": "curve", "mv": 1.5}, "v_target_v": None, "ratio": None}),
            "bool ratio": rows({"target": "P0", "offset": None, "v_target_v": None, "ratio": True}),
            "nothing to do": rows(row("P0")),
            "bad reset": {"reset": "factory"},
            "bad restore": {"restore": "latest"},
            "bad profile name": {"profile": "../x"},
            "unknown row key": rows({"target": "P0", "offset": None, "v_target_v": None, "ratio": 3, "static": True}),
        }
        for label, intent in bad.items():
            with self.subTest(label):
                r = h.call(f"b{len(h.messages)}", "tune.plan", {"intent": intent})
                self.assertEqual(error_code(r), "BAD_REQUEST", r)

    def test_a_policy_with_a_step_limit_blocks_the_plan_with_a_named_check(self) -> None:
        self.policy.write_text(ADVANCED.read_text().replace("max_offset_step_mv = 200", "max_offset_step_mv = 25"))
        h = self.boot()
        self.read(h)
        p = self.plan(h, rows(row("P2", curve=-150)))  # the plan itself is pure; the step is judged against the words read at apply time
        r = self.apply(h, p)
        self.assertEqual(error_code(r), "REFUSED")
        self.assertEqual(r["error"]["detail"]["check"], "STEP_LIMIT")
        self.assertEqual(self.word(2).offset_raw, 0)  # nothing written


class ApplyWordTests(TuneCase):
    def test_whole_curve_offset_on_one_p_core(self) -> None:
        h = self.boot()
        self.read(h)
        res = self.apply_ok(h, self.plan(h, rows(row("P2", curve=-30))))
        self.assertEqual(self.word(2).offset_raw, VfWord.mv_to_offset_raw(-30))
        self.assertEqual(self.word(2).ratio, 55)  # the XTU-shaped word: the ratio is 0x1C[7:0], never a zero field
        self.assertTrue(all(rb["match"] for rb in res["readback"]), res["readback"])
        self.assertEqual({rb["unit"] for rb in res["readback"]}, {"P2"})
        self.assertEqual(self.word(1).offset_raw, 0)  # nothing else moved

    def test_the_first_hardware_sittings_p0_whole_curve_and_ec1_oc_point(self) -> None:
        """G-120 [MEASURED lnx 2026-10-11] (tuning-results.md "First write sittings"): P0's OC ratio 57 is the top fused ratio = no OC point, so a whole-curve offset is
        ratio | offset << 21 with target 0 and reads back whole (the hardware kept 0xFEC00039 where the old plan wrote 0xFEC5B439 and tripped); an OC ratio above the
        top fused one is an OC point and keeps its adaptive target (EC1 48x 1.1367 V = 0x48C30)."""
        h = self.boot()
        self.read(h)
        self.assertFalse(getattr(h.helper, "tuned_this_boot", False))
        res = self.apply_ok(h, self.plan(h, rows(row("P0", curve=-10))))
        self.assertEqual(self.word(0).to_u32(), 0xFEC00039)
        self.assertTrue(all(rb["match"] for rb in res["readback"]), res["readback"])
        self.assertTrue(h.helper.tuned_this_boot)  # fix 4: any apply this boot ends "stock"
        res = self.apply_ok(h, self.plan(h, rows(row("P0", curve=0))))  # back to 0: XTU-shaped word, target kept with offset 0 (read back 0x0005B439)
        self.assertEqual(self.word(0).to_u32(), 0x0005B439)
        self.assertTrue(all(rb["match"] for rb in res["readback"]), res["readback"])
        res = self.apply_ok(h, self.plan(h, rows(row("EC1", ratio=48, v=1.1367))))
        self.assertEqual(self.word(12).to_u32(), 0x00048C30)
        self.assertEqual(self.sim.vf(12, 0, 7).ratio, 48)  # the OC point appeared as P2 = 7
        self.assertTrue(all(rb["match"] for rb in res["readback"]), res["readback"])

    def test_per_point_offset_by_ratio_leaves_the_point_ratio_and_v_target_alone(self) -> None:
        h = self.boot(ALL_P_53)
        self.read(h)
        before = self.word(3, 5)
        res = self.apply_ok(h, self.plan(h, rows(row("P3", points={"53": -70, "48": -5}))))
        after = self.word(3, 5)
        self.assertEqual((after.ratio, after.v_target_raw), (before.ratio, before.v_target_raw))
        self.assertEqual(after.offset_raw, VfWord.mv_to_offset_raw(-70))
        self.assertEqual(self.word(3, 4).offset_raw, VfWord.mv_to_offset_raw(-5))
        self.assertEqual(self.word(3, 6).offset_raw, VfWord.from_u32(h.helper.last_words[3][6]).offset_raw)  # untouched point
        pts = sorted(rb["point"] for rb in res["readback"])
        self.assertEqual(pts, [4, 5])
        self.assertEqual(h.helper.last_words[3][5], after.to_u32())  # the words in force follow the read-backs

    def test_simple_scope_p_and_e_two_rows(self) -> None:
        h = self.boot(ALL_P_53)
        self.read(h)
        plan = self.plan(h, rows(row("P", points={"53": -55}), row("E", points={"46": -25}), scope="simple"))
        self.assertEqual(plan["units"], [*(f"P{i}" for i in range(8)), *(f"EC{i}" for i in range(4))])
        self.apply_ok(h, plan)
        for cpu in range(8):
            self.assertEqual(self.word(cpu, 5).offset_raw, VfWord.mv_to_offset_raw(-55), cpu)
        for cpu in range(8, 24):  # every CPU of every cluster, not just the ones that were read
            self.assertEqual(self.word(cpu, 6).offset_raw, VfWord.mv_to_offset_raw(-25), cpu)
            self.assertEqual(self.word(cpu, 6).ratio, 46)

    def test_per_unit_rows_are_independent(self) -> None:
        h = self.boot(ALL_P_53)
        self.read(h)
        self.apply_ok(h, self.plan(h, rows(row("P0", points={"57": -30}), row("EC1", curve=-20), row("EC3", points={"40": -15}))))
        self.assertEqual(self.word(0, 8).offset_raw, VfWord.mv_to_offset_raw(-30))
        self.assertEqual({self.word(c).offset_raw for c in range(12, 16)}, {VfWord.mv_to_offset_raw(-20)})
        self.assertEqual({self.word(c, 5).offset_raw for c in range(20, 24)}, {VfWord.mv_to_offset_raw(-15)})
        self.assertEqual({self.word(c).offset_raw for c in (8, 9, 10, 11, 16, 17)}, {0})

    def test_oc_point_v_target_is_written_to_the_oc_word_only(self) -> None:
        h = self.boot(ALL_P_53)
        self.read(h)
        p5_before = self.word(4, 5)
        plan = self.plan(h, rows(row("P4", v=1.300)))
        self.assertEqual(plan["after"]["P4"]["0"]["v_target_v"], round(1331 / 1024, 4))
        self.assertEqual(sorted(plan["after"]["P4"]), ["0"])  # no point word is part of a V target change
        self.apply_ok(h, plan)
        self.assertEqual(self.word(4).v_target_raw, round(1.3 * 1024))
        self.assertEqual(self.word(4, 5), p5_before)  # a point word keeps its (zero) V target field

    def test_ratio_raise_adds_the_turbo_table_write(self) -> None:
        h = self.boot()
        self.read(h)
        plan = self.plan(h, rows(row("EC0", ratio=49)))
        self.assertTrue(any(s["kind"] == "write_msr" and s["table"] == "e" for s in plan["steps"]), plan["steps"])
        res = self.apply_ok(h, plan)
        self.assertEqual({self.word(c).ratio for c in range(8, 12)}, {49})
        self.assertEqual(self.sim.plain_msr(0, 0x650), 0x3131313131313131)  # every bucket raised to 49
        table = next(rb for rb in res["readback"] if rb["point"] == "turbo")
        self.assertTrue(table["match"])

    def test_ratio_without_a_table_read_warns_and_writes_no_table(self) -> None:
        self.policy.write_text(ADVANCED.read_text().split("# --- plain-MSR access")[0])
        h = self.boot()
        self.read(h)
        plan = self.plan(h, rows(row("EC0", ratio=49)))
        self.assertFalse(any(s["kind"] == "write_msr" for s in plan["steps"]))
        self.assertTrue(any("turbo table" in w and "not read" in w for w in texts(plan)))

    def test_whole_apply_is_one_guarded_transaction_at_a_time(self) -> None:
        h = self.boot(ALL_P_53)
        self.read(h)
        self.apply_ok(h, self.plan(h, rows(row("P", points={"53": -55}), scope="simple")))
        audit = [json.loads(line) for line in self.audit.read_text().splitlines()]
        attempts = [a for a in audit if a.get("event") == "attempt"]
        self.assertTrue(attempts)
        self.assertTrue(all(int(a["cmd"], 16) in (0x10, 0x11, 0x14, 0x15, 0x1C, 0x1D, 0x26, 0x27, 0x09, 0x28) for a in attempts if "cmd" in a))
        seqs = [a["seq"] for a in audit]
        self.assertEqual(seqs, sorted(set(seqs)))  # one transaction after the other: every request audited before it was sent, in order
        self.assertEqual(sum(1 for a in audit if a.get("event") == "attempt" and int(a.get("cmd", "0x0"), 16) == 0x11), len(self.sim_writes()))

    def sim_writes(self) -> list[Any]:
        return [r for r in self.sim.requests if r.cmd == 0x11]


class NonceTests(TuneCase):
    def test_replay_is_confirm_mismatch_and_changes_nothing(self) -> None:
        h = self.boot()
        self.read(h)
        plan = self.plan(h, rows(row("P2", curve=-30)))
        self.apply_ok(h, plan)
        sent = len(self.sim.requests)
        r = self.apply(h, plan)
        self.assertEqual(error_code(r), "CONFIRM_MISMATCH")
        self.assertEqual(len(self.sim.requests), sent)

    def test_wrong_nonce_is_refused_and_does_not_burn_the_plan(self) -> None:
        h = self.boot()
        self.read(h)
        plan = self.plan(h, rows(row("P2", curve=-30)))
        wrong = "CONFIRM " + ("AAAAAA" if plan["nonce"] != "AAAAAA" else "BBBBBB")
        r = self.apply(h, plan, wrong)
        self.assertEqual(error_code(r), "CONFIRM_MISMATCH")
        self.assertEqual(self.word(2).offset_raw, 0)
        self.apply_ok(h, plan)  # the right nonce still works

    def test_three_wrong_nonces_drop_the_plan(self) -> None:
        h = self.boot()
        self.read(h)
        plan = self.plan(h, rows(row("P2", curve=-30)))
        wrong = "CONFIRM " + ("AAAAAA" if plan["nonce"] != "AAAAAA" else "BBBBBB")
        for _ in range(3):
            self.assertEqual(error_code(self.apply(h, plan, wrong)), "CONFIRM_MISMATCH")
        self.assertEqual(error_code(self.apply(h, plan)), "PLAN_STALE")

    def test_unknown_plan_is_stale(self) -> None:
        h = self.boot()
        r = h.call("a", "tune.apply", {"plan_id": "0" * 64, "typed": "CONFIRM ABCDEF"})
        self.assertEqual(error_code(r), "PLAN_STALE")

    def test_plan_expires_after_600_seconds(self) -> None:
        skew = [0.0]
        h = self.boot(monotonic=lambda: time.monotonic() + skew[0])
        self.read(h)
        plan = self.plan(h, rows(row("P2", curve=-30)))
        skew[0] = 601.0
        r = self.apply(h, plan)
        self.assertEqual(error_code(r), "PLAN_STALE")
        self.assertIn("600", r["error"]["message"])
        self.assertEqual(self.word(2).offset_raw, 0)

    def test_a_word_that_changed_since_the_plan_makes_it_stale_before_any_write(self) -> None:
        h = self.boot(ALL_P_53)
        self.read(h)
        first = self.plan(h, rows(row("P3", points={"53": -60})))
        second = self.plan(h, rows(row("P3", points={"53": -70})))
        self.apply_ok(h, first)
        writes_after_first = [r for r in self.sim.requests if r.cmd == 0x11]
        r = self.apply(h, second)
        self.assertEqual(error_code(r), "PLAN_STALE")
        self.assertIn("a word changed since the plan", r["error"]["message"])
        self.assertEqual([x for x in self.sim.requests if x.cmd == 0x11], writes_after_first)  # the re-reads happened, no new write
        self.assertEqual(self.word(3, 5).offset_raw, VfWord.mv_to_offset_raw(-60))

    def test_out_of_band_change_between_plan_and_apply_is_stale(self) -> None:
        h = self.boot(ALL_P_53)
        self.read(h)
        plan = self.plan(h, rows(row("P3", points={"53": -60})))
        self._poke_point(3, 5, self.word(3, 5).with_(offset_raw=-3))  # somebody else (the CLI, XTU) writes the point behind the helper's back
        r = self.apply(h, plan)
        self.assertEqual(error_code(r), "PLAN_STALE")

    def _poke_point(self, cpu: int, p2: int, word: VfWord) -> None:
        from arltuner.mailbox import Cmd
        from arltuner.msr import MailboxTransport

        MailboxTransport(self.sim, sleep=lambda s: None).transact(cpu, int(Cmd.VOLTAGE_FREQ_WRITE), 0, p2, word.to_u32())


class StateMachineTests(TuneCase):
    def test_apply_runs_in_the_applying_state_and_blocks_other_long_ops(self) -> None:
        h = self.boot()
        self.read(h)
        plan = self.plan(h, rows(row("P2", curve=-30)))
        self.apply_ok(h, plan)
        states = [e["state"] for e in h.events("state")]
        self.assertIn("applying", states)
        self.assertEqual(states[-1], "idle")

    def test_every_write_op_is_long(self) -> None:
        h = self.boot()
        self.assertEqual([n for n, s in h.helper.ops.items() if s.write and not s.long], [])
        for name in ("tune.read", "tune.apply", "vf.dump"):
            self.assertTrue(h.helper.ops[name].long and h.helper.ops[name].write, name)
        for name in ("tune.plan", "profiles.list", "profiles.save"):
            self.assertFalse(h.helper.ops[name].write, name)

    def test_the_write_gate_applies_to_apply_on_hardware_mode(self) -> None:
        from ._support import READ_ONLY_REPORT

        h = self.boot(simulator=False, preflight_fn=lambda: READ_ONLY_REPORT, health_gate_factory=lambda: (lambda phase: (True, "ok")))
        plan_args = {"intent": rows(row("P2", curve=-30))}
        # a plan does not need the gate (it is pure) ...
        r = h.call("p", "tune.plan", plan_args)
        self.assertTrue(r["ok"], r)
        # ... the apply does, on every non-simulator helper
        a = h.call("a", "tune.apply", {"plan_id": r["result"]["plan_id"], "typed": "CONFIRM " + r["result"]["nonce"]})
        self.assertEqual(error_code(a), "PREFLIGHT_FAILED")
        self.assertEqual(self.sim.requests, [])


class FailureTests(TuneCase):
    def test_a_read_back_mismatch_is_reported_with_the_words(self) -> None:
        h = self.boot(ALL_P_53)
        self.read(h)
        plan = self.plan(h, rows(row("P3", points={"53": -60}), row("P4", points={"53": -60})))
        self.sim.silent_ignore(0x11)  # the chip accepts the write (rc 0) and keeps the old word: the guard's read-back differs
        r = self.apply(h, plan)
        self.assertTrue(r["ok"], r)  # a partly applied state is a result the page must show, not an error
        res = r["result"]
        self.assertFalse(res["complete"])
        self.assertEqual(res["stop"]["kind"], "unverified")
        self.assertTrue(res["stop"]["tripped"])
        bad = [rb for rb in res["readback"] if not rb["match"]]
        self.assertTrue(bad)
        self.assertNotEqual(bad[0]["expected"], bad[0]["actual"])
        self.assertTrue(any(s["status"] == "not_sent" for s in res["steps"]))  # the second core's write was never sent
        self.assertEqual(h.helper.last_words[3][5], self.word(3, 5).to_u32())  # the words in force are what the chip answered
        self.assertEqual(self.word(3, 5).offset_raw, 0)  # the chip kept the stock word

    def test_a_refused_guard_check_before_any_write_is_an_error(self) -> None:
        # the plan is valid today; the policy file then loses the point write entry: the pre-flight at apply time refuses and nothing is sent
        h = self.boot(ALL_P_53)
        self.read(h)
        plan = self.plan(h, rows(row("P3", points={"53": -60})))
        text = ADVANCED.read_text()
        start = text.index('[[allow]]\nname = "vf_point_write"')
        end = text.index("[[allow]]", start + 10)
        self.policy.write_text(text[:start] + text[end:])
        before = len(self.sim.requests)
        r = self.apply(h, plan)
        self.assertEqual(error_code(r), "REFUSED")
        self.assertEqual(r["error"]["detail"]["check"], "NOT_ALLOWLISTED")
        self.assertEqual(len(self.sim.requests), before)


class ResetRestoreTests(TuneCase):
    def accepted_dir(self, *names: str) -> Path:
        acc = self.tmp / "accepted"
        (acc / "profiles").mkdir(parents=True)
        (acc / "CURRENT").write_text("policy = policies/x.toml\n" + "".join(f"profile = profiles/{n}.toml\n" for n in names))
        return acc

    def test_reset_zeroes_offsets_and_nothing_else(self) -> None:
        h = self.boot(ALL_P_53)
        self.read(h)
        self.apply_ok(h, self.plan(h, rows(row("P3", points={"53": -60}), row("P4", curve=-40))))
        self.read(h)
        plan = self.plan(h, {"reset": "stock"})
        self.assertIn("P4", plan["units"])
        self.assertTrue(any("not changed" in w for w in texts(plan)))
        self.apply_ok(h, plan)
        for cpu in range(8):
            self.assertEqual(self.word(cpu).offset_raw, 0, cpu)
            for point in range(1, 9):
                self.assertEqual(self.word(cpu, point).offset_raw, 0, (cpu, point))  # the BIOS-style -50 mV of the simulator's tables is zeroed too
        self.assertEqual(self.word(0).ratio, 57)  # the OC ratio is not touched

    def test_reset_with_nothing_to_reset(self) -> None:
        zero = SimConfig.measured(per_point=True, reject_zero_fields=True, point_offsets={}, cpu_point_offsets={})
        h = self.boot(zero)
        self.read(h)
        r = h.call("z", "tune.plan", {"intent": {"reset": "stock"}})
        self.assertEqual(error_code(r), "BAD_REQUEST")
        self.assertEqual(r["error"]["detail"]["check"], "NOTHING_TO_DO")

    def test_reset_needs_a_read(self) -> None:
        h = self.boot()
        r = h.call("z", "tune.plan", {"intent": {"reset": "stock"}})
        self.assertEqual(error_code(r), "REFUSED")
        self.assertEqual(r["error"]["detail"]["check"], "NO_READ")

    def test_restore_accepted_merges_the_chain_and_applies(self) -> None:
        acc = self.accepted_dir("e-mini", "p-mini")
        (acc / "profiles/e-mini.toml").write_text(
            '[meta]\nname = "e-mini"\nplatform = "arl-s-285k"\n[[point]]\nkind = "E"\nindex = 0\npoint = 6\noffset_mv = -35\nexpect_ratio = 46\n'
        )
        (acc / "profiles/p-mini.toml").write_text(
            '[meta]\nname = "p-mini"\nplatform = "arl-s-285k"\n[[point]]\nkind = "P"\nindex = 0\npoint = 5\noffset_mv = -50\nexpect_ratio = 53\n'
            '[[point]]\nkind = "P"\nindex = 0\npoint = 6\noffset_mv = -28\nexpect_ratio = 55\n'
        )
        h = self.boot(accepted_dir=acc)
        plan = self.plan(h, {"restore": "accepted"})  # no tune.read needed: the points carry expect_ratio
        self.assertTrue(any("no tune.read" in w for w in texts(plan)))
        self.assertEqual(sorted(plan["units"]), ["EC0", "P0"])
        self.apply_ok(h, plan)
        self.assertEqual(self.word(0, 6).offset_raw, VfWord.mv_to_offset_raw(-28))
        self.assertEqual({self.word(c, 6).offset_raw for c in range(8, 12)}, {VfWord.mv_to_offset_raw(-35)})

    def test_restore_accepted_later_profile_overrides_the_earlier_one(self) -> None:
        acc = self.accepted_dir("first", "second")
        head = '[meta]\nname = "n"\nplatform = "arl-s-285k"\n'
        (acc / "profiles/first.toml").write_text(head + '[[point]]\nkind = "P"\nindex = 0\npoint = 5\noffset_mv = -10\nexpect_ratio = 53\n')
        (acc / "profiles/second.toml").write_text(head + '[[point]]\nkind = "P"\nindex = 0\npoint = 5\noffset_mv = -50\nexpect_ratio = 53\n')
        h = self.boot(accepted_dir=acc)
        plan = self.plan(h, {"restore": "accepted"})
        self.assertAlmostEqual(plan["after"]["P0"]["5"]["offset_mv"], VfWord.mv_to_offset_raw(-50) * 1000 / 1024, places=2)

    def test_the_repos_own_accepted_chain_plans_cleanly(self) -> None:
        acc = self.tmp / "accepted"
        (acc / "profiles").mkdir(parents=True)
        shutil.copy(REPO / "profiles/CURRENT", acc / "CURRENT")
        for line in (REPO / "profiles/CURRENT").read_text().splitlines():
            if line.startswith("profile"):
                rel = line.split("=", 1)[1].strip()
                shutil.copy(REPO / rel, acc / rel)
        h = self.boot(accepted_dir=acc)
        plan = self.plan(h, {"restore": "accepted"})
        self.assertEqual(set(plan["after"]), {*(f"P{i}" for i in range(8)), *(f"EC{i}" for i in range(4))})
        self.assertEqual(plan["after"]["P0"]["5"]["offset_mv"], round(VfWord.mv_to_offset_raw(-50) * 1000 / 1024, 3))  # p-step7: 53x -50 on all P
        self.assertTrue(any("e-p6-split3, p-boot-ramp, p-step7" in w for w in texts(plan)))

    def test_restore_without_accepted_state_is_a_named_refusal(self) -> None:
        h = self.boot()
        r = h.call("r", "tune.plan", {"intent": {"restore": "accepted"}})
        self.assertEqual(error_code(r), "REFUSED")
        self.assertEqual(r["error"]["detail"]["check"], "NO_ACCEPTED_STATE")


class ProfilesTests(TuneCase):
    def test_save_list_and_plan_from_a_saved_profile(self) -> None:
        h = self.boot(ALL_P_53)
        self.read(h)
        intent = rows(row("P3", points={"53": -61}))
        r = h.call("s", "profiles.save", {"name": "my-p3", "intent": intent})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["source"], "saved")
        listed = h.call("l", "profiles.list", {})["result"]["profiles"]
        self.assertIn({"name": "my-p3", "source": "saved", "created": r["result"]["created"]}, listed)
        plan = self.plan(h, {"profile": "my-p3"})
        self.assertTrue(any("saved profile my-p3" in w for w in texts(plan)))
        self.apply_ok(h, plan)
        self.assertEqual(self.word(3, 5).offset_raw, VfWord.mv_to_offset_raw(-61))

    def test_never_overwritten(self) -> None:
        h = self.boot()
        intent = rows(row("P3", curve=-10))
        self.assertTrue(h.call("s1", "profiles.save", {"name": "a", "intent": intent})["ok"])
        r = h.call("s2", "profiles.save", {"name": "a", "intent": rows(row("P3", curve=-20))})
        self.assertEqual(error_code(r), "REFUSED")
        self.assertEqual(r["error"]["detail"]["check"], "EXISTS")
        self.read(h)
        plan = self.plan(h, {"profile": "a"})
        self.assertEqual(plan["after"]["P3"]["0"]["offset_mv"], round(VfWord.mv_to_offset_raw(-10) * 1000 / 1024, 3))  # the first one stayed

    def test_names_and_shapes(self) -> None:
        h = self.boot()
        intent = rows(row("P3", curve=-10))
        for name in ("A", "has space", "", "x" * 65, "a/b", "a.json/../b", "naïve"):
            with self.subTest(name=name):
                self.assertEqual(error_code(h.call(f"n{len(h.messages)}", "profiles.save", {"name": name, "intent": intent})), "BAD_REQUEST")
        for intent in ({"reset": "stock"}, {"profile": "x"}, {"restore": "accepted"}):
            self.assertEqual(error_code(h.call(f"i{len(h.messages)}", "profiles.save", {"name": "ok", "intent": intent})), "BAD_REQUEST")
        self.assertEqual(h.call("e", "profiles.list", {})["result"]["profiles"], [])

    def test_accepted_profiles_are_listed_and_protected(self) -> None:
        acc = self.tmp / "accepted"
        (acc / "profiles").mkdir(parents=True)
        (acc / "CURRENT").write_text("policy = policies/x.toml\nprofile = profiles/p-step7.toml\n")
        (acc / "profiles/p-step7.toml").write_text('[meta]\nname = "x"\nplatform = "arl-s-285k"\n[[point]]\nkind = "P"\nindex = 0\npoint = 5\noffset_mv = -50\n')
        h = self.boot(accepted_dir=acc)
        listed = h.call("l", "profiles.list", {})["result"]["profiles"]
        self.assertEqual([p["name"] for p in listed if p["source"] == "accepted"], ["p-step7"])
        r = h.call("s", "profiles.save", {"name": "p-step7", "intent": rows(row("P3", curve=-10))})
        self.assertEqual(error_code(r), "REFUSED")
        self.assertEqual(r["error"]["detail"]["check"], "EXISTS")
        plan = self.plan(h, {"profile": "p-step7"})
        self.assertEqual(plan["units"], ["P0"])

    def test_unknown_profile_is_a_bad_request(self) -> None:
        h = self.boot()
        r = h.call("p", "tune.plan", {"intent": {"profile": "nope"}})
        self.assertEqual(error_code(r), "BAD_REQUEST")
